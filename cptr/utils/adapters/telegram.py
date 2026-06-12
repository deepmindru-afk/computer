"""Telegram adapter — zero SDK dependencies.

Uses raw httpx calls to the Telegram Bot API:
- ``sendMessageDraft`` for native streaming (Bot API 9.5+)
- ``editMessageText`` as fallback for older clients
- ``getUpdates`` long-polling for inbound messages
- ``sendChatAction(typing)`` for typing indicators
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from cptr.utils.bridge import BaseAdapter, MessageEvent, chunk_message

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT = 30  # seconds (Telegram long-poll)
MAX_MESSAGE_LEN = 4096
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0


class TelegramAdapter(BaseAdapter):
    """Telegram bot via raw HTTP — getUpdates long-polling + native streaming."""

    platform = "telegram"

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token
        self._base = API_BASE.format(token=token)
        self._client: Optional[httpx.AsyncClient] = None
        self._offset: int = 0
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        self._bot_info: dict = {}
        self._supports_draft: bool = True  # Assume 9.5+, fallback on error

    # ── Lifecycle ──────────────────────────────────────────

    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 10))
        try:
            info = await self._api("getMe")
            self._bot_info = info
            logger.info(
                "Telegram bot connected: @%s (%s)",
                info.get("username", "?"),
                info.get("id", "?"),
            )
        except Exception:
            logger.exception("Telegram getMe failed — bad token?")
            await self._client.aclose()
            self._client = None
            return False

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Sending ────────────────────────────────────────────

    async def send(self, chat_id: str, text: str) -> str | None:
        """Send a message. Returns the message ID for later editing."""
        chunks = chunk_message(text, MAX_MESSAGE_LEN)
        msg_id = None
        for chunk in chunks:
            result = await self._api(
                "sendMessage",
                chat_id=chat_id,
                text=chunk,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            if msg_id is None and result:
                msg_id = str(result.get("message_id", ""))
        return msg_id

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """Edit a previously sent message."""
        try:
            await self._api(
                "editMessageText",
                chat_id=chat_id,
                message_id=int(message_id),
                text=text[:MAX_MESSAGE_LEN],
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except TelegramAPIError as e:
            if "message is not modified" in e.description.lower():
                pass  # Content unchanged, ignore
            elif "parse" in e.description.lower():
                # Markdown parse error — retry without parse_mode
                await self._api(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=text[:MAX_MESSAGE_LEN],
                    disable_web_page_preview=True,
                )
            else:
                raise

    async def send_draft(self, chat_id: str, text: str, draft_id: str | None = None) -> str:
        """Send a streaming draft using sendMessageDraft (Bot API 9.5+).

        Returns the draft_id for subsequent updates.
        Falls back to edit-based streaming if the API doesn't support drafts.
        """
        if not self._supports_draft:
            raise TelegramAPIError(400, "Draft not supported")

        try:
            params: dict = {
                "chat_id": chat_id,
                "text": text[:MAX_MESSAGE_LEN],
                "parse_mode": "Markdown",
            }
            if draft_id:
                params["draft_id"] = draft_id

            result = await self._api("sendMessageDraft", **params)
            return str(result.get("draft_id", result.get("message_id", "")))

        except TelegramAPIError as e:
            if e.code == 404 or "unknown method" in e.description.lower():
                # API doesn't support sendMessageDraft — fall back
                self._supports_draft = False
                logger.info("Telegram sendMessageDraft not available, falling back to editMessageText")
                raise
            # Markdown parse failure — retry without parse_mode
            if "parse" in e.description.lower():
                params.pop("parse_mode", None)
                result = await self._api("sendMessageDraft", **params)
                return str(result.get("draft_id", result.get("message_id", "")))
            raise

    async def send_typing(self, chat_id: str) -> None:
        try:
            await self._api("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass

    # ── Polling ────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        delay = RECONNECT_BASE_DELAY
        while self._running:
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=self._offset,
                    timeout=POLL_TIMEOUT,
                    allowed_updates=["message"],
                )
                delay = RECONNECT_BASE_DELAY

                for update in updates:
                    try:
                        await self._process_update(update)
                    except Exception:
                        logger.exception("Error processing Telegram update")

            except asyncio.CancelledError:
                return
            except httpx.TimeoutException:
                continue
            except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, OSError) as e:
                logger.warning("Telegram poll network error (%s), retrying in %.0fs", type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
            except Exception:
                logger.exception("Telegram poll error, retrying in %.0fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _process_update(self, update: dict) -> None:
        update_id = update.get("update_id", 0)
        self._offset = max(self._offset, update_id + 1)

        message = update.get("message")
        if not message:
            return

        text = message.get("text") or message.get("caption") or ""
        if not text.strip():
            return

        chat = message.get("chat", {})
        sender = message.get("from", {})

        event = MessageEvent(
            platform="telegram",
            chat_id=str(chat.get("id", "")),
            sender_id=str(sender.get("id", "")),
            sender_name=(
                sender.get("first_name", "")
                + (" " + sender.get("last_name", "") if sender.get("last_name") else "")
            ).strip() or "User",
            text=text,
        )

        if self.on_message:
            await self.on_message(event)

    # ── API helpers ────────────────────────────────────────

    async def _api(self, method: str, **params) -> any:
        if not self._client:
            raise RuntimeError("Telegram client not connected")

        url = f"{self._base}/{method}"
        data = {k: v for k, v in params.items() if v is not None}

        resp = await self._client.post(url, json=data)
        body = resp.json()

        if not body.get("ok"):
            error_code = body.get("error_code", resp.status_code)
            description = body.get("description", "Unknown error")
            # Markdown parse failure — retry without parse_mode
            if error_code == 400 and "parse" in description.lower() and "text" in data:
                data.pop("parse_mode", None)
                resp = await self._client.post(url, json=data)
                body = resp.json()
                if body.get("ok"):
                    return body.get("result")
            raise TelegramAPIError(error_code, description)

        return body.get("result")


class TelegramAPIError(Exception):
    def __init__(self, code: int, description: str):
        self.code = code
        self.description = description
        super().__init__(f"Telegram API error {code}: {description}")


async def verify_token(token: str) -> dict:
    """Verify a Telegram bot token by calling getMe."""
    async with httpx.AsyncClient(timeout=10) as client:
        url = f"{API_BASE.format(token=token)}/getMe"
        resp = await client.get(url)
        body = resp.json()
        if not body.get("ok"):
            raise TelegramAPIError(
                body.get("error_code", resp.status_code),
                body.get("description", "Invalid token"),
            )
        return body["result"]
