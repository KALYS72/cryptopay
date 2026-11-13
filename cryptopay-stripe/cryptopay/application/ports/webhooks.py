from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    url: str
    body: str
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    ok: bool
    status: int | None
    error: str | None


class WebhookSender(ABC):
    """Отправка HTTP-запроса на сервер продавца."""

    @abstractmethod
    async def send(self, request: WebhookRequest) -> WebhookResponse: ...


class WebhookSigner(ABC):
    """Подпись тела вебхука: продавец проверяет её своим секретом."""

    @abstractmethod
    def sign(self, secret: str, timestamp: int, body: str) -> str: ...


class WebhookUrlPolicy(ABC):
    """Проверка, что URL вебхука безопасен (https, не внутренняя сеть). Бросает ValidationError."""

    @abstractmethod
    def validate(self, url: str) -> None: ...
