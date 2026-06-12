"""Tool definitions: plain functions with schema introspection.

Tools are real async functions. Schemas are auto-generated from
type hints + docstrings via inspect. Keyword-only parameters are
never exposed to the LLM — they carry injected execution context:

  - Legacy tools use ``*, workspace: str`` (injected by execute_tool).
  - Context-aware tools use ``*, __context__: dict`` containing
    workspace, user_id, and model_id (injected by execute_tool).
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import os
import re
import uuid
from pathlib import Path
from typing import Callable, Optional, Pattern, get_type_hints
from cptr.env import CHAT_TOOL_COMMAND_MAX_CHARS, CHAT_TOOL_MAX_CHARS


# ── Background task state ───────────────────────────────────

_bg_tasks: dict[str, dict] = {}  # task_id → {proc, output, command, done}
_BG_TASK_LIMIT = 5


async def _collect_bg_output(task_id: str, proc: asyncio.subprocess.Process):
    """Collect output from a background process into the task buffer."""
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            task = _bg_tasks.get(task_id)
            if task:
                task["output"].extend(chunk)
                # Cap buffer at 256KB
                if len(task["output"]) > 256 * 1024:
                    task["output"] = task["output"][-256 * 1024 :]
    except Exception:
        pass
    finally:
        task = _bg_tasks.get(task_id)
        if task:
            task["done"] = True


# ── Helper ──────────────────────────────────────────────────


def _is_dotenv(path: Path) -> bool:
    """Return True if the path refers to a .env file (e.g. .env, .env.local)."""
    return path.name == ".env" or path.name.startswith(".env.")


_DOTENV_ERROR = "Error: access to .env files is not allowed for security reasons."


def _human_size(size: int) -> str:
    """Format byte size for display."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _truncate_output(text: str, max_chars: int = 80_000) -> str:
    """Truncate long output, keeping head and tail."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n... (truncated) ...\n\n" + text[-half:]


# ── Image support ───────────────────────────────────────────

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
}

_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB target for API payload

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


def _read_image_file(full: Path, path: str) -> str:
    """Read an image file and return a data URI string.

    If the file exceeds _IMAGE_MAX_BYTES, attempts to resize it down
    using Pillow.  Falls back to a text error if Pillow is unavailable
    and the file is too large.
    """
    import base64

    size = full.stat().st_size
    ext = full.suffix.lower()
    media_type = _IMAGE_MIME.get(ext, "image/png")
    data = full.read_bytes()

    if size > _IMAGE_MAX_BYTES:
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(data))
            # Progressively scale down until under limit
            # Use JPEG for lossy formats, PNG for lossless
            out_format = "JPEG" if ext in (".jpg", ".jpeg", ".bmp", ".tiff", ".tif") else "PNG"
            if out_format == "JPEG":
                media_type = "image/jpeg"
                # Convert RGBA to RGB for JPEG
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
            else:
                media_type = "image/png"

            scale = 0.8  # start at 80%
            for _ in range(10):
                new_w = int(img.width * scale)
                new_h = int(img.height * scale)
                if new_w < 100 or new_h < 100:
                    break
                resized = img.resize((new_w, new_h), Image.LANCZOS)
                buf = io.BytesIO()
                save_kwargs = {"quality": 85} if out_format == "JPEG" else {}
                resized.save(buf, format=out_format, **save_kwargs)
                if buf.tell() <= _IMAGE_MAX_BYTES:
                    data = buf.getvalue()
                    size = len(data)
                    break
                scale *= 0.7  # more aggressive on each pass
            else:
                return f"Error: image too large ({_human_size(full.stat().st_size)}) and could not be resized below 5MB."
        except ImportError:
            return (
                f"Error: image file is too large ({_human_size(size)}). "
                f"Install Pillow (`pip install Pillow`) to enable automatic resizing."
            )

    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{b64}"


# ── Tool functions ──────────────────────────────────────────


async def read_file(
    path: str,
    start_line: int = 0,
    end_line: int = 0,
    *,
    workspace: str,
) -> str:
    """Read file contents with optional line range. Lines are 1-indexed.
    Supports absolute paths for user-attached files.
    :param path: Path relative to workspace root, or absolute path for attached files.
    :param start_line: First line to read (1-indexed, 0 = from beginning).
    :param end_line: Last line to read (inclusive, 0 = to end of file).
    """
    full = _resolve_path(path, workspace)
    if _is_dotenv(full):
        return _DOTENV_ERROR
    if not full.is_file():
        return f"Error: file not found: {path}"

    # Image files: return base64 JSON instead of garbled text
    if full.suffix.lower() in IMAGE_EXTENSIONS:
        return await asyncio.to_thread(_read_image_file, full, path)

    def _read():
        size = full.stat().st_size
        if size > 500_000:
            return f"Error: file too large ({size} bytes, max 500KB)"

        lines = full.read_text(errors="replace").splitlines()
        total = len(lines)

        if start_line > 0 or end_line > 0:
            s = max(1, start_line) - 1  # Convert to 0-indexed
            e = min(total, end_line) if end_line > 0 else total
            selected = lines[s:e]
            numbered = [f"{i + s + 1}: {line}" for i, line in enumerate(selected)]
            header = f"File: {path} | Lines {s + 1}-{e} of {total}\n"
            return header + "\n".join(numbered)
        else:
            # Cap at 800 lines, show line numbers
            capped = lines[:800]
            numbered = [f"{i + 1}: {line}" for i, line in enumerate(capped)]
            header = f"File: {path} | Total lines: {total}"
            if total > 800:
                header += " (showing first 800)"
            return header + "\n" + "\n".join(numbered)

    return await asyncio.to_thread(_read)


async def list_directory(
    path: str = ".",
    recursive: bool = False,
    *,
    workspace: str,
) -> str:
    """List files and directories with metadata (sizes, child counts).
    :param path: Directory path relative to workspace root.
    :param recursive: Whether to list recursively.
    """
    full = _resolve_path(path, workspace)
    if not full.is_dir():
        return f"Error: not a directory: {path}"

    def _list():
        ignore = {
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            ".next",
            "build",
            "dist",
            ".cptr",
            ".svelte-kit",
        }
        entries = []

        if recursive:
            for root, dirs, files in os.walk(full):
                dirs[:] = sorted(d for d in dirs if d not in ignore)
                rel = Path(root).relative_to(full)
                for f in sorted(files):
                    fpath = Path(root) / f
                    try:
                        sz = fpath.stat().st_size
                    except OSError:
                        sz = 0
                    entries.append(f"{rel / f}  ({_human_size(sz)})")
        else:
            for item in sorted(full.iterdir()):
                if item.name in ignore:
                    continue
                if item.is_dir():
                    try:
                        count = sum(1 for _ in item.rglob("*") if _.is_file())
                    except (PermissionError, OSError):
                        count = 0
                    entries.append(f"{item.name}/  ({count} files)")
                else:
                    try:
                        sz = item.stat().st_size
                    except OSError:
                        sz = 0
                    entries.append(f"{item.name}  ({_human_size(sz)})")

        return "\n".join(entries) if entries else "(empty directory)"

    res = await asyncio.to_thread(_list)
    return _truncate_output(res, max_chars=CHAT_TOOL_MAX_CHARS)


async def search_files(
    query: str,
    path: str = ".",
    regex: bool = False,
    case_insensitive: bool = False,
    include: str = "",
    filenames_only: bool = False,
    *,
    workspace: str,
) -> str:
    """Search files for a pattern using ripgrep. Fast, respects .gitignore.
    :param query: Search pattern (plain text or regex).
    :param path: Directory to search in, relative to workspace.
    :param regex: Treat query as a regular expression.
    :param case_insensitive: Case-insensitive matching.
    :param include: Glob pattern to filter files (e.g. '*.py', '*.ts').
    :param filenames_only: Only return filenames, not matching lines.
    """
    full = _resolve_path(path, workspace)
    if not full.is_dir():
        return f"Error: not a directory: {path}"

    # Try ripgrep first
    try:
        res = await _search_rg(query, full, regex, case_insensitive, include, filenames_only)
    except FileNotFoundError:
        # ripgrep not installed, fall back to Python
        res = await _search_python(query, full, case_insensitive)

    return _truncate_output(res, max_chars=CHAT_TOOL_MAX_CHARS)


async def _search_rg(
    query: str, full: Path, regex: bool, case_insensitive: bool, include: str, filenames_only: bool
) -> str:
    """Search using ripgrep."""
    args = ["rg", "--no-heading", "--max-count=50", "--color=never"]
    # Never search .env files
    args.extend(["--glob", "!.env", "--glob", "!.env.*"])
    if not regex:
        args.append("--fixed-strings")
    if case_insensitive:
        args.append("--ignore-case")
    if filenames_only:
        args.append("--files-with-matches")
    else:
        args.append("--line-number")
    if include:
        for glob in include.split(","):
            glob = glob.strip()
            if glob:
                args.extend(["--glob", glob])

    args.extend(["--", query, str(full)])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    output = stdout.decode(errors="replace").strip()

    if proc.returncode == 1:
        return "No matches found."
    if proc.returncode > 1:
        err = stderr.decode(errors="replace").strip()
        raise FileNotFoundError(err) if "not found" in err.lower() else Exception(err)

    # Make paths relative
    prefix = str(full) + os.sep
    lines = output.splitlines()[:50]
    result = [line.replace(prefix, "") for line in lines]
    return "\n".join(result) if result else "No matches found."


async def _search_python(query: str, full: Path, case_insensitive: bool) -> str:
    """Fallback search using pure Python (when ripgrep not installed)."""

    def _read_text_for_search(fpath: Path) -> str | None:
        """Read a file for searching, skipping binary-looking files.

        Match ripgrep's default binary handling closely enough for the fallback:
        files containing NUL bytes are treated as binary and are not decoded or
        returned as replacement-character text.
        """
        try:
            data = fpath.read_bytes()
        except (OSError, PermissionError):
            return None
        if b"\0" in data:
            return None
        return data.decode(errors="replace")

    def _walk_and_search():
        results = []
        ignore = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        q = query.lower() if case_insensitive else query

        for root, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d not in ignore]
            for fname in files:
                fpath = Path(root) / fname
                if _is_dotenv(fpath):
                    continue
                text = _read_text_for_search(fpath)
                if text is None:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    target = line.lower() if case_insensitive else line
                    if q in target:
                        rel = fpath.relative_to(full)
                        results.append(f"{rel}:{i}: {line.strip()}")
                        if len(results) >= 50:
                            results.append("... (truncated at 50 matches)")
                            return "\n".join(results)

        return "\n".join(results) if results else "No matches found."

    return await asyncio.to_thread(_walk_and_search)


async def create_file(
    path: str = "",
    content: str = "",
    overwrite: bool = False,
    artifact_type: str = "",
    *,
    workspace: str,
) -> str:
    """Create a new file, or create an artifact for user review.
    When artifact_type is set, path is optional. The artifact is saved automatically.
    :param path: Path relative to workspace root (optional for artifacts).
    :param content: File contents to write.
    :param overwrite: Set to true to overwrite an existing file.
    :param artifact_type: Set to 'implementation_plan' to present a plan for user review before coding.
    """
    # Artifact-only: save to .cptr/artifacts/ (same location as create_artifact)
    if artifact_type and not path:
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        artifact_dir = Path(workspace) / ".cptr" / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{ts}_{artifact_type}.md"

        def _write_artifact():
            artifact_path.write_text(content)

        await asyncio.to_thread(_write_artifact)

        rel_path = str(artifact_path.relative_to(Path(workspace)))
        display_title = artifact_type.replace("_", " ").title()
        return json.dumps({
            "artifact_type": artifact_type,
            "title": display_title,
            "path": rel_path,
            "bytes": len(content),
        })

    if not path:
        return "Error: path is required when artifact_type is not set."

    full = _resolve_path(path, workspace)
    if _is_dotenv(full):
        return _DOTENV_ERROR
    if full.is_file() and not overwrite:
        return f"Error: file already exists: {path}. Use overwrite=true or edit_file to modify."

    def _write():
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    await asyncio.to_thread(_write)
    return f"Created {path} ({len(content)} bytes, {len(content.splitlines())} lines)"


async def create_artifact(
    content: str,
    artifact_type: str = "implementation_plan",
    title: str = "",
    *,
    workspace: str,
) -> str:
    """Create an artifact for user review. Use for implementation plans and analysis.
    :param content: Artifact content as markdown.
    :param artifact_type: Type of artifact, e.g. 'implementation_plan'.
    :param title: Display title for the artifact card.
    """
    from datetime import datetime, timezone

    artifact_type = artifact_type or "implementation_plan"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    artifact_dir = Path(workspace) / ".cptr" / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{ts}_{artifact_type}.md"

    def _write():
        artifact_path.write_text(content)

    await asyncio.to_thread(_write)

    display_title = title or artifact_type.replace("_", " ").title()
    rel_path = str(artifact_path.relative_to(Path(workspace)))
    return json.dumps({
        "artifact_type": artifact_type,
        "title": display_title,
        "path": rel_path,
        "bytes": len(content),
    })


async def write_file(path: str, content: str, *, workspace: str) -> str:
    """Write or create a file (full content). Prefer edit_file for modifications.
    :param path: Path relative to workspace root.
    :param content: File contents to write.
    """
    full = _resolve_path(path, workspace)
    if _is_dotenv(full):
        return _DOTENV_ERROR

    def _write():
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    await asyncio.to_thread(_write)
    return f"Wrote {len(content)} bytes to {path}"


async def edit_file(
    path: str,
    target: str,
    replacement: str,
    start_line: int = 0,
    end_line: int = 0,
    *,
    workspace: str,
) -> str:
    """Replace a specific text block in a file. Only provide the text that changes.
    :param path: Path relative to workspace root.
    :param target: Exact text to find and replace (must match file content exactly).
    :param replacement: Text to replace the target with.
    :param start_line: Narrow search to lines starting here (1-indexed, 0 = from start).
    :param end_line: Narrow search to lines ending here (0 = to end).
    """
    full = _resolve_path(path, workspace)
    if _is_dotenv(full):
        return _DOTENV_ERROR
    if not full.is_file():
        return f"Error: file not found: {path}"

    def _edit():
        content = full.read_text(errors="replace")

        if start_line > 0 or end_line > 0:
            lines = content.splitlines(keepends=True)
            total = len(lines)
            s = max(1, start_line) - 1
            e = min(total, end_line) if end_line > 0 else total
            region = "".join(lines[s:e])

            if target not in region:
                return f"Error: target text not found in lines {s + 1}-{e} of {path}"

            count = region.count(target)
            if count > 1:
                return (
                    f"Error: target text found {count} times in lines {s + 1}-{e}. "
                    f"Narrow the line range or use a more specific target."
                )

            new_region = region.replace(target, replacement, 1)
            new_content = "".join(lines[:s]) + new_region + "".join(lines[e:])
        else:
            count = content.count(target)
            if count == 0:
                return f"Error: target text not found in {path}"
            if count > 1:
                return (
                    f"Error: target text found {count} times in {path}. "
                    f"Use start_line/end_line to disambiguate."
                )
            new_content = content.replace(target, replacement, 1)

        full.write_text(new_content)
        return None  # success sentinel

    result = await asyncio.to_thread(_edit)
    if result is not None:
        return result

    target_lines = len(target.splitlines())
    replacement_lines = len(replacement.splitlines())
    return (
        f"Edited {path}: replaced {target_lines} lines with {replacement_lines} lines "
        f"({len(target)} chars → {len(replacement)} chars)"
    )


async def multi_edit_file(
    path: str,
    edits: str,
    *,
    workspace: str,
) -> str:
    """Apply multiple non-contiguous edits to a file in one operation.
    :param path: Path relative to workspace root.
    :param edits: JSON array of edit objects, each with 'target' and 'replacement' strings, and optional 'start_line'/'end_line' integers.
    """
    full = _resolve_path(path, workspace)
    if _is_dotenv(full):
        return _DOTENV_ERROR
    if not full.is_file():
        return f"Error: file not found: {path}"

    try:
        edit_list = json.loads(edits)
    except json.JSONDecodeError as e:
        return f"Error: invalid edits JSON: {e}"

    if not isinstance(edit_list, list) or not edit_list:
        return "Error: edits must be a non-empty JSON array"

    def _apply():
        content = full.read_text(errors="replace")
        applied = 0

        for i, edit in enumerate(edit_list):
            target = edit.get("target", "")
            replacement = edit.get("replacement", "")

            if not target:
                return f"Error: edit {i + 1} missing 'target'"

            if target not in content:
                return f"Error: target not found for edit {i + 1}: {target[:100]}..."

            count = content.count(target)
            if count > 1:
                return (
                    f"Error: edit {i + 1} target found {count} times. "
                    f"Each target must be unique in the file."
                )

            content = content.replace(target, replacement, 1)
            applied += 1

        full.write_text(content)
        return f"Applied {applied} edits to {path}"

    return await asyncio.to_thread(_apply)


async def run_command(
    command: str,
    cwd: str = ".",
    timeout: int = 30,
    background: bool = False,
    *,
    workspace: str,
) -> str:
    """Run a shell command. Use background=true for long-running processes.
    :param command: The shell command to execute.
    :param cwd: Working directory relative to workspace root.
    :param timeout: Timeout in seconds (max 300, ignored if background).
    :param background: Run in background and return a task_id for status checks.
    """
    work_dir = _resolve_path(cwd, workspace)
    if not work_dir.is_dir():
        return f"Error: not a directory: {cwd}"

    env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat"}

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir),
            env=env,
        )

        if background:
            active = sum(1 for t in _bg_tasks.values() if not t.get("done"))
            if active >= _BG_TASK_LIMIT:
                proc.kill()
                return (
                    f"Error: too many background tasks ({active}/{_BG_TASK_LIMIT}). Kill one first."
                )

            task_id = uuid.uuid4().hex[:8]
            _bg_tasks[task_id] = {
                "proc": proc,
                "output": bytearray(),
                "command": command,
                "done": False,
            }
            asyncio.create_task(_collect_bg_output(task_id, proc))
            return f"Background task started: {task_id}\nCommand: {command}\nUse check_task('{task_id}') to see output or kill_task('{task_id}') to stop it."

        timeout = min(max(timeout, 5), 300)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors="replace").strip()
        output = _truncate_output(output, max_chars=CHAT_TOOL_COMMAND_MAX_CHARS)

        if proc.returncode != 0:
            return f"Exit code {proc.returncode}\n{output}"
        return output or "(no output)"

    except asyncio.TimeoutError:
        proc.kill()
        return f"Error: command timed out after {timeout}s. Consider using background=true for long commands."
    except Exception as e:
        return f"Error: {e}"


async def check_task(task_id: str, *, workspace: str) -> str:
    """Check status and recent output of a background task.
    :param task_id: The task ID returned by run_command with background=true.
    """
    task = _bg_tasks.get(task_id)
    if not task:
        available = list(_bg_tasks.keys())
        return f"Error: no task with id '{task_id}'. Active tasks: {available or 'none'}"

    proc = task["proc"]
    output = task["output"].decode(errors="replace")
    output = _truncate_output(output, max_chars=CHAT_TOOL_COMMAND_MAX_CHARS)

    done = task.get("done", False) or proc.returncode is not None
    if done:
        status = f"exited (code {proc.returncode})"
    else:
        status = "running"

    return f"Task {task_id}: {status}\nCommand: {task['command']}\n---\n{output}"


async def kill_task(task_id: str, *, workspace: str) -> str:
    """Kill a running background task.
    :param task_id: The task ID to kill.
    """
    task = _bg_tasks.get(task_id)
    if not task:
        available = list(_bg_tasks.keys())
        return f"Error: no task with id '{task_id}'. Active tasks: {available or 'none'}"

    proc = task["proc"]
    if proc.returncode is not None:
        _bg_tasks.pop(task_id, None)
        return f"Task {task_id} already finished (code {proc.returncode})"

    try:
        proc.kill()
    except ProcessLookupError:
        pass
    _bg_tasks.pop(task_id, None)
    return f"Killed task {task_id}"


async def web_search(query: str, *, workspace: str) -> str:
    """Search the web for information. Returns summaries with source URLs.
    :param query: The search query.
    """
    # Defer to web module
    from cptr.utils.web import web_search_handler

    return await web_search_handler(query)


async def read_url(url: str, *, workspace: str) -> str:
    """Fetch content from a URL and return as text. Converts HTML to readable text.
    :param url: The URL to fetch.
    """
    from cptr.utils.web import read_url_handler

    return await read_url_handler(url)


# ── Path safety ──────────────────────────────────────────────


def _resolve_path(path: str, workspace: str) -> Path:
    """Resolve a path within the workspace or uploads dir. Rejects traversal."""
    from cptr.utils.storage import UPLOADS_DIR

    p = Path(path)
    # Allow absolute paths to the uploads directory (for user-attached files)
    if p.is_absolute():
        full = p.resolve()
        uploads = str(UPLOADS_DIR.resolve())
        ws = str(Path(workspace).resolve())
        if str(full).startswith(uploads) or str(full).startswith(ws):
            return full
        raise ValueError(f"Path outside allowed directories: {path}")

    # Relative paths resolve against workspace
    ws = Path(workspace).resolve()
    full = (ws / path).resolve()
    if not str(full).startswith(str(ws)):
        raise ValueError(f"Path traversal rejected: {path}")
    return full


async def create_automation(
    name: str,
    prompt: str,
    rrule: str,
    *,
    __context__: dict,
) -> str:
    """Create a scheduled automation that runs a prompt on a recurring or one-time schedule.
    The rrule parameter must be a valid iCalendar RRULE string. Common examples:
    - Every day at 9am: "DTSTART:20250101T090000\\nRRULE:FREQ=DAILY"
    - Every Monday at 8am: "DTSTART:20250106T080000\\nRRULE:FREQ=WEEKLY;BYDAY=MO"
    - Every hour: "RRULE:FREQ=HOURLY;INTERVAL=1"
    - Once at a specific time: "DTSTART:20250415T140000\\nRRULE:FREQ=DAILY;COUNT=1"
    - First day of every month: "DTSTART:20250101T090000\\nRRULE:FREQ=MONTHLY;BYMONTHDAY=1"
    :param name: A short descriptive name for the automation.
    :param prompt: The instructions/prompt to execute on each run.
    :param rrule: An iCalendar RRULE string defining the schedule.
    """
    workspace = __context__["workspace"]
    user_id = __context__["user_id"]
    model_id = __context__.get("model_id", "")

    try:
        import time
        from cptr.models.automations import Automation as AutomationModel
        from cptr.utils.automations import next_run_ns, next_n_runs_ns, validate_rrule

        # Validate RRULE
        try:
            validate_rrule(rrule)
        except ValueError as e:
            return json.dumps({"error": f"Invalid schedule: {e}"})

        if not model_id:
            return json.dumps({"error": "Could not detect model from current chat context."})

        now_ns = int(time.time() * 1_000_000_000)
        nxt = next_run_ns(rrule)

        automation = await AutomationModel.create(
            user_id=user_id,
            name=name,
            prompt=prompt,
            model_id=model_id,
            workspace=workspace,
            rrule=rrule,
            next_run_at=nxt,
            is_active=True,
            created_at=now_ns,
        )
        return json.dumps({
            "status": "success",
            "id": automation.id,
            "name": automation.name,
            "model_id": automation.model_id,
            "is_active": automation.is_active,
            "next_runs": next_n_runs_ns(rrule),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def list_automations(
    status: str = "",
    count: int = 10,
    *,
    __context__: dict,
) -> str:
    """List scheduled automations for the current workspace.
    :param status: Filter by status: "active", "paused", or empty for all.
    :param count: Maximum number of automations to return (default: 10).
    """
    workspace = __context__["workspace"]
    user_id = __context__["user_id"]

    try:
        from cptr.models.automations import Automation as AutomationModel
        from cptr.utils.automations import next_n_runs_ns

        items, total = await AutomationModel.get_by_workspace(
            user_id=user_id,
            workspace=workspace or None,
            status=status or None,
            limit=count,
        )
        automations = []
        for item in items:
            automations.append({
                "id": item.id,
                "name": item.name,
                "prompt_snippet": item.prompt[:100] + ("..." if len(item.prompt) > 100 else ""),
                "model_id": item.model_id,
                "rrule": item.rrule,
                "is_active": item.is_active,
                "last_run_at": item.last_run_at,
                "next_runs": next_n_runs_ns(item.rrule),
            })
        return json.dumps({"automations": automations, "total": total})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def update_automation(
    automation_id: str,
    name: str = "",
    prompt: str = "",
    rrule: str = "",
    model_id: str = "",
    *,
    __context__: dict,
) -> str:
    """Update an existing automation. Only provided fields are changed.
    :param automation_id: The ID of the automation to update.
    :param name: New name (optional).
    :param prompt: New prompt/instructions (optional).
    :param rrule: New iCalendar RRULE schedule string (optional).
    :param model_id: New model ID (optional).
    """
    try:
        import time
        from cptr.models.automations import Automation as AutomationModel
        from cptr.utils.automations import next_run_ns, next_n_runs_ns, validate_rrule

        automation = await AutomationModel.get_by_id(automation_id)
        if not automation:
            return json.dumps({"error": "Automation not found"})

        kwargs = {}
        if name:
            kwargs["name"] = name
        if prompt:
            kwargs["prompt"] = prompt
        if model_id:
            kwargs["model_id"] = model_id
        if rrule:
            try:
                validate_rrule(rrule)
            except ValueError as e:
                return json.dumps({"error": f"Invalid schedule: {e}"})
            kwargs["rrule"] = rrule
            kwargs["next_run_at"] = next_run_ns(rrule)

        if not kwargs:
            return json.dumps({"error": "No fields to update"})

        now_ns = int(time.time() * 1_000_000_000)
        success = await AutomationModel.update_by_id(automation_id, updated_at=now_ns, **kwargs)
        if not success:
            return json.dumps({"error": "Failed to update automation"})

        final_rrule = rrule or automation.rrule
        return json.dumps({
            "status": "success",
            "id": automation_id,
            "updated_fields": list(kwargs.keys()),
            "next_runs": next_n_runs_ns(final_rrule),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def toggle_automation(
    automation_id: str,
    *,
    __context__: dict,
) -> str:
    """Pause or resume a scheduled automation. If active, it will be paused. If paused, it will be resumed.
    :param automation_id: The ID of the automation to toggle.
    """
    try:
        from cptr.models.automations import Automation as AutomationModel
        from cptr.utils.automations import next_run_ns

        automation = await AutomationModel.get_by_id(automation_id)
        if not automation:
            return json.dumps({"error": "Automation not found"})

        nxt = next_run_ns(automation.rrule) if not automation.is_active else None
        toggled = await AutomationModel.toggle(automation_id, next_run_at=nxt)
        if not toggled:
            return json.dumps({"error": "Failed to toggle automation"})

        return json.dumps({
            "status": "success",
            "id": toggled.id,
            "name": toggled.name,
            "is_active": toggled.is_active,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def delete_automation(
    automation_id: str,
    *,
    __context__: dict,
) -> str:
    """Delete a scheduled automation and all its run history.
    :param automation_id: The ID of the automation to delete.
    """
    try:
        from cptr.models.automations import Automation as AutomationModel

        automation = await AutomationModel.get_by_id(automation_id)
        if not automation:
            return json.dumps({"error": "Automation not found"})

        name = automation.name
        success = await AutomationModel.delete(automation_id)
        if not success:
            return json.dumps({"error": "Failed to delete automation"})

        return json.dumps({"status": "success", "message": f'Automation "{name}" deleted'})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Skill tools ─────────────────────────────────────────────

# Track activated skills per session (cleared on import)
_activated_skills: set[str] = set()


async def view_skill(
    skill_name: str,
    *,
    workspace: str,
) -> str:
    """Load the full instructions and resource listing for an available skill.
    :param skill_name: The name of the skill to load (from the <available_skills> catalog).
    """
    from cptr.utils.skills import load_skill, format_skill_content

    # Deduplication: if already activated, return short notice
    if skill_name in _activated_skills:
        return f"Skill '{skill_name}' is already loaded in this session. Refer to the existing <skill_content> above."

    skill = load_skill(workspace, skill_name)
    if not skill:
        return f"Error: skill '{skill_name}' not found. Check <available_skills> for valid names."

    _activated_skills.add(skill_name)
    return format_skill_content(skill)


# ── Browser tools ────────────────────────────────────────────


async def _get_browser_config() -> dict:
    """Read browser config from DB."""
    try:
        from cptr.models import Config

        return {
            "enabled": await Config.get("browser.enabled") or False,
            "provider": await Config.get("browser.provider") or "local",
            "cdp_url": await Config.get("browser.cdp_url") or "http://localhost:9222",
            "auto_launch": await Config.get("browser.auto_launch") if await Config.get("browser.auto_launch") is not None else True,
            "session_timeout": int(await Config.get("browser.session_timeout_minutes") or 10),
            "firecrawl_api_key": await Config.get("browser.firecrawl_api_key") or "",
            "firecrawl_base_url": await Config.get("browser.firecrawl_base_url") or "https://api.firecrawl.dev",
            "browser_use_api_key": await Config.get("browser.browser_use_api_key") or "",
            "browser_use_base_url": await Config.get("browser.browser_use_base_url") or "https://api.browser-use.com",
        }
    except Exception:
        return {"enabled": False, "provider": "local"}


async def _get_cdp_session(chat_id: str) -> "CDPClient":
    """Get or create a CDP session for the current chat."""
    cfg = await _get_browser_config()
    cdp_url = cfg["cdp_url"]

    if cfg.get("auto_launch", True):
        from cptr.utils.browser.launcher import ensure_browser

        cdp_url = await ensure_browser(port=int(cdp_url.split(":")[-1]))

    from cptr.utils.browser.session import session_manager

    session_manager.set_timeout(cfg.get("session_timeout", 10))
    return await session_manager.get_or_create(chat_id, cdp_url)


async def browser_navigate(url: str, *, __context__: dict) -> str:
    """Navigate to a URL in the browser. Returns the page title and status.
    :param url: The URL to navigate to.
    """
    cfg = await _get_browser_config()
    provider = cfg.get("provider", "local")

    if provider == "firecrawl":
        key = cfg.get("firecrawl_api_key", "")
        if not key:
            return "Error: Firecrawl API key not configured. Set it in Settings > Browser."
        from cptr.utils.browser.firecrawl import scrape

        content = await scrape(url, key, cfg.get("firecrawl_base_url", ""))
        return f"Navigated to {url} (via Firecrawl)\n\n{content}"

    if provider == "browser_use":
        key = cfg.get("browser_use_api_key", "")
        if not key:
            return "Error: Browser-Use API key not configured. Set it in Settings > Browser."
        from cptr.utils.browser.browser_use import browse

        result = await browse(f"Navigate to {url} and describe what you see", key, cfg.get("browser_use_base_url", ""))
        return f"Navigated to {url} (via Browser-Use)\n\n{result}"

    # Local CDP
    chat_id = __context__.get("chat_id", "default")
    client = await _get_cdp_session(chat_id)
    result = await client.navigate(url)
    return f"Navigated to {url}\nTitle: {result.get('title', '')}"


async def browser_snapshot(*, __context__: dict) -> str:
    """Get the current page content. For local browser, returns an accessibility tree with ref IDs (@e1, @e2, etc.) that can be used with browser_click and browser_type. For cloud providers, returns page content as text."""
    cfg = await _get_browser_config()
    provider = cfg.get("provider", "local")

    if provider in ("firecrawl", "browser_use"):
        return "Snapshot is only meaningful after browser_navigate. The navigate result already contains the page content."

    chat_id = __context__.get("chat_id", "default")
    client = await _get_cdp_session(chat_id)
    return await client.snapshot()


async def browser_click(ref: str, *, __context__: dict) -> str:
    """Click an element on the page identified by its ref ID from the snapshot (e.g. @e1).
    :param ref: The ref ID of the element to click (e.g. @e1, @e5).
    """
    cfg = await _get_browser_config()
    if cfg.get("provider", "local") != "local":
        return "Error: browser_click requires Local CDP provider. Cloud providers (Firecrawl, Browser-Use) don't support interactive browsing. Switch to Local CDP in Settings > Browser."

    chat_id = __context__.get("chat_id", "default")
    client = await _get_cdp_session(chat_id)
    await client.click(ref)
    # Return updated snapshot so the AI sees the result
    return await client.snapshot()


async def browser_type(ref: str, text: str, *, __context__: dict) -> str:
    """Type text into an input element identified by its ref ID from the snapshot.
    :param ref: The ref ID of the input element (e.g. @e3).
    :param text: The text to type.
    """
    cfg = await _get_browser_config()
    if cfg.get("provider", "local") != "local":
        return "Error: browser_type requires Local CDP provider. Switch to Local CDP in Settings > Browser."

    chat_id = __context__.get("chat_id", "default")
    client = await _get_cdp_session(chat_id)
    await client.type_text(ref, text)
    return await client.snapshot()


async def browser_screenshot(*, __context__: dict) -> str:
    """Take a screenshot of the current browser page. Saves the image to the workspace.
    """
    cfg = await _get_browser_config()
    if cfg.get("provider", "local") != "local":
        return "Error: browser_screenshot requires Local CDP provider."

    chat_id = __context__.get("chat_id", "default")
    client = await _get_cdp_session(chat_id)
    png_bytes = await client.screenshot()

    # Save to workspace
    workspace = __context__.get("workspace", ".")
    screenshots_dir = Path(workspace) / ".cptr" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    import time

    filename = f"screenshot_{int(time.time())}.png"
    filepath = screenshots_dir / filename
    filepath.write_bytes(png_bytes)

    return f"Screenshot saved: {filepath}"


async def browser_evaluate(javascript: str, *, __context__: dict) -> str:
    """Execute JavaScript in the browser page and return the result.
    :param javascript: The JavaScript expression to evaluate.
    """
    cfg = await _get_browser_config()
    if cfg.get("provider", "local") != "local":
        return "Error: browser_evaluate requires Local CDP provider."

    chat_id = __context__.get("chat_id", "default")
    client = await _get_cdp_session(chat_id)
    return await client.evaluate(javascript)


# ── Registry ────────────────────────────────────────────────

TOOLS: dict[str, dict] = {
    # Read-only (auto-approve)
    "read_file": {"fn": read_file, "auto": True},
    "list_directory": {"fn": list_directory, "auto": True},
    "search_files": {"fn": search_files, "auto": True},
    "check_task": {"fn": check_task, "auto": True},
    "web_search": {"fn": web_search, "auto": True},
    "read_url": {"fn": read_url, "auto": True},
    "list_automations": {"fn": list_automations, "auto": True},
    "view_skill": {"fn": view_skill, "auto": True},
    # Write / mutate (require approval unless auto_approve_all)
    "create_file": {"fn": create_file, "auto": False},
    "edit_file": {"fn": edit_file, "auto": False},
    "multi_edit_file": {"fn": multi_edit_file, "auto": False},
    "write_file": {"fn": write_file, "auto": False},
    "run_command": {"fn": run_command, "auto": False},
    "kill_task": {"fn": kill_task, "auto": False},
    "create_automation": {"fn": create_automation, "auto": False},
    "update_automation": {"fn": update_automation, "auto": False},
    "toggle_automation": {"fn": toggle_automation, "auto": False},
    "delete_automation": {"fn": delete_automation, "auto": False},
}

# Browser tools — registered conditionally based on browser.enabled config
BROWSER_TOOLS: dict[str, dict] = {
    "browser_navigate": {"fn": browser_navigate, "auto": False},
    "browser_snapshot": {"fn": browser_snapshot, "auto": True},
    "browser_click": {"fn": browser_click, "auto": False},
    "browser_type": {"fn": browser_type, "auto": False},
    "browser_screenshot": {"fn": browser_screenshot, "auto": True},
    "browser_evaluate": {"fn": browser_evaluate, "auto": False},
}


# ── Schema from function signature ──────────────────────────

_TYPE_MAP = {str: "string", int: "integer", bool: "boolean", float: "number"}


def _parse_param_descriptions(docstring: str) -> dict[str, str]:
    """Extract :param name: description lines from docstring."""
    descs: dict[str, str] = {}
    if not docstring:
        return descs
    for line in docstring.splitlines():
        line = line.strip()
        if line.startswith(":param "):
            rest = line[7:]
            if ":" in rest:
                name, desc = rest.split(":", 1)
                descs[name.strip()] = desc.strip()
    return descs


def _fn_to_schema(name: str, fn) -> dict:
    """Introspect function → {name, description, parameters} for LLM."""
    doc = inspect.getdoc(fn) or ""
    description = doc.split("\n")[0]
    param_descs = _parse_param_descriptions(doc)
    hints = get_type_hints(fn)
    sig = inspect.signature(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        # Skip injected context params (keyword-only, never exposed to LLM)
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            continue
        ptype = _TYPE_MAP.get(hints.get(pname), "string")  # type: ignore[arg-type]
        prop: dict = {"type": ptype}
        if pname in param_descs:
            prop["description"] = param_descs[pname]
        properties[pname] = prop
        # Positional with no default → required
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


async def get_tool_list() -> list[dict]:
    """Return tool schemas for the LLM.

    Automatically includes browser tools when browser.enabled is true in config.
    """
    tools = dict(TOOLS)
    try:
        from cptr.models import Config

        if (await Config.get("browser.enabled")) in (True, "true", "1"):
            tools.update(BROWSER_TOOLS)
    except Exception:
        pass
    return [_fn_to_schema(name, t["fn"]) for name, t in tools.items()]


async def execute_tool(name: str, args: dict, __context__: dict) -> str:
    """Execute a tool by name, injecting execution context."""
    info = TOOLS.get(name) or BROWSER_TOOLS.get(name)
    if not info:
        return f"Error: unknown tool: {name}"
    fn = info["fn"]
    try:
        sig = inspect.signature(fn)
        if "__context__" in sig.parameters:
            return await fn(**args, __context__=__context__)
        else:
            # Legacy tools: inject workspace directly
            return await fn(**args, workspace=__context__["workspace"])
    except Exception as e:
        return f"Error executing {name}: {e}"
