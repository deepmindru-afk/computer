"""Gateway: expose cptr workspaces as OpenAI-compatible models.

GET  /v1/models           - list workspaces in OpenAI model-list format
POST /v1/chat/completions - run the agentic loop on a workspace, stream SSE

Any OpenAI-compatible client (Open WebUI, curl, Python SDK) can connect
to cptr and use each workspace as a "model" that can read files, edit code,
run commands, and use skills.

Session mapping: if the caller sends X-Chat-Id or X-OpenWebUI-Chat-Id,
the same cptr chat is reused across turns. Otherwise each request creates
a fresh chat.

Auth: Bearer token validated against hashed keys in the Config store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from cptr.models import Chat, ChatMessage, Config
from cptr.models.workspaces import Workspace
from cptr.utils.config import now_ms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["gateway"])

# Headers that clients can send with the chat ID.
CHAT_ID_HEADER = "X-Chat-Id"
OWUI_CHAT_ID_HEADER = "X-OpenWebUI-Chat-Id"


# ── API key management ───────────────────────────────────────


async def _get_api_keys() -> list[dict]:
    """Load API keys from Config store."""
    keys = await Config.get("api_keys")
    return keys if isinstance(keys, list) else []


async def _save_api_keys(keys: list[dict]) -> None:
    await Config.upsert({"api_keys": keys})


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _validate_bearer(request: Request) -> str:
    """Validate Bearer token from Authorization header.  Returns user_id."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")

    token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Empty bearer token")

    token_hash = _hash_key(token)
    keys = await _get_api_keys()
    for key in keys:
        if key.get("key_hash") == token_hash:
            user_id = key.get("user_id")
            if not user_id:
                raise HTTPException(500, "API key has no user_id")
            return user_id

    raise HTTPException(401, "Invalid API key")


# ── GET /v1/models ───────────────────────────────────────────


@router.get("/models")
async def list_models(request: Request):
    """List workspaces as OpenAI-format models."""
    user_id = await _validate_bearer(request)
    workspaces = await Workspace.get_by_user(user_id)

    # Disambiguate basenames
    name_counts: dict[str, int] = {}
    for ws in workspaces:
        name = Path(ws.path).name
        name_counts[name] = name_counts.get(name, 0) + 1

    seen: dict[str, int] = {}
    models = []
    for ws in workspaces:
        basename = Path(ws.path).name
        if name_counts[basename] > 1:
            seen[basename] = seen.get(basename, 0) + 1
            model_id = f"cptr/{basename}-{seen[basename]}"
        else:
            model_id = f"cptr/{basename}"

        models.append(
            {
                "id": model_id,
                "object": "model",
                "created": ws.created_at or int(time.time()),
                "owned_by": "cptr",
                "name": f"{ws.name} - {ws.path}",
                # Extra metadata for cptr
                "cptr_workspace": ws.path,
            }
        )

    return {"object": "list", "data": models}


# ── POST /v1/chat/completions ────────────────────────────────


class ChatCompletionMessage(BaseModel):
    role: str
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    stream: bool = True
    # Other OpenAI params are accepted but ignored
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


@router.post("/chat/completions")
async def create_chat_completion(request: Request, body: ChatCompletionRequest):
    """Run the cptr agentic loop and stream results as OpenAI SSE."""
    user_id = await _validate_bearer(request)

    # 1. Resolve model → workspace path
    workspace = await _resolve_workspace(user_id, body.model)

    # 2. Resolve the underlying LLM connection for this workspace
    connection, bare_model, model_id = await _resolve_model_connection(workspace)

    # 3. Session mapping: find or create a cptr chat
    client_chat_id = request.headers.get(CHAT_ID_HEADER) or request.headers.get(OWUI_CHAT_ID_HEADER)
    chat_id = await _find_or_create_chat(
        user_id, workspace, client_chat_id, body.messages, model_id
    )

    # 4. Create user + assistant messages
    user_content = ""
    if body.messages:
        last_user = next(
            (m for m in reversed(body.messages) if m.get("role") == "user"),
            None,
        )
        user_content = (last_user.get("content", "") if last_user else "") or ""

    # Get parent (latest message in the chat)
    chat = await Chat.get_by_id(chat_id)
    parent_id = chat.current_message_id if chat else None

    user_msg = await ChatMessage.create(
        chat_id=chat_id,
        role="user",
        content=user_content,
        parent_id=parent_id,
        created_at=now_ms(),
    )

    assistant_msg = await ChatMessage.create(
        chat_id=chat_id,
        role="assistant",
        content="",
        parent_id=user_msg.id,
        model=model_id,
        done=False,
        created_at=now_ms(),
    )
    await Chat.update_current_message(chat_id, assistant_msg.id, now_ms())

    # Export JSON so cptr sidebar sees it immediately
    from cptr.utils.chat_export import export_chat_to_file

    await export_chat_to_file(chat_id)

    # 5. Create output queue and start the agentic loop
    output_queue: asyncio.Queue = asyncio.Queue()

    from cptr.utils.chat_task import start_task

    start_task(
        message_id=assistant_msg.id,
        chat_id=chat_id,
        user_id=user_id,
        connection=connection,
        workspace=workspace,
        model=bare_model,
        output_queue=output_queue,
    )

    # 6. Stream SSE response
    completion_id = f"chatcmpl-{assistant_msg.id[:24]}"
    created = int(time.time())

    if body.stream:
        return StreamingResponse(
            _sse_generator(output_queue, completion_id, created, body.model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # Non-streaming: collect all text, return as single response
        full_text = await _collect_response(output_queue)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": body.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": full_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }


# ── SSE generator ────────────────────────────────────────────


async def _sse_generator(
    queue: asyncio.Queue,
    completion_id: str,
    created: int,
    model: str,
):
    """Translate queue events → OpenAI SSE chunks."""

    def _chunk(delta: dict, finish_reason: str | None = None) -> str:
        data = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        return f"data: {json.dumps(data)}\n\n"

    # Initial chunk with role
    yield _chunk({"role": "assistant", "content": ""})

    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=300)
        except asyncio.TimeoutError:
            # Safety timeout, end the stream
            yield _chunk({}, "stop")
            yield "data: [DONE]\n\n"
            return

        if event is None:
            # Sentinel: stream complete
            yield _chunk({}, "stop")
            yield "data: [DONE]\n\n"
            return

        event_type = event.get("type")

        if event_type == "delta":
            content = event.get("content", "")
            if content:
                yield _chunk({"content": content})

        elif event_type == "done":
            finish = event.get("finish_reason", "stop")
            yield _chunk({}, finish)
            yield "data: [DONE]\n\n"
            return

        elif event_type == "error":
            # Stream the error as content, then stop
            error_msg = event.get("message", "Internal error")
            yield _chunk({"content": f"\n\n> **Error:** {error_msg}"})
            yield _chunk({}, "stop")
            yield "data: [DONE]\n\n"
            return

        # Other event types (tool calls, etc.) are silently consumed,
        # they're persisted in cptr's DB and visible in its sidebar.


async def _collect_response(queue: asyncio.Queue) -> str:
    """Collect all text from the queue for non-streaming mode."""
    parts = []
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=300)
        except asyncio.TimeoutError:
            break
        if event is None:
            break
        if event.get("type") == "delta":
            parts.append(event.get("content", ""))
        elif event.get("type") in ("done", "error"):
            if event.get("type") == "error":
                parts.append(f"\n\n> **Error:** {event.get('message', '')}")
            break
    return "".join(parts)


# ── Workspace resolution ─────────────────────────────────────


async def _resolve_workspace(user_id: str, model_id: str) -> str:
    """Resolve 'cptr/basename' → workspace filesystem path."""
    if not model_id.startswith("cptr/"):
        raise HTTPException(400, f"Invalid model ID: {model_id}")

    target = model_id[5:]  # strip "cptr/"
    workspaces = await Workspace.get_by_user(user_id)

    # Exact basename match
    for ws in workspaces:
        if Path(ws.path).name == target:
            return ws.path

    # Disambiguated match (e.g., "my-project-2")
    name_counts: dict[str, int] = {}
    for ws in workspaces:
        name = Path(ws.path).name
        name_counts[name] = name_counts.get(name, 0) + 1

    seen: dict[str, int] = {}
    for ws in workspaces:
        basename = Path(ws.path).name
        if name_counts[basename] > 1:
            seen[basename] = seen.get(basename, 0) + 1
            if f"{basename}-{seen[basename]}" == target:
                return ws.path

    raise HTTPException(404, f"Workspace not found for model: {model_id}")


# ── Model connection resolution ──────────────────────────────


async def _resolve_model_connection(workspace: str) -> tuple[dict, str, str]:
    """Find a model connection to use for the agentic loop.

    Priority:
      1. Workspace-specific model override (.cptr/model)
      2. First enabled connection's first model

    Returns (connection_dict, bare_model, full_model_id).
    """
    from cptr.routers.chat import _resolve_connection

    # Check for workspace-specific model override
    model_file = Path(workspace) / ".cptr" / "model"
    model_override = None
    if model_file.is_file():
        model_override = model_file.read_text().strip()

    if model_override:
        try:
            connection, bare = await _resolve_connection(model_override)
            return connection, bare, model_override
        except Exception:
            logger.warning(
                "[openai-compat] Workspace model override '%s' not found, falling back",
                model_override,
            )

    # Fall back to the first enabled connection + its first model
    connections = await Config.get("chat.connections") or []
    for conn in connections:
        if not conn.get("enabled", True):
            continue
        model_ids = conn.get("data", {}).get("models")
        if model_ids:
            prefix = (conn.get("prefix_id") or "").strip()
            bare = model_ids[0]
            full = f"{prefix}/{bare}" if prefix else bare
            return conn, bare, full

    raise HTTPException(503, "No model connections configured. Add a connection in cptr settings.")


# ── Session mapping ──────────────────────────────────────────


async def _find_or_create_chat(
    user_id: str,
    workspace: str,
    client_chat_id: str | None,
    messages: list[dict],
    model_id: str,
) -> str:
    """Find an existing cptr chat for this client conversation, or create one."""

    if client_chat_id:
        # Search for a chat with this client chat ID in metadata
        from cptr.utils.db import get_db
        from sqlalchemy import select

        async with await get_db() as db:
            result = await db.execute(select(Chat).where(Chat.user_id == user_id))
            for chat in result.scalars():
                meta = chat.meta or {}
                if (
                    meta.get("client_chat_id") == client_chat_id
                    or meta.get("owui_chat_id") == client_chat_id
                ):
                    return chat.id

    # Create a new chat
    title = "Open WebUI Chat"
    if messages:
        first_user = next(
            (m.get("content", "")[:50] for m in messages if m.get("role") == "user"),
            None,
        )
        if first_user:
            title = first_user.strip() or title

    meta = {
        "workspace": workspace,
        "params": {"tool_approval_mode": "full"},
    }
    if client_chat_id:
        meta["client_chat_id"] = client_chat_id
        meta["owui_chat_id"] = client_chat_id

    chat = await Chat.create(
        user_id=user_id,
        title=title[:100],
        meta=meta,
        created_at=now_ms(),
    )

    # Ensure .cptr/chats/ dir exists and create marker file
    chats_dir = Path(workspace) / ".cptr" / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    (chats_dir / f"{chat.id}.json").write_text("{}")

    return chat.id


# ── API key admin endpoint ───────────────────────────────────


class CreateApiKeyRequest(BaseModel):
    name: str = "default"


@router.post("/keys")
async def create_api_key(request: Request, body: CreateApiKeyRequest):
    """Create a new API key (requires cookie auth, admin only)."""
    from cptr.utils.config import check_access

    client_host = request.client.host if request.client else "127.0.0.1"
    jwt_token = request.cookies.get("cptr_session")
    auth = check_access(client_host=client_host, jwt_token=jwt_token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "Admin authentication required")

    raw = f"sk-cptr-{secrets.token_urlsafe(32)}"
    entry = {
        "id": str(uuid.uuid4()),
        "key_hash": _hash_key(raw),
        "user_id": auth.user_id,
        "name": body.name,
        "created_at": int(time.time()),
    }
    keys = await _get_api_keys()
    keys.append(entry)
    await _save_api_keys(keys)

    return {"key": raw, "id": entry["id"], "name": body.name}


@router.get("/keys")
async def list_api_keys(request: Request):
    """List API keys (masked). Requires cookie auth."""
    from cptr.utils.config import check_access

    client_host = request.client.host if request.client else "127.0.0.1"
    jwt_token = request.cookies.get("cptr_session")
    auth = check_access(client_host=client_host, jwt_token=jwt_token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "Admin authentication required")

    keys = await _get_api_keys()
    return [
        {
            "id": k.get("id"),
            "name": k.get("name", ""),
            "created_at": k.get("created_at"),
        }
        for k in keys
    ]


@router.delete("/keys/{key_id}")
async def delete_api_key(request: Request, key_id: str):
    """Delete an API key. Requires cookie auth."""
    from cptr.utils.config import check_access

    client_host = request.client.host if request.client else "127.0.0.1"
    jwt_token = request.cookies.get("cptr_session")
    auth = check_access(client_host=client_host, jwt_token=jwt_token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "Admin authentication required")

    keys = await _get_api_keys()
    filtered = [k for k in keys if k.get("id") != key_id]
    if len(filtered) == len(keys):
        raise HTTPException(404, "Key not found")
    await _save_api_keys(filtered)
    return {"ok": True}
