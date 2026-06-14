"""Proxy fallback middleware.

Intercepts requests for sub-resources of proxied dev servers.
When a page is loaded through /api/proxy/{port}/, its sub-resources
(JS modules, CSS, images) use absolute paths like /@vite/client or
/node_modules/... which resolve against the cptr origin.  This
middleware detects those requests via the Referer header and a
path-to-port cache, then proxies them to the correct local port.

The cache chain works like this:
  1. /api/proxy/5174/ loads HTML with <script src="/@vite/client">
  2. Browser requests /@vite/client with Referer containing /api/proxy/5174/
  3. Middleware extracts port 5174, proxies, caches /@vite/client -> 5174
  4. /@vite/client imports /node_modules/foo
  5. Referer is /@vite/client, middleware looks up cache -> port 5174
  6. Chain continues indefinitely
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Regex to extract port from a proxy Referer URL
_PROXY_PORT_RE = re.compile(r"/api/proxy/(\d+)/")

# Paths that should never be intercepted by the proxy middleware
_PASSTHROUGH_PREFIXES = (
    "/api/",
    "/_app/",
    "/socket.io/",
    "/favicon",
)


def _extract_port_from_referer(referer: str) -> int | None:
    """Extract the proxy port from a Referer like /api/proxy/5174/..."""
    m = _PROXY_PORT_RE.search(referer)
    return int(m.group(1)) if m else None


class ProxyFallbackMiddleware:
    """ASGI middleware that intercepts sub-resource requests for proxied dev servers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Never intercept known cptr paths
        if any(path.startswith(prefix) for prefix in _PASSTHROUGH_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Try to resolve a proxy port for this request
        port = self._resolve_port(scope)
        if port is None:
            await self.app(scope, receive, send)
            return

        # Auth check: this middleware runs before the auth middleware (outermost),
        # so we must verify the session ourselves to prevent unauthenticated SSRF.
        if not self._is_authenticated(scope):
            # Not authenticated — fall through to the normal app (which will
            # either serve a page or return 401 via the auth middleware).
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            await self._proxy_http(scope, receive, send, port, path)
        elif scope["type"] == "websocket":
            await self._proxy_websocket(scope, receive, send, port, path)

    @staticmethod
    def _is_authenticated(scope: Scope) -> bool:
        """Check if the request carries a valid cptr session cookie."""
        from cptr.utils.config import check_access

        headers = dict(scope.get("headers", []))
        cookie_header = (headers.get(b"cookie") or b"").decode("latin-1", errors="replace")

        token = None
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("cptr_session="):
                token = part.split("=", 1)[1]
                break

        if not token:
            return False

        client = scope.get("client")
        client_host = client[0] if client else "127.0.0.1"
        return check_access(client_host=client_host, jwt_token=token) is not None

    def _resolve_port(self, scope: Scope) -> int | None:
        """Determine which proxy port this request belongs to."""
        from cptr.routers.proxy import resolve_cached_port

        path: str = scope.get("path", "")

        # 1. Check the path cache (handles chained imports)
        cached = resolve_cached_port(path)
        if cached is not None:
            return cached

        # 2. Check Referer header
        headers = dict(scope.get("headers", []))
        referer_bytes = headers.get(b"referer", b"")
        if referer_bytes:
            referer = referer_bytes.decode("latin-1", errors="replace")

            # Direct: Referer is /api/proxy/{port}/...
            port = _extract_port_from_referer(referer)
            if port:
                return port

            # Indirect: Referer is a previously-cached path
            try:
                ref_path = urlparse(referer).path
            except Exception:
                ref_path = ""
            if ref_path:
                cached = resolve_cached_port(ref_path)
                if cached is not None:
                    return cached

        return None

    async def _proxy_http(
        self, scope: Scope, receive: Receive, send: Send, port: int, path: str
    ) -> None:
        """Proxy an HTTP request through the shared proxy function."""
        from cptr.routers.proxy import proxy_http_request, cache_path

        # Cache this path for future chained requests
        cache_path(path, port)

        request = Request(scope, receive)
        response = await proxy_http_request(port, path.lstrip("/"), request)
        await response(scope, receive, send)

    async def _proxy_websocket(
        self, scope: Scope, receive: Receive, send: Send, port: int, path: str
    ) -> None:
        """Proxy a WebSocket connection to the dev server.

        This handles the case where Vite HMR or similar tools try to
        connect a WebSocket to the page origin (e.g. ws://remote:8000/)
        instead of through /api/proxy/{port}/ws-proxy/.
        """
        import asyncio
        from starlette.websockets import WebSocket

        websocket = WebSocket(scope, receive, send)
        await websocket.accept()

        # Build query string
        qs_bytes = scope.get("query_string", b"")
        qs = f"?{qs_bytes.decode()}" if qs_bytes else ""

        upstream_ws = None
        for host in ("127.0.0.1", "[::1]"):
            ws_url = f"ws://{host}:{port}{path}{qs}"
            try:
                import websockets

                upstream_ws = await asyncio.wait_for(
                    websockets.connect(ws_url, additional_headers={"host": f"127.0.0.1:{port}"}),
                    timeout=5.0,
                )
                break
            except Exception:
                continue

        if upstream_ws is None:
            await websocket.close(code=4002, reason=f"Cannot connect to localhost:{port}")
            return

        async def client_to_upstream():
            try:
                while True:
                    data = await websocket.receive()
                    if "text" in data:
                        await upstream_ws.send(data["text"])
                    elif "bytes" in data:
                        await upstream_ws.send(data["bytes"])
                    else:
                        break
            except Exception:
                pass

        async def upstream_to_client():
            try:
                async for msg in upstream_ws:
                    if isinstance(msg, str):
                        await websocket.send_text(msg)
                    elif isinstance(msg, bytes):
                        await websocket.send_bytes(msg)
            except Exception:
                pass

        try:
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        finally:
            try:
                await upstream_ws.close()
            except Exception:
                pass
            try:
                await websocket.close()
            except Exception:
                pass
