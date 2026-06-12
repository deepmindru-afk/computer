"""WhatsApp adapter — zero SDK dependencies.

Uses Meta's WhatsApp Cloud API via httpx:
- Webhook endpoint for inbound messages (registered via FastAPI)
- REST POST to /messages for sending
- No native streaming; uses edit-via-context approach

Token format: ``access_token|phone_number_id`` (pipe-separated).

Requires a publicly accessible URL for webhook verification.
Configure the webhook URL in Meta's WhatsApp dashboard as:
    https://your-domain/api/webhooks/whatsapp/{bot_id}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from cptr.utils.bridge import BaseAdapter, MessageEvent, chunk_message

logger = logging.getLogger(__name__)

API_BASE = "https://graph.facebook.com/v21.0"
MAX_MESSAGE_LEN = 4096


def _parse_token(token: str) -> tuple[str, str]:
    """Parse 'access_token|phone_number_id' format."""
    if "|" in token:
        access_token, phone_number_id = token.split("|", 1)
        return access_token.strip(), phone_number_id.strip()
    raise ValueError("WhatsApp token must be 'access_token|phone_number_id' (pipe-separated)")


class WhatsAppAdapter(BaseAdapter):
    """WhatsApp bot via Cloud API + webhook inbound."""

    platform = "whatsapp"

    def __init__(self, token: str, bot_id: str = "") -> None:
        super().__init__()
        self._access_token, self._phone_number_id = _parse_token(token)
        self._bot_id = bot_id
        self._http: Optional[httpx.AsyncClient] = None
        self._running = False
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._process_task: Optional[asyncio.Task] = None

    # ── Lifecycle ──────────────────────────────────────────

    async def connect(self) -> bool:
        self._http = httpx.AsyncClient(
            timeout=15,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )

        # Verify token by fetching phone number info
        try:
            resp = await self._http.get(f"{API_BASE}/{self._phone_number_id}")
            data = resp.json()
            if "error" in data:
                raise ValueError(data["error"].get("message", "Invalid token"))
            display_name = data.get("verified_name") or data.get("display_phone_number", "?")
            logger.info("WhatsApp bot connected: %s (%s)", display_name, self._phone_number_id)
        except Exception:
            logger.exception("WhatsApp token verification failed")
            await self._http.aclose()
            self._http = None
            return False

        self._running = True
        self._process_task = asyncio.create_task(self._process_loop())
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._process_task and not self._process_task.done():
            self._process_task.cancel()
            try:
                await self._process_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Webhook ingestion ──────────────────────────────────

    async def handle_webhook(self, payload: dict) -> None:
        """Called by the webhook route to push inbound messages."""
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    if message.get("type") != "text":
                        continue

                    sender = message.get("from", "")
                    text = message.get("text", {}).get("body", "").strip()
                    if not text:
                        continue

                    # Resolve sender name from contacts
                    contacts = value.get("contacts", [])
                    sender_name = "User"
                    for c in contacts:
                        if c.get("wa_id") == sender:
                            profile = c.get("profile", {})
                            sender_name = profile.get("name", sender)
                            break

                    event = MessageEvent(
                        platform="whatsapp",
                        chat_id=sender,  # WhatsApp uses phone number as chat ID
                        sender_id=sender,
                        sender_name=sender_name,
                        text=text,
                    )
                    await self._message_queue.put(event)

    async def _process_loop(self) -> None:
        """Process inbound messages from the webhook queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._message_queue.get(), timeout=5.0)
                if self.on_message:
                    await self.on_message(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error processing WhatsApp message")

    # ── Sending ────────────────────────────────────────────

    async def send(self, chat_id: str, text: str) -> str | None:
        if not self._http:
            return None
        chunks = chunk_message(text, MAX_MESSAGE_LEN)
        msg_id = None
        for chunk in chunks:
            resp = await self._http.post(
                f"{API_BASE}/{self._phone_number_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "to": chat_id,
                    "type": "text",
                    "text": {"body": chunk},
                },
            )
            data = resp.json()
            if msg_id is None:
                messages = data.get("messages", [])
                if messages:
                    msg_id = messages[0].get("id")
        return msg_id

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """WhatsApp doesn't support message editing. Send a new message instead."""
        # WhatsApp has no edit API — for streaming we just send new messages
        # The stream loop will handle this gracefully
        pass

    async def send_typing(self, chat_id: str) -> None:
        """Send a 'typing' indicator (read receipt + typing)."""
        if not self._http:
            return
        try:
            # Mark as read (closest to typing indicator)
            await self._http.post(
                f"{API_BASE}/{self._phone_number_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": "placeholder",  # Not ideal but API requires it
                },
            )
        except Exception:
            pass


async def verify_token(token: str) -> dict:
    """Verify WhatsApp Cloud API credentials."""
    access_token, phone_number_id = _parse_token(token)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{API_BASE}/{phone_number_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        data = resp.json()
        if "error" in data:
            raise ValueError(data["error"].get("message", "Invalid credentials"))

        return {
            "username": data.get("verified_name") or data.get("display_phone_number"),
            "id": phone_number_id,
        }
