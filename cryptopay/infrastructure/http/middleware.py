"""ASGI-middleware: заголовки безопасности, лимит тела запроса, журнал запросов, ограничение частоты."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable

from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import BodyTooLargeError, RateLimitedError, error_response

logger = logging.getLogger("cryptopay.http")

# Строгая политика безопасности контента: скрипты только с нашего домена, никаких inline-скриптов.
_CSP_APP = (
    "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'"
)
# Swagger UI рисует часть интерфейса inline-стилями — для /docs разрешаем только их.
_CSP_DOCS = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
)

_COMMON_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"origin-agent-cluster", b"?1"),
    (b"x-dns-prefetch-control", b"off"),
    (b"x-permitted-cross-domain-policies", b"none"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
]


class SecurityHeadersMiddleware:
    """Аналог helmet: CSP, HSTS (только в production), запрет встраивания в iframe и т. д."""

    def __init__(self, app: ASGIApp, *, production: bool) -> None:
        self.app = app
        self.production = production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = _CSP_DOCS if scope["path"].rstrip("/") == "/docs" else _CSP_APP
        if self.production:
            csp += "; upgrade-insecure-requests"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                headers += [*_COMMON_HEADERS, (b"content-security-policy", csp.encode())]
                if self.production:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Тело запроса не больше max_bytes (32 КБ): защита от «тяжёлых» запросов."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            # Обработчики ошибок FastAPI работают глубже middleware — отвечаем сами.
            await error_response(413, "invalid_request_error", "request_too_large", "Слишком большой запрос")(
                scope, receive, send
            )
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLargeError()
            return message

        await self.app(scope, limited_receive, send)


class AccessLogMiddleware:
    """Одна строка лога на запрос: метод, путь, статус, длительность. /healthz и статику не пишем."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if scope["type"] != "http" or path == "/healthz" or path.startswith("/assets/"):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
            logger.log(
                level,
                "request",
                extra={
                    "method": scope.get("method"),
                    "url": path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )


def client_ip(request: Request, trust_proxy: int) -> str:
    """IP клиента с учётом обратных прокси (как trust proxy = N в Express).

    Доверяем только N последним звеньям цепочки X-Forwarded-For: подделать их
    клиент не может, их дописывают прокси Render/Caddy.
    """
    peer = request.client.host if request.client else "unknown"
    if trust_proxy <= 0:
        return peer
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    chain = [*forwarded, peer]
    return chain[max(0, len(chain) - 1 - trust_proxy)]


class RateLimiter:
    """Ограничение частоты запросов с одного IP: окно в 60 секунд (зависимость FastAPI)."""

    WINDOW_SECONDS = 60.0

    def __init__(
        self, per_minute: int, *, enabled: bool, trust_proxy: int, clock: Callable[[], float] = time.monotonic
    ):
        self.per_minute = per_minute
        self.enabled = enabled
        self.trust_proxy = trust_proxy
        self._clock = clock
        self._hits: dict[str, tuple[float, int]] = {}

    def __call__(self, request: Request) -> None:
        if not self.enabled:
            return
        now = self._clock()
        ip = client_ip(request, self.trust_proxy)
        started, count = self._hits.get(ip, (now, 0))
        if now - started >= self.WINDOW_SECONDS:
            started, count = now, 0
        count += 1
        self._hits[ip] = (started, count)
        if len(self._hits) > 10_000:  # не даём словарю расти бесконечно
            self._hits = {k: v for k, v in self._hits.items() if now - v[0] < self.WINDOW_SECONDS}
        if count > self.per_minute:
            raise RateLimitedError(retry_after=max(1, math.ceil(self.WINDOW_SECONDS - (now - started))))
