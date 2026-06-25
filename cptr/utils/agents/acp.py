"""Small ACP JSON-RPC client for Cursor and Grok agents."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from typing import Any, AsyncIterator


class AcpClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: str,
        env: dict[str, str],
        auth_method_id: str,
        client_capabilities: dict[str, Any] | None = None,
        resume_session_id: str | None = None,
        auto_approve_permissions: bool = False,
    ) -> None:
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        self.auth_method_id = auth_method_id
        self.client_capabilities = client_capabilities or {}
        self.resume_session_id = resume_session_id
        self.auto_approve_permissions = auto_approve_permissions
        self.proc: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.next_id = 1
        self.session_id: str | None = None
        self.setup_result: dict[str, Any] = {}
        self.model_config_id: str | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd or os.getcwd(),
            env=self.env,
        )
        self.reader_task = asyncio.create_task(self._reader_loop())
        self.stderr_task = asyncio.create_task(self._stderr_loop())
        await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                    **self.client_capabilities,
                },
                "clientInfo": {"name": "cptr", "version": "0"},
            },
        )
        await self.request("authenticate", {"methodId": self.auth_method_id})
        await self._open_session()

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        for task in (self.reader_task, self.stderr_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        for future in self.pending.values():
            if not future.done():
                future.cancel()
        self.pending.clear()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response = await future
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def prompt(self, text: str) -> dict[str, Any]:
        if not self.session_id:
            raise RuntimeError("ACP session has not started")
        return await self.request(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
        )

    async def cancel(self) -> None:
        if self.session_id:
            with suppress(Exception):
                await self.notify("session/cancel", {"sessionId": self.session_id})

    async def set_model(self, model: str) -> None:
        if not self.session_id:
            return
        if self.model_config_id:
            with suppress(Exception):
                await self.request(
                    "session/set_config_option",
                    {"sessionId": self.session_id, "configId": self.model_config_id, "value": model},
                )
                return
        with suppress(Exception):
            await self.request("session/set_model", {"sessionId": self.session_id, "modelId": model})

    async def _open_session(self) -> None:
        setup: dict[str, Any] | None = None
        if self.resume_session_id:
            with suppress(Exception):
                setup = await self.request(
                    "session/load",
                    {"sessionId": self.resume_session_id, "cwd": self.cwd, "mcpServers": []},
                )
                self.session_id = self.resume_session_id
        if setup is None:
            setup = await self.request("session/new", {"cwd": self.cwd, "mcpServers": []})
            session_id = setup.get("sessionId")
            self.session_id = session_id if isinstance(session_id, str) else None
        if not self.session_id:
            raise RuntimeError("ACP did not return a session id")
        self.setup_result = setup
        self.model_config_id = _extract_model_config_id(setup)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        data = json.dumps(payload, separators=(",", ":")).encode()
        framed = b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data
        self.proc.stdin.write(framed)
        await self.proc.stdin.drain()

    async def _reader_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        buffer = b""
        while True:
            chunk = await self.proc.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            while True:
                extracted = _extract_json_message(buffer)
                if extracted is None:
                    break
                message, buffer = extracted
                await self._handle_message(message)

    async def _stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while await self.proc.stderr.readline():
            pass

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message) and "method" not in message:
            future = self.pending.pop(int(message["id"]), None)
            if future and not future.done():
                future.set_result(message)
            return

        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" in message and method == "session/request_permission":
            await self._reply_permission(int(message["id"]), params)
            return
        await self.events.put(message)

    async def _reply_permission(self, request_id: int, params: dict[str, Any]) -> None:
        option_id = None
        if self.auto_approve_permissions:
            option_id = _select_permission_option(params, "allow_always") or _select_permission_option(
                params, "allow_once"
            )
        outcome = (
            {"outcome": {"outcome": "selected", "optionId": option_id}}
            if option_id
            else {"outcome": {"outcome": "cancelled"}}
        )
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": outcome})


def _extract_json_message(buffer: bytes) -> tuple[dict[str, Any], bytes] | None:
    stripped = buffer.lstrip()
    skipped = len(buffer) - len(stripped)
    if skipped:
        buffer = stripped

    lower = buffer[:32].lower()
    if lower.startswith(b"content-length:"):
        header_end = buffer.find(b"\r\n\r\n")
        sep_len = 4
        if header_end < 0:
            header_end = buffer.find(b"\n\n")
            sep_len = 2
        if header_end < 0:
            return None
        header = buffer[:header_end].decode(errors="replace")
        length = None
        for line in header.splitlines():
            if line.lower().startswith("content-length:"):
                with suppress(ValueError):
                    length = int(line.split(":", 1)[1].strip())
        if length is None:
            raise RuntimeError("ACP message missing Content-Length")
        start = header_end + sep_len
        end = start + length
        if len(buffer) < end:
            return None
        return json.loads(buffer[start:end].decode()), buffer[end:]

    line_end = buffer.find(b"\n")
    if line_end < 0:
        return None
    line = buffer[:line_end].strip()
    if not line:
        return {}, buffer[line_end + 1 :]
    return json.loads(line.decode()), buffer[line_end + 1 :]


def _extract_model_config_id(setup: dict[str, Any]) -> str | None:
    options = setup.get("configOptions")
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and option.get("category") == "model":
            option_id = option.get("id")
            if isinstance(option_id, str) and option_id.strip():
                return option_id.strip()
    return None


def _select_permission_option(params: dict[str, Any], kind: str) -> str | None:
    options = params.get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and option.get("kind") == kind:
            option_id = option.get("optionId")
            if isinstance(option_id, str) and option_id.strip():
                return option_id.strip()
    return None


def acp_text_from_update(params: dict[str, Any]) -> str | None:
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") != "agent_message_chunk":
        return None
    content = update.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        text = content.get("text")
        return text if isinstance(text, str) and text else None
    return None


def acp_models_from_setup(setup: dict[str, Any]) -> list[str]:
    models = setup.get("models")
    if not isinstance(models, dict):
        return []
    available = models.get("availableModels")
    if not isinstance(available, list):
        return []
    result = []
    for item in available:
        if isinstance(item, dict):
            model = item.get("modelId")
            if isinstance(model, str) and model.strip():
                result.append(model.strip())
    return result


async def acp_event_stream(client: AcpClient) -> AsyncIterator[dict[str, Any]]:
    while True:
        yield await client.events.get()
