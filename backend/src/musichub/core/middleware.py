"""Request-ID und Access-Log als reine ASGI-Middleware (kein BaseHTTPMiddleware-Overhead)."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from musichub.core.logging import correlation_id

log = logging.getLogger("musichub.access")

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HEADER = b"x-request-id"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(HEADER, b"").decode("latin-1")
        request_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        token = correlation_id.set(request_id)
        # Auch für den äußersten Fehler-Handler (läuft nach dem Zurücksetzen des Kontexts)
        scope["musichub.request_id"] = request_id
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [h for h in message.get("headers", []) if h[0].lower() != HEADER]
                headers.append((HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            # Health-Checks fluten sonst das Log.
            level = logging.DEBUG if path.startswith("/api/health") else logging.INFO
            log.log(
                level,
                "%s %s -> %s (%.1f ms)",
                scope.get("method"),
                path,
                status,
                duration_ms,
                extra={"http_status": status, "duration_ms": duration_ms, "path": path},
            )
            correlation_id.reset(token)


class SecurityHeadersMiddleware:
    """Sicherheits-Header für alle API-Antworten (zusätzlich zu nginx im Frontend).

    API-Antworten enthalten persönliche Daten und dürfen nicht zwischengespeichert werden.
    """

    _HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        no_store = path.startswith(("/api/v1", "/api/auth", "/api/oauth"))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {h[0].lower() for h in headers}
                headers.extend(h for h in self._HEADERS if h[0] not in present)
                if no_store and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
