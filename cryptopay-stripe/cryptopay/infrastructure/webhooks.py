from __future__ import annotations

import hashlib
import hmac
import ipaddress
from urllib.parse import urlsplit

import httpx

from ..application.ports.webhooks import (
    WebhookRequest,
    WebhookResponse,
    WebhookSender,
    WebhookSigner,
    WebhookUrlPolicy,
)
from ..domain.errors import ValidationError


class HttpxWebhookSender(WebhookSender):
    """HTTP-отправка вебхука через httpx."""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    async def send(self, request: WebhookRequest) -> WebhookResponse:
        try:
            # Редиректы не выполняем: иначе внешний сервер мог бы перенаправить нас во внутреннюю сеть.
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.post(request.url, content=request.body.encode(), headers=request.headers)
        except httpx.TimeoutException:
            return WebhookResponse(ok=False, status=None, error=f"Сервер продавца не ответил за {self._timeout:g} с")
        except httpx.HTTPError as error:
            return WebhookResponse(ok=False, status=None, error=f"Не удалось подключиться: {error}")
        ok = 200 <= response.status_code < 300
        return WebhookResponse(
            ok=ok,
            status=response.status_code,
            error=None if ok else f"Сервер продавца ответил HTTP {response.status_code}",
        )


class HmacSha256WebhookSigner(WebhookSigner):
    def sign(self, secret: str, timestamp: int, body: str) -> str:
        return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "127.0.0.0/8",
        "0.0.0.0/8",
        "169.254.0.0/16",  # метаданные облака (169.254.169.254)
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # carrier-grade NAT
        "::1/128",
        "::/128",
        "fc00::/7",
        "fe80::/10",
        "::ffff:0:0/96",  # IPv4, записанный как IPv6: ::ffff:127.0.0.1
    )
)


def is_private_host(hostname: str) -> bool:
    host = hostname.strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address.version == net.version and address in net for net in _PRIVATE_NETWORKS)


class DefaultWebhookUrlPolicy(WebhookUrlPolicy):
    """Защита от SSRF.

    Продавец не должен заставить наш сервер стучаться во внутренние адреса
    (метаданные облака 169.254.169.254, localhost и т. п.).
    """

    def __init__(self, allow_insecure: bool) -> None:
        self._allow_insecure = allow_insecure

    def validate(self, url: str) -> None:
        if len(url) > 500:
            raise ValidationError("URL вебхука слишком длинный", param="url")
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            _ = parts.port  # некорректный порт → ValueError
        except ValueError:
            raise ValidationError("Некорректный URL вебхука", param="url") from None
        if not parts.scheme or not hostname or " " in url:
            raise ValidationError("Некорректный URL вебхука", param="url")
        http_allowed = self._allow_insecure and parts.scheme == "http"
        if parts.scheme != "https" and not http_allowed:
            raise ValidationError("URL вебхука должен начинаться с https://", param="url")
        if parts.username or parts.password:
            raise ValidationError("Не указывайте логин и пароль в URL вебхука", param="url")
        if not self._allow_insecure and is_private_host(hostname):
            raise ValidationError("Вебхук не может указывать на локальный или внутренний адрес", param="url")
