"""Claude Code SDK adapter."""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any, AsyncIterator

from cptr.utils.agents.events import AgentDone, AgentError, AgentEvent, AgentReasoningDelta, AgentTextDelta


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


def _chat_approval_mode(chat_params: dict[str, Any]) -> str:
    if "tool_approval_mode" in chat_params:
        return str(chat_params.get("tool_approval_mode") or "auto")
    if "auto_approve_tools" in chat_params:
        return "full" if chat_params.get("auto_approve_tools") else "auto"
    return "auto"


def _permission_mode(chat_approval_mode: str) -> str:
    if chat_approval_mode == "full":
        return "bypassPermissions"
    if chat_approval_mode == "auto":
        return "acceptEdits"
    return "default"


async def run_claude_code_agent(
    *,
    profile: dict[str, Any],
    model: str,
    workspace: str,
    messages: list[dict[str, Any]],
    system_prompt: str,
    chat_params: dict[str, Any],
    resume_state: dict[str, Any] | None,
) -> AsyncIterator[AgentEvent]:
    try:
        import claude_agent_sdk as sdk
    except ImportError:
        yield AgentError("Claude Code support requires the claude-agent-sdk Python package")
        return

    prompt = _prompt_from_messages(messages)
    env = os.environ.copy()
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))

    permission_mode = _permission_mode(_chat_approval_mode(chat_params))
    launch_args = str(profile.get("launch_args") or "").strip()
    extra_args = shlex.split(launch_args) if launch_args else []

    try:
        options_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt or None,
            "permission_mode": permission_mode,
            "env": env,
            "include_partial_messages": True,
        }
        if workspace:
            options_kwargs["cwd"] = workspace
        if extra_args:
            options_kwargs["extra_args"] = {arg: None for arg in extra_args}

        options = sdk.ClaudeAgentOptions(**options_kwargs)
        options.cli_path = str(profile["command"])
        if model != "default":
            options.model = model

        client = sdk.ClaudeSDKClient(options)
        await client.connect()
        session_id = None
        if resume_state:
            value = resume_state.get("session_id")
            session_id = value if isinstance(value, str) and value else None

        try:
            query = client.query(prompt, session_id=session_id) if session_id else client.query(prompt)
            await asyncio.wait_for(query, timeout=20)
            stream = client.receive_response()
            usage: dict[str, Any] | None = None
            observed_session_id = session_id

            async for message in stream:
                event = getattr(message, "event", None)
                if isinstance(event, dict):
                    event_type = event.get("type")
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if isinstance(delta, dict):
                            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                                yield AgentTextDelta(delta["text"])
                            elif delta.get("type") == "thinking_delta" and isinstance(
                                delta.get("thinking"), str
                            ):
                                yield AgentReasoningDelta(delta["thinking"])
                    continue

                class_name = message.__class__.__name__
                if class_name == "AssistantMessage":
                    for block in getattr(message, "content", []) or []:
                        if block.__class__.__name__ == "TextBlock":
                            text = getattr(block, "text", "")
                            if text:
                                yield AgentTextDelta(text)
                        elif block.__class__.__name__ == "ThinkingBlock":
                            text = getattr(block, "thinking", "")
                            if text:
                                yield AgentReasoningDelta(text)
                elif class_name == "ResultMessage":
                    observed_session_id = getattr(message, "session_id", None) or observed_session_id
                    raw_usage = getattr(message, "usage", None)
                    if isinstance(raw_usage, dict):
                        usage = {
                            **raw_usage,
                            "total_tokens": (raw_usage.get("input_tokens") or 0)
                            + (raw_usage.get("output_tokens") or 0),
                        }
                    break

            yield AgentDone(
                usage=usage,
                resume_state={
                    "profile_id": profile["id"],
                    "session_id": observed_session_id,
                    "workspace": workspace,
                    "model": model,
                },
            )
        finally:
            await client.disconnect()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced in the chat.
        yield AgentError(str(exc))
