"""Context usage helpers for chat compaction."""

from __future__ import annotations

from cptr.env import CHAT_COMPACT_TOKEN_THRESHOLD


def estimate_tokens(text: str) -> int:
    """Rough token estimate: len/4 for Latin text."""
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Total estimated tokens for a message list."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
                elif block.get("type") in ("image", "image_url"):
                    total += 1000  # rough estimate for images
        else:
            total += estimate_tokens(content)
        # Tool call arguments
        for tc in m.get("tool_calls", []):
            total += estimate_tokens(tc.get("function", {}).get("arguments", ""))
        total += 4  # per-message overhead (role, separators)
    return total


def should_compact(
    messages: list[dict],
    system_prompt: str,
    last_usage: dict | None = None,
    new_messages_since: int = 0,
) -> bool:
    """True when estimated tokens exceed the compact token threshold.

    If last_usage is provided (real data from the previous API call),
    uses actual input_tokens + output_tokens as the base and only
    estimates the new messages appended since that call.
    Falls back to full estimation when no usage data exists.
    """
    threshold = _get_threshold()

    if last_usage and last_usage.get("input_tokens"):
        # Real base from last API call + estimate only new additions
        base = last_usage["input_tokens"] + last_usage.get("output_tokens", 0)
        if new_messages_since > 0:
            new_msgs = messages[-new_messages_since:]
            base += estimate_messages_tokens(new_msgs)
        return base > threshold

    # Full estimation fallback
    total = estimate_tokens(system_prompt) + estimate_messages_tokens(messages)
    return total > threshold


def build_context_usage(tokens: int, *, threshold: int | None = None, source: str) -> dict:
    """Return context fullness stats for estimated token counts."""
    resolved_threshold = threshold or _get_threshold()
    percent = round((tokens / resolved_threshold) * 100) if resolved_threshold > 0 else 0
    return {
        "tokens": tokens,
        "estimated_tokens": tokens,
        "threshold": resolved_threshold,
        "percent": max(0, percent),
        "source": source,
    }


def estimate_context_usage(messages: list[dict], system_prompt: str) -> dict:
    """Return context fullness stats using the same estimate as compaction."""
    threshold = _get_threshold()
    estimated_tokens = estimate_tokens(system_prompt) + estimate_messages_tokens(messages)
    return build_context_usage(estimated_tokens, threshold=threshold, source="estimated")


def _get_threshold() -> int:
    """Read threshold: app_config (admin UI) > config.toml [chat] > env var/default."""
    try:
        from cptr.utils.config import load_config

        config = load_config()
        # Admin UI saves to [app_config] with dotted keys
        val = config.get("app_config", {}).get("chat.compact_token_threshold")
        if val is not None:
            return int(val)
        # Manual config.toml [chat] section
        val = config.get("chat", {}).get("compact_token_threshold")
        if val is not None:
            return int(val)
    except Exception:
        pass
    return CHAT_COMPACT_TOKEN_THRESHOLD
