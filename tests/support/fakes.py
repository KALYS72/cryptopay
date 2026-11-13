"""ТЕСТОВЫЕ ДВОЙНИКИ (test doubles).

Благодаря тому, что сервисы зависят от абстракций (SOLID: D), в тестах вместо
настоящих часов, генератора id, HTTP-клиента и медленного scrypt подставляются
эти простые классы — тесты идут миллисекунды и без сети.

Кошелёк в тестах — настоящий MockWallet: он и так имитатор, а его «блокчейн»
двигается вместе с подменными часами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cryptopay.application.ports.security import PasswordHasher
from cryptopay.application.ports.system import Clock, IdGenerator, IdPrefix
from cryptopay.application.ports.webhooks import WebhookRequest, WebhookResponse, WebhookSender

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


class FakeClock(Clock):
    def __init__(self, start: datetime = T0) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta

    def advance_seconds(self, seconds: float) -> None:
        self.advance(timedelta(seconds=seconds))

    def advance_minutes(self, minutes: float) -> None:
        self.advance(timedelta(minutes=minutes))


class SequentialIds(IdGenerator):
    """Предсказуемые id: pi_test000000000001, cs_test000000000002… — легко читать в проверках."""

    def __init__(self) -> None:
        self._counter = 0

    def generate(self, prefix: IdPrefix) -> str:
        self._counter += 1
        return f"{prefix}_test{self._counter:012d}"


class PlainPasswordHasher(PasswordHasher):
    """Вместо медленного scrypt — простая обёртка: в тестах важна логика, а не стойкость хэша."""

    def hash(self, password: str) -> str:
        return f"plain${password}"

    def verify(self, password: str, password_hash: str) -> bool:
        return password_hash == f"plain${password}"


@dataclass
class RecordingWebhookSender(WebhookSender):
    """Записывает отправленные вебхуки; умеет «ронять» сервер магазина."""

    sent: list[WebhookRequest] = field(default_factory=list)
    fail_with_status: int | None = None

    async def send(self, request: WebhookRequest) -> WebhookResponse:
        self.sent.append(request)
        if self.fail_with_status is not None:
            return WebhookResponse(ok=False, status=self.fail_with_status, error=f"HTTP {self.fail_with_status}")
        return WebhookResponse(ok=True, status=200, error=None)

    def event_types(self) -> list[str]:
        return [r.headers["cryptopay-event"] for r in self.sent]
