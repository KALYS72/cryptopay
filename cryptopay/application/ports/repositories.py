"""Порты хранилища (паттерн Repository) и единица работы (Unit of Work).

Прикладной слой знает только эти абстрактные классы. Реализаций две —
PostgreSQL и память процесса — и они взаимозаменяемы (SOLID L): обе проходят
один и тот же набор контрактных тестов (tests/contract/repository_contract.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar

from ...domain.checkout_session import CheckoutSession
from ...domain.ledger import BalanceTransaction
from ...domain.merchant import Merchant
from ...domain.payment_intent import PaymentIntent, PaymentIntentStatus
from ...domain.refund import Refund
from ...domain.webhook_endpoint import WebhookEndpoint

T = TypeVar("T")

DEFAULT_PAGE_LIMIT = 10
MAX_PAGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ListQuery:
    """Курсорная пагинация, как в Stripe: ``limit`` и ``starting_after=<id последнего объекта>``.

    Объекты всегда идут от новых к старым. В отличие от OFFSET, курсор не «съезжает»,
    если пока листаешь страницы, появляются новые платежи.
    """

    limit: int = DEFAULT_PAGE_LIMIT
    starting_after: str | None = None


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: list[T]
    has_more: bool


# ───────────────────────────── записи без собственного поведения ─────────────────────────────


@dataclass(frozen=True, slots=True)
class EventRecord:
    """Событие в журнале (объект Event в Stripe): что случилось и как выглядел объект в этот момент."""

    id: str
    merchant_id: str
    type: str
    subject_id: str
    data: dict[str, Any]
    """JSON объекта, с которым произошло событие (data.object)."""
    created_at: datetime


DeliveryStatus = Literal["pending", "delivered", "failed"]


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """Запись Transactional Outbox: «это событие нужно доставить на этот адрес».

    Пишется в той же транзакции БД, что и само изменение, поэтому событие
    не потеряется, даже если сервер упадёт сразу после коммита.
    """

    id: str
    merchant_id: str
    event_id: str
    endpoint_id: str
    event_type: str
    url: str
    status: DeliveryStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    last_status_code: int | None
    delivered_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Сохранённый ответ на запрос с заголовком Idempotency-Key."""

    merchant_id: str
    key: str
    request_hash: str
    """Хэш метода, пути и тела: тот же ключ с другими параметрами — ошибка."""
    status: Literal["processing", "completed"]
    response_status: int | None
    response_body: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DailyVolume:
    day: date
    currency: str
    amount: int


# ─────────────────────────────────────── репозитории ───────────────────────────────────────


class MerchantRepository(ABC):
    @abstractmethod
    async def insert(self, merchant: Merchant) -> None:
        """ConflictError, если e-mail уже занят."""

    @abstractmethod
    async def update(self, merchant: Merchant) -> None: ...

    @abstractmethod
    async def find_by_id(self, id: str) -> Merchant | None: ...

    @abstractmethod
    async def find_by_email(self, email: str) -> Merchant | None: ...

    @abstractmethod
    async def find_by_api_key_hash(self, key_hash: str) -> Merchant | None: ...


class PaymentIntentRepository(ABC):
    @abstractmethod
    async def insert(self, intent: PaymentIntent) -> None: ...

    @abstractmethod
    async def update(self, intent: PaymentIntent) -> None:
        """Оптимистическая блокировка: ConcurrencyError, если версия в базе уже другая."""

    @abstractmethod
    async def find_by_id(self, id: str) -> PaymentIntent | None: ...

    @abstractmethod
    async def page(
        self, merchant_id: str, query: ListQuery, status: PaymentIntentStatus | None = None
    ) -> Page[PaymentIntent]: ...

    @abstractmethod
    async def list_waiting_for_wallet(self, limit: int) -> list[PaymentIntent]:
        """Намерения в requires_action и processing — их судьбу фоновая задача узнаёт у кошелька."""

    @abstractmethod
    async def count_by_status(self, merchant_id: str) -> dict[PaymentIntentStatus, int]: ...


class CheckoutSessionRepository(ABC):
    @abstractmethod
    async def insert(self, session: CheckoutSession) -> None: ...

    @abstractmethod
    async def update(self, session: CheckoutSession) -> None: ...

    @abstractmethod
    async def find_by_id(self, id: str) -> CheckoutSession | None: ...

    @abstractmethod
    async def page(self, merchant_id: str, query: ListQuery) -> Page[CheckoutSession]: ...

    @abstractmethod
    async def find_past_expiry(self, now: datetime, limit: int) -> list[CheckoutSession]: ...


class RefundRepository(ABC):
    @abstractmethod
    async def insert(self, refund: Refund) -> None: ...

    @abstractmethod
    async def update(self, refund: Refund) -> None: ...

    @abstractmethod
    async def find_by_id(self, id: str) -> Refund | None: ...

    @abstractmethod
    async def page(self, merchant_id: str, query: ListQuery, payment_intent_id: str | None = None) -> Page[Refund]: ...

    @abstractmethod
    async def list_pending(self, limit: int) -> list[Refund]: ...


class LedgerRepository(ABC):
    """Журнал проводок. Только добавление: проводки не изменяются и не удаляются."""

    @abstractmethod
    async def append(self, entry: BalanceTransaction) -> None: ...

    @abstractmethod
    async def page(self, merchant_id: str, query: ListQuery) -> Page[BalanceTransaction]: ...

    @abstractmethod
    async def balances(self, merchant_id: str) -> dict[str, int]:
        """Сумма net по каждой валюте — это и есть доступный баланс."""

    @abstractmethod
    async def daily_volume(self, merchant_id: str, since: datetime) -> list[DailyVolume]:
        """Оборот (сумма успешных платежей) по дням — для графика на главной кабинета."""


class EventRepository(ABC):
    @abstractmethod
    async def append(self, event: EventRecord) -> None: ...

    @abstractmethod
    async def find_by_id(self, id: str) -> EventRecord | None: ...

    @abstractmethod
    async def page(
        self, merchant_id: str, query: ListQuery, *, type: str | None = None, subject_id: str | None = None
    ) -> Page[EventRecord]: ...


class WebhookEndpointRepository(ABC):
    @abstractmethod
    async def insert(self, endpoint: WebhookEndpoint) -> None: ...

    @abstractmethod
    async def delete(self, id: str) -> None: ...

    @abstractmethod
    async def find_by_id(self, id: str) -> WebhookEndpoint | None: ...

    @abstractmethod
    async def list_by_merchant(self, merchant_id: str) -> list[WebhookEndpoint]: ...


class WebhookOutbox(ABC):
    @abstractmethod
    async def enqueue(self, delivery: WebhookDelivery) -> None: ...

    @abstractmethod
    async def find_due(self, now: datetime, limit: int) -> list[WebhookDelivery]: ...

    @abstractmethod
    async def save(self, delivery: WebhookDelivery) -> None: ...

    @abstractmethod
    async def list_by_event(self, event_id: str) -> list[WebhookDelivery]: ...


class IdempotencyStore(ABC):
    @abstractmethod
    async def try_begin(self, record: IdempotencyRecord) -> IdempotencyRecord | None:
        """Атомарно занять ключ. None — ключ свободен и теперь наш; иначе — уже существующая запись."""

    @abstractmethod
    async def complete(self, merchant_id: str, key: str, status: int, body: str) -> None: ...

    @abstractmethod
    async def release(self, merchant_id: str, key: str) -> None:
        """Освободить ключ, если запрос завершился ошибкой: повтор с тем же ключом снова выполнится."""

    @abstractmethod
    async def delete_older_than(self, cutoff: datetime) -> int: ...


@dataclass(frozen=True, slots=True)
class Repositories:
    merchants: MerchantRepository
    payment_intents: PaymentIntentRepository
    checkout_sessions: CheckoutSessionRepository
    refunds: RefundRepository
    ledger: LedgerRepository
    events: EventRepository
    webhook_endpoints: WebhookEndpointRepository
    outbox: WebhookOutbox
    idempotency: IdempotencyStore


class UnitOfWork(ABC):
    """Единица работы: всё внутри ``async with uow.transaction() as repos:`` либо сохраняется
    целиком (COMMIT), либо откатывается целиком (ROLLBACK), если внутри возникло исключение.
    """

    @property
    @abstractmethod
    def repos(self) -> Repositories:
        """Репозитории вне транзакции — для чтения и одиночных операций."""

    @abstractmethod
    def transaction(self) -> AbstractAsyncContextManager[Repositories]: ...


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    return max(1, min(MAX_PAGE_LIMIT, limit))
