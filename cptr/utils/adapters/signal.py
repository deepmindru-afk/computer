"""Signal adapter — uses signal-cli REST API bridge.

Requires a running signal-cli-rest-api container:
    docker run -p 8080:8080 bbernhard/signal-cli-rest-api

Token format: ``base_url|phone_number`` (pipe-separated).
Example: ``http://localhost:8080|+1234567890``

Uses polling on GET /v1/receive/{number} for inbound messages
and POST /v2/send for outbound messages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import quote

import httpx

from cptr.utils.bridge import BaseAdapter, MessageEvent, chunk_message

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 4096
POLL_INTERVAL = 2.0  # seconds between receive polls
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0


def _parse_token(token: str) -> tuple[str, str]:
    """Parse 'base_url|phone_number' format."""
    if "|" in token:
        base_url, phone = token.split("|", 1)
        return base_url.strip().rstrip("/"), phone.strip()
    raise ValueError("Signal token must be 'base_url|phone_number' (pipe-separated)")


class SignalAdapter(BaseAdapter):
    """Signal bot via signal-cli REST API bridge."""

    platform = "signal"

    def __init__(self, token: str) -> None:
        super().__init__()
        self._base_url, self._phone = _parse_token(token)
        self._http: Optional[httpx.AsyncClient] = None
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    # ── Lifecycle ──────────────────────────────────────────

    async def connect(self) -> bool:
        self._http = httpx.AsyncClient(timeout=30)

        # Verify by fetching account info
        try:
            resp = await self._http.get(f"{self._base_url}/v1/about")
            data = resp.json()
            # Also verify the number is registered
            resp2 = await self._http.get(
                f"{self._base_url}/v1/identities/{quote(self._phone, safe='')}"
            )
            if resp2.status_code == 404:
                raise ValueError(f"Phone number {self._phone} not registered with signal-cli")
            logger.info(
                "Signal adapter connected: %s (signal-cli %s)",
                self._phone, data.get("versions", {}).get("signal-cli", "?"),
            )
        except httpx.ConnectError:
            logger.error("Cannot connect to signal-cli REST API at %s", self._base_url)
            await self._http.aclose()
            self._http = None
            return False
        except Exception:
            logger.exception("Signal verification failed")
            await self._http.aclose()
            self._http = None
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
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Sending ────────────────────────────────────────────

    async def send(self, chat_id: str, text: str) -> str | None:
        if not self._http:
            return None
        chunks = chunk_message(text, MAX_MESSAGE_LEN)
        timestamp = None
        for chunk in chunks:
            resp = await self._http.post(
                f"{self._base_url}/v2/send",
                json={
                    "message": chunk,
                    "number": self._phone,
                    "recipients": [chat_id],
                },
            )
            data = resp.json()
            if timestamp is None:
                # signal-cli returns timestamp as message identifier
                timestamp = str(data.get("timestamp", ""))
        return timestamp

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """Signal doesn't support message editing. No-op."""
        pass

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator via signal-cli."""
        if not self._http:
            return
        try:
            await self._http.put(
                f"{self._base_url}/v1/typing-indicator/{quote(self._phone, safe='')}",
                json={"recipient": chat_id},
            )
        except Exception:
            pass

    # ── Polling ────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        delay = RECONNECT_BASE_DELAY
        while self._running:
            try:
                resp = await self._http.get(
                    f"{self._base_url}/v1/receive/{quote(self._phone, safe='')}",
                    timeout=30,
                )
                if resp.status_code == 200:
                    messages = resp.json()
                    for msg in messages:
                        try:
                            await self._process_message(msg)
                        except Exception:
                            logger.exception("Error processing Signal message")
                delay = RECONNECT_BASE_DELAY
                await asyncio.sleep(POLL_INTERVAL)

            except asyncio.CancelledError:
                return
            except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, OSError) as e:
                logger.warning("Signal poll network error (%s), retrying in %.0fs", type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
            except Exception:
                logger.exception("Signal poll error, retrying in %.0fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _process_message(self, msg: dict) -> None:
        envelope = msg.get("envelope", {})
        data_message = envelope.get("dataMessage")
        if not data_message:
            return

        text = (data_message.get("message") or "").strip()
        if not text:
            return

        source = envelope.get("source", "")
        source_name = envelope.get("sourceName") or source

        # Use source number as chat_id (DM) or groupId for groups
        group_info = data_message.get("groupInfo")
        chat_id = group_info.get("groupId", "") if group_info else source

        event = MessageEvent(
            platform="signal",
            chat_id=chat_id,
            sender_id=source,
            sender_name=source_name,
            text=text,
        )

        if self.on_message:
            await self.on_message(event)


async def verify_token(token: str) -> dict:
    """Verify signal-cli REST API connection and phone number."""
    base_url, phone = _parse_token(token)

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{base_url}/v1/about")
            data = resp.json()
        except httpx.ConnectError:
            raise ValueError(f"Cannot connect to signal-cli at {base_url}")

        # Check phone number is registered
        resp2 = await client.get(
            f"{base_url}/v1/identities/{quote(phone, safe='')}"
        )
        if resp2.status_code == 404:
            raise ValueError(f"Phone {phone} not registered with signal-cli")

        return {
            "username": phone,
            "id": phone,
            "version": data.get("versions", {}).get("signal-cli", "unknown"),
        }
