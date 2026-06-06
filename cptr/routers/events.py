"""Unified system events WebSocket: fs watching + port scanning.

Replaces the old watch.py with a single multiplexed event stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from cptr.utils.terminal import manager
from cptr.utils.config import check_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])

# ── Filesystem watcher ────────────────────────────────────────────


class _ChangeCollector(FileSystemEventHandler):
    """Collects fs events into a set for debounced delivery."""

    def __init__(self) -> None:
        self._changes: set[str] = set()

    def _record(self, event: FileSystemEvent) -> None:
        path = event.src_path
        self._changes.add(str(Path(path).parent))
        self._changes.add(path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._record(event)
        if hasattr(event, "dest_path"):
            self._changes.add(str(Path(event.dest_path).parent))
            self._changes.add(event.dest_path)

    def drain(self) -> list[str]:
        paths = list(self._changes)
        self._changes.clear()
        return paths


async def _fs_watcher_loop(ws: WebSocket, initial_path: str, path_holder: dict) -> None:
    """Watch filesystem and push fs_change events."""
    observer = Observer()
    observer.daemon = True
    observer.start()

    handler = _ChangeCollector()
    target = str(Path(initial_path).resolve())
    path_holder["current"] = target

    try:
        watch = observer.schedule(handler, target, recursive=True)
    except Exception as e:
        logger.warning(f"Failed to watch {target}: {e}")
        return

    try:
        while True:
            await asyncio.sleep(1.0)

            # Check if path changed (from receive loop)
            new_path = path_holder.get("pending")
            if new_path:
                del path_holder["pending"]
                resolved = str(Path(new_path).resolve())
                if resolved != path_holder["current"] and Path(resolved).is_dir():
                    try:
                        observer.unschedule(watch)
                    except Exception:
                        pass
                    handler = _ChangeCollector()
                    watch = observer.schedule(handler, resolved, recursive=True)
                    path_holder["current"] = resolved
                    logger.info(f"FS watch path changed to {resolved}")

            paths = handler.drain()
            if paths:
                # Normalize to forward slashes so the frontend can compare with /
                if sys.platform == "win32":
                    paths = [p.replace("\\", "/") for p in paths]
                try:
                    await ws.send_json({"type": "fs_change", "paths": paths})
                except Exception:
                    break
    finally:
        try:
            observer.stop()
        except Exception:
            pass


# ── Port scanning ─────────────────────────────────────────────────

# System processes to ignore
_IGNORED_PROCESSES = {
    "sshd", "cupsd", "mDNSResponder", "rapportd", "systemd-resolve",
    "avahi-daemon", "dnsmasq", "launchd", "systemd", "ntpd",
    "bluetoothd", "AirPlayXPCHelper", "ControlCenter",
}

# Ports that are almost certainly system services
_SYSTEM_PORTS = {22, 53, 80, 443, 631, 5353}


def _get_ppid(pid: int) -> int:
    """Get parent PID. Cross-platform."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "ParentProcessId", "/value"],
                capture_output=True, text=True, timeout=3,
            )
            for line in r.stdout.splitlines():
                if line.startswith("ParentProcessId="):
                    return int(line.split("=", 1)[1])
            return 0
        elif sys.platform == "linux":
            with open(f"/proc/{pid}/stat") as f:
                return int(f.read().split()[3])
        else:
            r = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            return int(r.stdout.strip())
    except Exception:
        return 0


def _find_session_for_pid(pid: int) -> Optional[str]:
    """Walk up the process tree to find which terminal session spawned this PID."""
    current = pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        visited.add(current)
        for session in manager._sessions.values():
            # Unix: match by child PID
            if session._pid == current:
                return session.session_id
            # Windows: winpty process has a .pid attribute
            if session._process is not None:
                try:
                    if getattr(session._process, "pid", None) == current:
                        return session.session_id
                except Exception:
                    pass
        current = _get_ppid(current)
    return None


def _get_process_name(pid: int) -> str:
    """Get process name from PID."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3,
            )
            # Output: "name.exe","1234",...
            line = r.stdout.strip()
            if line and line.startswith('"'):
                return line.split('"')[1]
            return "unknown"
        elif sys.platform == "linux":
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        else:
            r = subprocess.run(
                ["ps", "-o", "comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            name = r.stdout.strip()
            # macOS returns full path, extract basename
            return os.path.basename(name) if name else "unknown"
    except Exception:
        return "unknown"


def _scan_ports_darwin() -> list[dict]:
    """Scan listening ports on macOS using lsof."""
    try:
        r = subprocess.run(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP", "-F", "pcn"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return []

        ports = []
        current_pid = 0
        current_process = ""
        for line in r.stdout.splitlines():
            if line.startswith("p"):
                current_pid = int(line[1:])
            elif line.startswith("c"):
                current_process = line[1:]
            elif line.startswith("n"):
                # Format: n*:PORT or nhost:PORT
                addr = line[1:]
                if ":" in addr:
                    port_str = addr.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        ports.append({
                            "port": port,
                            "pid": current_pid,
                            "process": current_process,
                        })
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


def _scan_ports_linux() -> list[dict]:
    """Scan listening ports on Linux using /proc/net/tcp."""
    ports = []
    try:
        with open("/proc/net/tcp") as f:
            for line in f.readlines()[1:]:  # skip header
                parts = line.split()
                if parts[3] == "0A":  # LISTEN state
                    local = parts[1]
                    port = int(local.split(":")[1], 16)
                    inode = int(parts[9])
                    # Find PID for this inode
                    pid = _inode_to_pid(inode)
                    process = _get_process_name(pid) if pid else "unknown"
                    ports.append({"port": port, "pid": pid or 0, "process": process})
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
    return ports


def _inode_to_pid(inode: int) -> Optional[int]:
    """Map a socket inode to a PID on Linux."""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                fd_dir = f"/proc/{entry}/fd"
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(f"{fd_dir}/{fd}")
                        if f"socket:[{inode}]" in link:
                            return int(entry)
                    except (OSError, ValueError):
                        continue
            except (OSError, PermissionError):
                continue
    except Exception:
        pass
    return None


def _scan_ports_windows() -> list[dict]:
    """Scan listening ports on Windows using netstat."""
    try:
        r = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
        )
        ports = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts:
                local = parts[1]
                if ":" in local:
                    port_str = local.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        pid = int(parts[-1])
                        ports.append({
                            "port": port,
                            "pid": pid,
                            "process": _get_process_name(pid),
                        })
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


def _scan_ports() -> list[dict]:
    """Scan listening ports. Cross-platform."""
    system = platform.system()
    if system == "Darwin":
        raw = _scan_ports_darwin()
    elif system == "Linux":
        raw = _scan_ports_linux()
    elif system == "Windows":
        raw = _scan_ports_windows()
    else:
        return []

    # Get our own PID to filter ourselves out
    our_pid = os.getpid()

    # Deduplicate by port and filter
    seen: set[int] = set()
    filtered = []
    for entry in raw:
        port = entry["port"]
        if port in seen:
            continue
        seen.add(port)

        # Skip system ports and our own process
        if port in _SYSTEM_PORTS:
            continue
        if entry["pid"] == our_pid:
            continue
        if entry["process"] in _IGNORED_PROCESSES:
            continue

        # Session attribution: only include ports spawned by our terminals
        if entry["pid"]:
            session_id = _find_session_for_pid(entry["pid"])
            if session_id:
                entry["session_id"] = session_id
                filtered.append(entry)
            # else: not from a cptr terminal, skip
        # pid=0 or no pid: skip

    return filtered


async def _port_scanner_loop(ws: WebSocket) -> None:
    """Periodically scan ports and push add/remove events."""
    known: dict[int, dict] = {}  # port -> info

    while True:
        await asyncio.sleep(3)

        try:
            current_ports = {p["port"]: p for p in _scan_ports()}
        except Exception as e:
            logger.warning(f"Port scan error: {e}")
            continue

        # Detect new ports
        for port, info in current_ports.items():
            if port not in known:
                try:
                    await ws.send_json({
                        "type": "port_added",
                        "port": info["port"],
                        "pid": info["pid"],
                        "process": info["process"],
                        "session_id": info.get("session_id"),
                    })
                except Exception:
                    return

        # Detect removed ports
        for port in list(known.keys()):
            if port not in current_ports:
                try:
                    await ws.send_json({
                        "type": "port_removed",
                        "port": port,
                    })
                except Exception:
                    return

        known = current_ports


# ── Receive loop ──────────────────────────────────────────────────

async def _receive_loop(ws: WebSocket, path_holder: dict) -> None:
    """Handle incoming messages from the client."""
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "watch_path":
                new_path = msg.get("path")
                if new_path:
                    path_holder["pending"] = new_path
    except (WebSocketDisconnect, Exception):
        pass


# ── WebSocket endpoint ────────────────────────────────────────────

@router.websocket("/ws")
async def events_ws(websocket: WebSocket, path: str = Query("/", description="Initial fs watch path")):
    """Unified system events WebSocket.

    Pushes:
      - {"type": "fs_change", "paths": [...]}
      - {"type": "port_added", "port": N, "pid": N, "process": "...", "session_id": "..." | null}
      - {"type": "port_removed", "port": N}

    Receives:
      - {"type": "watch_path", "path": "..."}: change fs watch directory
    """
    target = Path(path).resolve()
    if not target.is_dir():
        await websocket.close(code=4000, reason="Not a directory")
        return

    # Auth check for WebSocket
    client_host = websocket.client.host if websocket.client else "127.0.0.1"
    token = websocket.cookies.get("cptr_session") or websocket.query_params.get("token")
    auth = check_access(client_host=client_host, jwt_token=token)
    if auth is None:
        await websocket.close(code=4001, reason="unauthorized")
        return

    await websocket.accept()
    logger.info(f"Events WS connected, watching {target}")

    path_holder: dict = {}

    fs_task = asyncio.create_task(_fs_watcher_loop(websocket, str(target), path_holder))
    port_task = asyncio.create_task(_port_scanner_loop(websocket))
    recv_task = asyncio.create_task(_receive_loop(websocket, path_holder))

    try:
        done, pending = await asyncio.wait(
            [fs_task, port_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error(f"Events WS error: {e}")
    finally:
        logger.info("Events WS disconnected")
