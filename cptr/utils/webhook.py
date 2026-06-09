"""Webhook notifications: POST to Slack, Discord, Teams, or generic JSON.

Adapted from open-webui's webhook.py, using httpx instead of aiohttp.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


async def post_webhook(url: str, title: str, message: str) -> bool:
    """Send a notification to a webhook URL.

    Auto-detects Slack, Discord, Teams, and Google Chat webhook URLs
    and formats the payload accordingly. Falls back to generic JSON.

    Returns True on success, False on failure (never raises).
    """
    try:
        payload: dict = {}

        # Slack and Google Chat
        if "hooks.slack.com" in url or "chat.googleapis.com" in url:
            payload["text"] = f"*{title}*\n{message}"

        # Discord (2000 char limit)
        elif "discord.com/api/webhooks" in url:
            content = f"**{title}**\n{message}"
            if len(content) > 2000:
                content = content[:1980] + "... (truncated)"
            payload["content"] = content

        # Microsoft Teams
        elif "webhook.office.com" in url:
            payload = {
                "@type": "MessageCard",
                "@context": "http://schema.org/extensions",
                "themeColor": "0076D7",
                "summary": title,
                "sections": [
                    {
                        "activityTitle": title,
                        "activitySubtitle": "cptr",
                        "facts": [{"name": "Message", "value": message}],
                        "markdown": True,
                    }
                ],
            }

        # Generic JSON
        else:
            payload = {
                "title": title,
                "message": message,
                "source": "cptr",
            }

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()

        logger.info("[webhook] Sent notification to %s", url[:50])
        return True

    except Exception:
        logger.exception("[webhook] Failed to send notification to %s", url[:50])
        return False
""", "Description": "Adapted from open-webui's webhook.py — uses httpx, supports Slack/Discord/Teams/generic JSON", "IsArtifact": false, "Overwrite": false, "TargetFile": "/Users/tim/Documents/workspace/computer/cptr/utils/webhook.py
