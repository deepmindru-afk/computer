"""Chat task runner: agentic loop with tool calling.

Runs as an asyncio.Task. Streams deltas via Socket.IO, persists to DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from cptr.env import CHAT_MAX_ITERATIONS
from cptr.models import Chat, ChatMessage, Config
from cptr.socket.main import emit_to_user
from cptr.utils.ai import (
    ChatCompletionForm,
    chat_completion,
    stream_anthropic,
    stream_openai_completions,
    stream_openai_responses,
)
from cptr.utils.config import _get_jwt_secret, now_ms
from cptr.utils.crypto import decrypt_key
from cptr.utils.tools import TOOLS, execute_tool, get_tool_list, _fn_to_schema, create_artifact
from cptr.utils.chat_export import export_chat_to_file
from cptr.utils.json_parser import extract_json

logger = logging.getLogger(__name__)

PLAN_MODE_PROMPT = (
    "[Plan Mode] Research the codebase with read-only tools, then present your plan "
    "using create_artifact. Wait for approval before coding."
)

# ── Task registry ───────────────────────────────────────────

_tasks: dict[str, asyncio.Task] = {}  # message_id → asyncio.Task
_task_state: dict[str, dict] = {}  # message_id → {content, output}
_task_chat: dict[str, str] = {}  # message_id → chat_id
_queue_locks: dict[str, asyncio.Lock] = {}  # chat_id → Lock


def start_task(
    message_id: str,
    chat_id: str,
    user_id: str,
    connection: dict,
    workspace: str,
    model: str,
    regeneration_prompt: str | None = None,
):
    """Launch the agentic loop as a background asyncio.Task."""
    task = asyncio.create_task(
        run_chat_task(
            message_id, chat_id, user_id, connection, workspace, model, regeneration_prompt
        )
    )
    _tasks[message_id] = task
    _task_chat[message_id] = chat_id


async def cancel_task(message_id: str) -> bool:
    """Cancel a running task. Returns True if found."""
    task = _tasks.get(message_id)
    if task:
        task.cancel()
        return True
    return False


def is_running(message_id: str) -> bool:
    """Check if a task is currently running."""
    task = _tasks.get(message_id)
    return task is not None and not task.done()


def get_live_state(message_id: str) -> dict | None:
    """Get live in-memory state for a running task."""
    return _task_state.get(message_id)


def get_active_chat_ids() -> set[str]:
    """Return the set of chat_ids that currently have a running task."""
    return {cid for mid, cid in _task_chat.items() if mid in _tasks and not _tasks[mid].done()}


# ── Queue processing ────────────────────────────────────────


async def _process_queue(chat_id: str, user_id: str, workspace: str):
    """Check for queued user messages and start the next task.

    Uses a per-chat lock to prevent concurrent processing from
    both the task's finally block and the API double-check.
    """
    lock = _queue_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        all_msgs = await ChatMessage.get_all_by_chat(chat_id)

        # Don't process queue if there's still an active generation
        if any(m.role == "assistant" and not m.done for m in all_msgs):
            return

        # Find queued messages (ordered by created_at)
        queued = [m for m in all_msgs if m.role == "user" and m.meta and m.meta.get("queued")]
        if not queued:
            return

        # Combine all queued prompts into one user message
        combined_content = "\n\n".join(m.content for m in queued if m.content)

        # Find the current leaf (latest done assistant message)
        done_assistants = [m for m in all_msgs if m.role == "assistant" and m.done]
        parent_id = done_assistants[-1].id if done_assistants else queued[0].parent_id

        # Delete individual queued messages, create one combined message
        for m in queued:
            await ChatMessage.delete(m.id)

        combined_msg = await ChatMessage.create(
            chat_id=chat_id,
            role="user",
            content=combined_content,
            parent_id=parent_id,
            created_at=now_ms(),
        )

        # Resolve model from the chat's last used model
        chat = await Chat.get_by_id(chat_id)
        if not chat:
            return
        model_id = (chat.meta or {}).get("last_model", "")
        if not model_id:
            # Fall back to the model from the last assistant message
            last_asst = done_assistants[-1] if done_assistants else None
            model_id = (last_asst.model if last_asst else "") or ""
        if not model_id:
            logger.error("[queue] No model found for chat %s, cannot process queue", chat_id)
            return

        # Resolve connection
        try:
            from cptr.routers.chat import _resolve_connection

            connection, bare_model = await _resolve_connection(model_id)
        except Exception:
            logger.exception("[queue] Failed to resolve connection for model %s", model_id)
            return

        # Create assistant placeholder
        assistant_msg = await ChatMessage.create(
            chat_id=chat_id,
            role="assistant",
            content="",
            parent_id=combined_msg.id,
            model=model_id,
            done=False,
            created_at=now_ms(),
        )
        await Chat.update_current_message(chat_id, assistant_msg.id, now_ms())

        # Notify frontend that queue was processed (new messages appeared)
        await emit_to_user(
            user_id,
            {"chat_id": chat_id, "message_id": assistant_msg.id, "queue_processed": True},
        )

        # Start new task
        start_task(
            message_id=assistant_msg.id,
            chat_id=chat_id,
            user_id=user_id,
            connection=connection,
            workspace=workspace,
            model=bare_model,
        )
        logger.info(
            "[queue] Processed %d queued message(s) for chat %s",
            len(queued),
            chat_id[:8],
        )


async def reconcile_chat_state():
    """Recover from server crash: fix stuck messages, process orphaned queues.

    Called once on startup when ENABLE_CHAT_RECONCILE_ON_STARTUP=true (default).
    Finds:
      1. Assistant messages with done=False that have no running task → mark done
      2. Chats with queued user messages → process them
    """
    from sqlalchemy import select, and_
    from cptr.utils.db import get_db

    async with await get_db() as db:
        result = await db.execute(
            select(ChatMessage).where(
                and_(
                    ChatMessage.role == "assistant",
                    ChatMessage.done == False,  # noqa: E712
                )
            )
        )
        stuck = list(result.scalars().all())

    healed_chats: set[str] = set()
    for msg in stuck:
        if not is_running(msg.id):
            logger.warning("[reconcile] Marking stuck message %s as done", msg.id)
            meta = dict(msg.meta or {})
            meta["error"] = "interrupted by server restart"
            await ChatMessage.update(msg.id, done=True, meta=meta)
            healed_chats.add(msg.chat_id)

    # Process orphaned queues for healed chats
    for cid in healed_chats:
        chat = await Chat.get_by_id(cid)
        if chat:
            workspace = (chat.meta or {}).get("workspace", "")
            try:
                await _process_queue(cid, chat.user_id, workspace)
            except Exception:
                logger.exception("[reconcile] Failed to process queue for chat %s", cid)

    if healed_chats:
        logger.info("[reconcile] Recovered %d chat(s) on startup", len(healed_chats))


# ── System prompt ───────────────────────────────────────────


def _get_file_tree(workspace: str, max_entries: int = 200) -> str:
    """Generate a compact file tree listing for the workspace."""
    ws = Path(workspace)
    if not ws.is_dir():
        return ""
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
        ".DS_Store",
    }
    entries = []
    for item in sorted(ws.iterdir()):
        if item.name in ignore:
            continue
        suffix = "/" if item.is_dir() else ""
        entries.append(f"  {item.name}{suffix}")
        if item.is_dir():
            try:
                for child in sorted(item.iterdir()):
                    if child.name in ignore:
                        continue
                    csuffix = "/" if child.is_dir() else ""
                    entries.append(f"    {child.name}{csuffix}")
                    if len(entries) >= max_entries:
                        entries.append("    ...")
                        break
            except PermissionError:
                pass
        if len(entries) >= max_entries:
            break
    return "\n".join(entries)


INSTRUCTION_FILENAMES = ["MEMORY.md", "AGENTS.md", "AGENT.md", "CLAUDE.md"]


def _load_instruction_files(workspace: str, max_bytes: int = 32_000) -> str:
    """Load well-known AI instruction files from workspace root.

    Scans for MEMORY.md, AGENTS.md, AGENT.md, CLAUDE.md.
    All found files are concatenated (not first-found-wins).
    """
    ws = Path(workspace)
    if not ws.is_dir():
        return ""
    parts: list[str] = []
    total = 0
    for name in INSTRUCTION_FILENAMES:
        path = ws / name
        if path.is_file():
            remaining = max_bytes - total
            if remaining <= 0:
                break
            try:
                content = path.read_text(errors="replace")[:remaining].strip()
            except OSError:
                continue
            if content:
                parts.append(f"# {name}\n{content}")
                total += len(content)
                logger.debug("[instructions] Loaded %s (%d bytes)", name, len(content))
    return "\n\n".join(parts)


def _load_system_prompt(workspace: str) -> str:
    """Load system prompt: .cptr/system.md > default. Appends instruction files + file tree."""
    ws_prompt = Path(workspace) / ".cptr" / "system.md"
    if ws_prompt.is_file():
        base = ws_prompt.read_text(errors="replace").strip()
    else:
        base = (
            "You are a helpful coding assistant. "
            "You have access to tools to read, search, and modify files in the workspace. "
            "Use them to help the user with their coding tasks.\n\n"
            "For complex tasks, create an implementation plan first using "
            "create_artifact, then wait for approval before coding."
        )

    # Load MEMORY.md / AGENTS.md / CLAUDE.md from workspace root
    instructions = _load_instruction_files(workspace)
    if instructions:
        base += f"\n\n<instructions>\n{instructions}\n</instructions>"
        base += (
            "\n\nThe above <instructions> were loaded from instruction files in the workspace root. "
            "These files persist across sessions. "
            "You can update them with your file tools to save learnings, decisions, or "
            "project conventions for future sessions."
        )

    tree = _get_file_tree(workspace)
    if tree:
        base += f"\n\nWorkspace: {Path(workspace).name}\nFiles:\n{tree}"

    return base


# ── Title generation ────────────────────────────────────────


TITLE_PROMPT = (
    "Generate a concise title (max 8 words) for this conversation based on the user's message. "
    'Respond with ONLY a JSON object: {"title": "Your Title Here"}. '
    "Do not include quotes around the JSON. Do not explain."
)


async def generate_chat_title(
    chat_id: str,
    user_id: str,
    connection: dict,
    model: str,
    user_message: str,
):
    """Generate a proper title for a new chat via a lightweight LLM call.

    Uses the shared chat_completion() helper from ai.py.
    On success, updates the DB and emits a socket event so the frontend
    updates the tab label in real time.
    """
    provider = connection["provider"]
    api_key = decrypt_key(connection.get("api_key", ""), _get_jwt_secret())
    base_url = connection.get("base_url") or _default_base_url(provider)
    api_type = connection.get("api_type", "chat_completions")

    # Truncate very long messages to keep the prompt small
    truncated = user_message[:200].strip()
    if len(user_message) > 200:
        truncated += "..."

    try:
        text = await chat_completion(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=[{"role": "user", "content": truncated}],
            system=TITLE_PROMPT,
            max_tokens=50,
            api_type=api_type,
        )

        # Parse the JSON title
        parsed = extract_json(text)
        title = (parsed.get("title", "") if isinstance(parsed, dict) else "").strip()
        if not title:
            return

        # Persist and notify
        await Chat.update_title(chat_id, title, now_ms())
        await emit_to_user(user_id, {"chat_id": chat_id, "title": title})
        logger.info("[title] Generated title for chat %s: %s", chat_id[:8], title)

    except Exception:
        logger.debug("[title] Failed to generate title for chat %s", chat_id[:8], exc_info=True)


# ── Message history ─────────────────────────────────────────


async def _load_message_history(chat_id: str, message_id: str) -> list[dict]:
    """Load the ancestor chain from message_id to root as LLM messages.

    Walks up via parent_id so only the active branch is included.
    The current message (message_id) is always included even if done=False,
    since it may contain completed tool calls from prior approval rounds.
    """
    all_msgs = await ChatMessage.get_all_by_chat(chat_id)
    msg_map = {m.id: m for m in all_msgs}

    # Trace from message_id up to root
    chain: list = []
    cur = msg_map.get(message_id)
    while cur:
        chain.append(cur)
        cur = msg_map.get(cur.parent_id) if cur.parent_id else None
    chain.reverse()  # root → leaf

    result = []
    for m in chain:
        # Skip in-progress assistant placeholders, but NOT the current
        # message being continued, which may have accumulated tool call
        # results from prior approval rounds that the LLM needs to see.
        if m.role == "assistant" and not m.done and m.id != message_id:
            continue
        # For the current message, skip if it has no content and no output
        # (truly empty placeholder on first run)
        if m.id == message_id and not m.done and not m.content and not m.output:
            continue
        entry: dict = {"role": m.role, "content": m.content or ""}

        # Transform uploaded images into base64 multimodal blocks; inline text files
        if m.role == "user":
            attached_files = (m.meta or {}).get("files", [])
            images = [
                f for f in attached_files 
                if isinstance(f, dict) and (f.get("type") == "image" or (f.get("content_type") or "").startswith("image/"))
            ]
            non_images = [
                f for f in attached_files 
                if isinstance(f, dict) and f not in images
            ]
            
            if images or non_images:
                from cptr.utils.storage import get_storage
                import base64
                
                text_content = entry["content"]
                
                # Append file:// references so the AI can read them with view_file
                if non_images:
                    from cptr.utils.storage import UPLOADS_DIR
                    file_refs = []
                    for f in non_images:
                        file_id = f.get("id")
                        if not file_id:
                            continue
                        name = f.get("name", "file")
                        file_path = UPLOADS_DIR / file_id
                        file_refs.append(f"[{name}](file://{file_path})")
                    if file_refs:
                        text_content += "\n\nAttached files:\n" + "\n".join(file_refs)

                content_blocks = [{"type": "text", "text": text_content}] if text_content else []
                
                for img in images:
                    file_id = img.get("id")
                    if not file_id:
                        continue
                    data = await get_storage().get(file_id)
                    if data:
                        b64_str = base64.b64encode(data).decode("utf-8")
                        ctype = img.get("content_type") or "image/png"
                        content_blocks.append({
                            "type": "image",
                            "media_type": ctype,
                            "base64": b64_str
                        })
                
                if len(content_blocks) > (1 if text_content else 0):
                    entry["content"] = content_blocks
                elif text_content != entry["content"]:
                    entry["content"] = text_content

        # Reconstruct tool calls from output items for the provider
        if m.output:
            tool_calls = []
            for item in m.output:
                if item.get("type") == "function_call" and item.get("status") == "completed":
                    tool_calls.append(
                        {
                            "id": item["call_id"],
                            "type": "function",
                            "function": {
                                "name": item["name"],
                                "arguments": json.dumps(item.get("arguments", {})),
                            },
                        }
                    )
            if tool_calls:
                entry["tool_calls"] = tool_calls

            # Add tool results as separate messages
            for item in m.output:
                if item.get("type") == "function_call_output":
                    result.append(entry)
                    entry = {
                        "role": "tool",
                        "tool_call_id": item["call_id"],
                        "content": item.get("output", ""),
                    }

        result.append(entry)
    return result


def _append_tool_to_messages(messages: list[dict], event: dict, result: str, provider: str):
    """Append a tool call + result to the message history for the next API call."""
    # Add assistant message with tool_call
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": event["call_id"],
                    "type": "function",
                    "function": {
                        "name": event["name"],
                        "arguments": json.dumps(event["arguments"]),
                    },
                }
            ],
        }
    )
    # Add tool result
    messages.append(
        {
            "role": "tool",
            "tool_call_id": event["call_id"],
            "content": result,
        }
    )


# ── Connection resolution ───────────────────────────────────


def _default_base_url(provider: str) -> str:
    return {
        "anthropic": "https://api.anthropic.com/v1",
        "openai": "https://api.openai.com/v1",
    }.get(provider, "https://api.openai.com/v1")


# ── The agentic loop ────────────────────────────────────────


async def run_chat_task(
    message_id: str,
    chat_id: str,
    user_id: str,
    connection: dict,
    workspace: str,
    model: str,
    regeneration_prompt: str | None = None,
):
    """Plain async function. Makes raw API calls in a loop."""

    async def emit(**data):
        """Stream an output delta to the user."""
        await emit_to_user(user_id, {"chat_id": chat_id, "message_id": message_id, **data})

    # Load existing state so continuations don't overwrite previous output
    msg = await ChatMessage.get_by_id(message_id)
    content = (msg.content or "") if msg else ""
    output_items: list[dict] = list(msg.output or []) if msg else []
    text_buffer = ""  # Accumulates text between tool calls

    logger.info(
        "[task %s] start: existing content=%d chars, output=%d items",
        message_id[:8],
        len(content),
        len(output_items),
    )

    def _flush_text() -> dict | None:
        """Flush accumulated text into a message output item."""
        nonlocal text_buffer
        if not text_buffer:
            return None
        logger.info(
            "[task %s] flush_text: %d chars into message item", message_id[:8], len(text_buffer)
        )
        item = {
            "type": "message",
            "id": str(uuid.uuid4()),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text_buffer}],
        }
        output_items.append(item)
        text_buffer = ""
        _sync_state()
        return item

    def _sync_state():
        """Update in-memory state so API can serve it on refresh."""
        _task_state[message_id] = {"content": content, "output": output_items}

    try:
        provider = connection["provider"]
        api_key = decrypt_key(connection.get("api_key", ""), _get_jwt_secret())
        base_url = connection.get("base_url") or _default_base_url(provider)

        system = _load_system_prompt(workspace)
        messages = await _load_message_history(chat_id, message_id)
        if regeneration_prompt:
            messages.append({"role": "user", "content": regeneration_prompt})
        tools = get_tool_list()

        # Load chat params for approval mode
        chat_obj = await Chat.get_by_id(chat_id)
        chat_params = (chat_obj.meta or {}).get("params", {}) if chat_obj else {}

        # Plan mode: strip write tools, inject prompt as user message (not system, to preserve cache)
        plan_mode = chat_params.get("plan_mode", False)
        if plan_mode:
            tools = [t for t in tools if TOOLS.get(t["name"], {}).get("auto")]
            # Inject create_artifact (only available in plan mode)
            tools.append(_fn_to_schema("create_artifact", create_artifact))
            messages.append({"role": "user", "content": PLAN_MODE_PROMPT})
            logger.info("[task %s] plan mode active, %d tools available", message_id[:8], len(tools))

        # Tool approval mode: 'ask' | 'auto' | 'full'
        #   ask  = require approval for ALL tools (including reads)
        #   auto = auto-approve tools marked auto=True, ask for others
        #   full = auto-approve everything
        approval_mode = chat_params.get("tool_approval_mode", "auto")
        # Legacy compat: old boolean auto_approve_tools
        if "tool_approval_mode" not in chat_params and "auto_approve_tools" in chat_params:
            approval_mode = "full" if chat_params["auto_approve_tools"] else "auto"

        for _iteration in range(CHAT_MAX_ITERATIONS):
            form_data = ChatCompletionForm(
                model=model,
                messages=messages,
                instructions=system,
                tools=tools,
            )

            if provider == "anthropic":
                stream = stream_anthropic(form_data, base_url, api_key)
            elif connection.get("api_type") == "responses":
                stream = stream_openai_responses(form_data, base_url, api_key)
            else:
                stream = stream_openai_completions(form_data, base_url, api_key)

            restart = False

            async for event in stream:
                if event["type"] == "text_delta":
                    content += event["content"]
                    text_buffer += event["content"]
                    await emit(delta=event["content"])
                    _sync_state()

                elif event["type"] == "tool_call":
                    # Flush any text before the tool call
                    flushed_item = _flush_text()

                    name = event["name"]
                    tool = TOOLS.get(name)
                    item = {
                        "type": "function_call",
                        "id": str(uuid.uuid4()),
                        "call_id": event["call_id"],
                        "name": name,
                        "arguments": event["arguments"],
                    }

                    should_auto = approval_mode == "full" or (
                        approval_mode == "auto" and tool and tool["auto"]
                    )

                    if should_auto:
                        if name == "create_artifact":
                            result = await create_artifact(**event["arguments"], workspace=workspace)
                        else:
                            result = await execute_tool(name, event["arguments"], workspace)
                        item["status"] = "completed"
                        output_items.append(item)
                        result_item = {
                            "type": "function_call_output",
                            "call_id": event["call_id"],
                            "output": result,
                        }
                        output_items.append(result_item)
                        if flushed_item:
                            await emit(output=flushed_item)
                        await emit(output=item)
                        await emit(output=result_item)
                        _sync_state()

                        # Artifact UI card: detect create_artifact or create_file with artifact_type
                        args = event["arguments"]
                        artifact_type = args.get("artifact_type", "")
                        if name == "create_artifact":
                            artifact_type = artifact_type or "implementation_plan"
                        if artifact_type:
                            # Parse metadata from tool result or build from args
                            try:
                                meta = json.loads(result)
                            except (json.JSONDecodeError, TypeError):
                                meta = {}
                            artifact_item = {
                                "type": "artifact",
                                "artifact_type": artifact_type,
                                "title": meta.get("title") or args.get("title") or artifact_type.replace("_", " ").title(),
                                "content": args.get("content", ""),
                                "path": meta.get("path", ""),
                            }
                            output_items.append(artifact_item)
                            await emit(output=artifact_item)
                            _sync_state()

                        # Persist intermediate state so content survives crashes/errors
                        await ChatMessage.update(
                            message_id, content=content, output=output_items
                        )

                        # Append to messages for next iteration
                        _append_tool_to_messages(messages, event, result, provider)
                        restart = True
                        break

                    else:
                        # Needs approval, persist and stop
                        item["status"] = "pending"
                        output_items.append(item)
                        await ChatMessage.update(
                            message_id,
                            content=content,
                            output=output_items,
                            done=False,
                        )
                        if flushed_item:
                            await emit(output=flushed_item)
                        await emit(output=item)
                        _task_state.pop(message_id, None)
                        await emit(done=True)
                        return

                elif event["type"] == "usage":
                    _flush_text()
                    usage = {k: v for k, v in event.items() if k != "type"}
                    logger.info(
                        "[task %s] save (usage): content=%d chars, output=%d items, types=%s",
                        message_id[:8],
                        len(content),
                        len(output_items),
                        [i.get("type") for i in output_items],
                    )
                    await ChatMessage.update(
                        message_id,
                        content=content,
                        output=output_items,
                        usage=usage,
                        done=True,
                    )
                    _task_state.pop(message_id, None)
                    await emit(done=True)
                    return

                elif event["type"] == "done":
                    # Stream ended without explicit usage
                    pass

            if not restart:
                flushed_item = _flush_text()
                if flushed_item:
                    await emit(output=flushed_item)
                logger.info(
                    "[task %s] save (end): content=%d chars, output=%d items, types=%s",
                    message_id[:8],
                    len(content),
                    len(output_items),
                    [i.get("type") for i in output_items],
                )
                await ChatMessage.update(
                    message_id,
                    content=content,
                    output=output_items,
                    done=True,
                )
                _task_state.pop(message_id, None)
                await emit(done=True)
                return

        # Max iterations reached
        await ChatMessage.update(
            message_id,
            content=content,
            output=output_items,
            done=True,
            meta={"error": "max iterations reached"},
        )
        _task_state.pop(message_id, None)
        await emit(done=True)

    except asyncio.CancelledError:
        _flush_text()
        await ChatMessage.update(message_id, content=content, output=output_items, done=True)
        _task_state.pop(message_id, None)
        await emit(done=True)
    except Exception as e:
        logger.exception(f"Chat task error for message {message_id}")
        _flush_text()
        await ChatMessage.update(
            message_id,
            content=content,
            output=output_items,
            done=True,
            meta={"error": str(e)},
        )
        _task_state.pop(message_id, None)
        await emit(done=True)
    finally:
        _tasks.pop(message_id, None)
        _task_state.pop(message_id, None)
        _task_chat.pop(message_id, None)
        try:
            await export_chat_to_file(chat_id)
        except Exception:
            logger.exception(f"Failed to export chat {chat_id}")
        # Generate a proper title if the chat still has the auto-truncated fallback
        try:
            chat_obj = await Chat.get_by_id(chat_id)
            if chat_obj:
                all_msgs = await ChatMessage.get_all_by_chat(chat_id)
                first_user = next(
                    (
                        m
                        for m in all_msgs
                        if m.role == "user" and not (m.meta and m.meta.get("queued"))
                    ),
                    None,
                )
                if first_user:
                    # The router sets title = content[:50] on creation.
                    # Only generate if the title still matches that fallback.
                    fallback = first_user.content[:50].strip() or "New Chat"
                    if chat_obj.title == fallback:
                        await generate_chat_title(
                            chat_id, user_id, connection, model, first_user.content
                        )
        except Exception:
            logger.debug(
                "[title] Error in title generation for chat %s", chat_id[:8], exc_info=True
            )
        # Process any queued follow-up messages
        try:
            await _process_queue(chat_id, user_id, workspace)
        except Exception:
            logger.exception(f"Failed to process queue for chat {chat_id}")
