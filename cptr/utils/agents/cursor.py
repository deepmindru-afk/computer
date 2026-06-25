"""Cursor Agent ACP adapter."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any, AsyncIterator

from cptr.utils.agents.acp import AcpClient, acp_event_stream, acp_text_from_update
from cptr.utils.agents.events import AgentDone, AgentError, AgentEvent, AgentTextDelta


CURSOR_CAPABILITIES = {"_meta": {"parameterizedModelPicker": True}}


def _prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                str(block.get("text", "")) for block in content if isinstance(block, dict)
            )
        else:
            text = str(content or "")
        if text:
            parts.append(f"[{role}]\n{text}")
    return "\n\n".join(parts)


def _auto_approve(chat_params: dict[str, Any]) -> bool:
    if chat_params.get("tool_approval_mode") == "full":
        return True
    return bool(chat_params.get("auto_approve_tools"))


async def run_cursor_agent(
    *,
    profile: dict[str, Any],
    model: str,
    workspace: str,
    messages: list[dict[str, Any]],
    system_prompt: str,
    chat_params: dict[str, Any],
    resume_state: dict[str, Any] | None,
) -> AsyncIterator[AgentEvent]:
    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))

    args = []
    api_endpoint = str(profile.get("api_endpoint") or "").strip()
    if api_endpoint:
        args.extend(["-e", api_endpoint])
    args.append("acp")

    session_id = None
    if resume_state and isinstance(resume_state.get("session_id"), str):
        session_id = resume_state["session_id"]

    client = AcpClient(
        command=str(profile["command"]),
        args=args,
        cwd=workspace,
        env=env,
        auth_method_id="cursor_login",
        client_capabilities=CURSOR_CAPABILITIES,
        resume_session_id=session_id,
        auto_approve_permissions=_auto_approve(chat_params),
    )
    try:
        await client.start()
        if model != "default":
            await client.set_model(model)

        prompt = _prompt_from_messages(messages)
        if system_prompt:
            prompt = f"{system_prompt}\n\n{prompt}" if prompt else system_prompt

        prompt_task = asyncio.create_task(client.prompt(prompt))
        try:
            async for event in acp_event_stream(client):
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                text = acp_text_from_update(params)
                if text:
                    yield AgentTextDelta(text)
                if prompt_task.done():
                    try:
                        next_event = await asyncio.wait_for(client.events.get(), timeout=0.25)
                    except asyncio.TimeoutError:
                        break
                    next_params = (
                        next_event.get("params") if isinstance(next_event.get("params"), dict) else {}
                    )
                    next_text = acp_text_from_update(next_params)
                    if next_text:
                        yield AgentTextDelta(next_text)
            await prompt_task
        finally:
            if not prompt_task.done():
                prompt_task.cancel()
                with suppress(asyncio.CancelledError):
                    await prompt_task

        yield AgentDone(
            resume_state={
                "profile_id": profile["id"],
                "session_id": client.session_id,
                "workspace": workspace,
                "model": model,
            }
        )
    except asyncio.CancelledError:
        await client.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced in chat.
        yield AgentError(str(exc))
    finally:
        await client.close()
