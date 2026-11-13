"""Хранилище в памяти процесса.

Нужно для двух вещей: 1) быстрые тесты без PostgreSQL; 2) запуск демо одной командой
без базы данных. Записи хранятся как копии-снимки: изменить объект «мимо» репозитория нельзя.

SOLID (L): репозитории ведут себя так же, как PostgreSQL-версии (уникальность e-mail,
версии, курсорная пагинация, откат транзакции) — это проверяет общий набор контрактных
тестов tests/contract/repository_contract.py.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import TypeVar

from ...application.ports.repositories import (
    CheckoutSessionRepository,
    DailyVolume,
    EventRecord,
    EventRepository,
    IdempotencyRecord,
    IdempotencyStore,
    LedgerRepository,
    ListQuery,
    MerchantRepository,
    Page,
    PaymentIntentRepository,
    RefundRepository,
    Repositories,
    UnitOfWork,
    WebhookDelivery,
    WebhookEndpointRepository,
    WebhookOutbox,
)
from ...domain.checkout_session import CheckoutSession, CheckoutSessionProps
from ...domain.errors import ConcurrencyError, ConflictError
from ...domain.ledger import BalanceTransaction
from ...domain.merchant import Merchant, MerchantProps
from ...domain.payment_intent import (
    PAYMENT_INTENT_STATUSES,
    PaymentIntent,
    PaymentIntentProps,
    PaymentIntentStatus,
)
from ...domain.refund import Refund, RefundProps
from ...domain.webhook_endpoint import WebhookEndpoint

T = TypeVar("T")
R = TypeVar("R")


@dataclass(slots=True)
class InMemoryState:
    merchants: dict[str, MerchantProps] = field(default_factory=dict)
    intents: dict[str, PaymentIntentProps] = field(default_factory=dict)
    sessions: dict[str, CheckoutSessionProps] = field(default_factory=dict)
    refunds: dict[str, RefundProps] = field(default_factory=dict)
    ledger: list[BalanceTransaction] = field(default_factory=list)
    events: dict[str, EventRecord] = field(default_factory=dict)
    endpoints: dict[str, WebhookEndpoint] = field(default_factory=dict)
    outbox: dict[str, WebhookDelivery] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRecord] = field(default_factory=dict)


class InMemoryDatabase:
    def __init__(self) -> None:
        self.state = InMemoryState()

    def snapshot(self) -> InMemoryState:
        """Снимок для отката транзакции. Записи — неизменяемые копии, поэтому достаточно копий словарей."""
        s = self.state
        return InMemoryState(
            merchants=dict(s.merchants),
            intents=dict(s.intents),
            sessions=dict(s.sessions),
            refunds=dict(s.refunds),
            ledger=list(s.ledger),
            events=dict(s.events),
            endpoints=dict(s.endpoints),
            outbox=dict(s.outbox),
            idempotency=dict(s.idempotency),
        )

    def restore(self, saved: InMemoryState) -> None:
        self.state = saved


def _newest_first(items: Iterable[T], created_at: Callable[[T], datetime], id: Callable[[T], str]) -> list[T]:
    return sorted(items, key=lambda item: (created_at(item), id(item)), reverse=True)


def _page(ordered: list[T], query: ListQuery, id: Callable[[T], str], restore: Callable[[T], R]) -> Page[R]:
    """Страница после курсора starting_after (как в Stripe). Неизвестный курсор — пустая страница."""
    if query.starting_after is not None:
        ids = [id(item) for item in ordered]
        ordered = ordered[ids.index(query.starting_after) + 1 :] if query.starting_after in ids else []
    return Page(items=[restore(item) for item in ordered[: query.limit]], has_more=len(ordered) > query.limit)


class InMemoryMerchantRepository(MerchantRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def insert(self, merchant: Merchant) -> None:
        s = merchant.snapshot()
        if s.id in self._db.state.merchants or any(m.email == s.email for m in self._db.state.merchants.values()):
            raise ConflictError("Аккаунт с таким e-mail уже есть", code="email_taken", param="email")
        self._db.state.merchants[s.id] = s

    async def update(self, merchant: Merchant) -> None:
        self._db.state.merchants[merchant.id] = merchant.snapshot()

    async def find_by_id(self, id: str) -> Merchant | None:
        props = self._db.state.merchants.get(id)
        return Merchant.restore(props) if props else None

    async def find_by_email(self, email: str) -> Merchant | None:
        props = next((m for m in self._db.state.merchants.values() if m.email == email), None)
        return Merchant.restore(props) if props else None

    async def find_by_api_key_hash(self, key_hash: str) -> Merchant | None:
        props = next((m for m in self._db.state.merchants.values() if m.api_key_hash == key_hash), None)
        return Merchant.restore(props) if props else None


class InMemoryPaymentIntentRepository(PaymentIntentRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def insert(self, intent: PaymentIntent) -> None:
        if intent.id in self._db.state.intents:
            raise ConflictError(f"Платёж {intent.id} уже существует")
        self._db.state.intents[intent.id] = intent.snapshot()

    async def update(self, intent: PaymentIntent) -> None:
        stored = self._db.state.intents.get(intent.id)
        if stored is None or stored.version != intent.version:
            raise ConcurrencyError("Платёж", intent.id)
        s = intent.snapshot()
        if s.wallet_tx_id is not None and any(
            p.wallet_tx_id == s.wallet_tx_id and p.id != s.id for p in self._db.state.intents.values()
        ):
            raise ConflictError("Эта транзакция уже засчитана другому платежу", code="transaction_reused")
        self._db.state.intents[intent.id] = replace(s, version=intent.version + 1)
        intent.bump_version()

    async def find_by_id(self, id: str) -> PaymentIntent | None:
        props = self._db.state.intents.get(id)
        return PaymentIntent.restore(props) if props else None

    async def page(
        self, merchant_id: str, query: ListQuery, status: PaymentIntentStatus | None = None
    ) -> Page[PaymentIntent]:
        matching = (
            p
            for p in self._db.state.intents.values()
            if p.merchant_id == merchant_id and (status is None or p.status == status)
        )
        ordered = _newest_first(matching, lambda p: p.created_at, lambda p: p.id)
        return _page(ordered, query, lambda p: p.id, PaymentIntent.restore)

    async def list_waiting_for_wallet(self, limit: int) -> list[PaymentIntent]:
        waiting = sorted(
            (p for p in self._db.state.intents.values() if p.status in ("requires_action", "processing")),
            key=lambda p: (p.created_at, p.id),
        )
        return [PaymentIntent.restore(p) for p in waiting[:limit]]

    async def count_by_status(self, merchant_id: str) -> dict[PaymentIntentStatus, int]:
        counts: dict[PaymentIntentStatus, int] = dict.fromkeys(PAYMENT_INTENT_STATUSES, 0)
        for p in self._db.state.intents.values():
            if p.merchant_id == merchant_id:
                counts[p.status] += 1
        return counts


class InMemoryCheckoutSessionRepository(CheckoutSessionRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def insert(self, session: CheckoutSession) -> None:
        if session.id in self._db.state.sessions:
            raise ConflictError(f"Сессия {session.id} уже существует")
        self._db.state.sessions[session.id] = session.snapshot()

    async def update(self, session: CheckoutSession) -> None:
        self._db.state.sessions[session.id] = session.snapshot()

    async def find_by_id(self, id: str) -> CheckoutSession | None:
        props = self._db.state.sessions.get(id)
        return CheckoutSession.restore(props) if props else None

    async def page(self, merchant_id: str, query: ListQuery) -> Page[CheckoutSession]:
        matching = (s for s in self._db.state.sessions.values() if s.merchant_id == merchant_id)
        ordered = _newest_first(matching, lambda s: s.created_at, lambda s: s.id)
        return _page(ordered, query, lambda s: s.id, CheckoutSession.restore)

    async def find_past_expiry(self, now: datetime, limit: int) -> list[CheckoutSession]:
        due = sorted(
            (s for s in self._db.state.sessions.values() if s.status == "open" and s.expires_at <= now),
            key=lambda s: (s.expires_at, s.id),
        )
        return [CheckoutSession.restore(s) for s in due[:limit]]


class InMemoryRefundRepository(RefundRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def insert(self, refund: Refund) -> None:
        if refund.id in self._db.state.refunds:
            raise ConflictError(f"Возврат {refund.id} уже существует")
        self._db.state.refunds[refund.id] = refund.snapshot()

    async def update(self, refund: Refund) -> None:
        self._db.state.refunds[refund.id] = refund.snapshot()

    async def find_by_id(self, id: str) -> Refund | None:
        props = self._db.state.refunds.get(id)
        return Refund.restore(props) if props else None

    async def page(self, merchant_id: str, query: ListQuery, payment_intent_id: str | None = None) -> Page[Refund]:
        matching = (
            r
            for r in self._db.state.refunds.values()
            if r.merchant_id == merchant_id and (payment_intent_id is None or r.payment_intent_id == payment_intent_id)
        )
        ordered = _newest_first(matching, lambda r: r.created_at, lambda r: r.id)
        return _page(ordered, query, lambda r: r.id, Refund.restore)

    async def list_pending(self, limit: int) -> list[Refund]:
        pending = sorted(
            (r for r in self._db.state.refunds.values() if r.status == "pending"), key=lambda r: (r.created_at, r.id)
        )
        return [Refund.restore(r) for r in pending[:limit]]


class InMemoryLedgerRepository(LedgerRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def append(self, entry: BalanceTransaction) -> None:
        duplicate_payment = entry.type == "payment" and any(
            e.type == "payment" and e.source_id == entry.source_id for e in self._db.state.ledger
        )
        if duplicate_payment or any(e.id == entry.id for e in self._db.state.ledger):
            raise ConflictError("Проводка по этому платежу уже есть", code="duplicate_entry")
        self._db.state.ledger.append(entry)

    async def page(self, merchant_id: str, query: ListQuery) -> Page[BalanceTransaction]:
        matching = (e for e in self._db.state.ledger if e.merchant_id == merchant_id)
        ordered = _newest_first(matching, lambda e: e.created_at, lambda e: e.id)
        return _page(ordered, query, lambda e: e.id, lambda e: e)

    async def balances(self, merchant_id: str) -> dict[str, int]:
        totals: dict[str, int] = {}
        for e in self._db.state.ledger:
            if e.merchant_id == merchant_id:
                totals[e.currency] = totals.get(e.currency, 0) + e.net
        return totals

    async def daily_volume(self, merchant_id: str, since: datetime) -> list[DailyVolume]:
        totals: dict[tuple[date, str], int] = {}
        for e in self._db.state.ledger:
            if e.merchant_id == merchant_id and e.type == "payment" and e.created_at >= since:
                key = (e.created_at.astimezone(UTC).date(), e.currency)
                totals[key] = totals.get(key, 0) + e.amount
        return [DailyVolume(day=d, currency=c, amount=a) for (d, c), a in sorted(totals.items())]


class InMemoryEventRepository(EventRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def append(self, event: EventRecord) -> None:
        self._db.state.events[event.id] = replace(event, data=copy.deepcopy(event.data))

    async def find_by_id(self, id: str) -> EventRecord | None:
        event = self._db.state.events.get(id)
        return replace(event, data=copy.deepcopy(event.data)) if event else None

    async def page(
        self, merchant_id: str, query: ListQuery, *, type: str | None = None, subject_id: str | None = None
    ) -> Page[EventRecord]:
        matching = (
            e
            for e in self._db.state.events.values()
            if e.merchant_id == merchant_id
            and (type is None or e.type == type)
            and (subject_id is None or e.subject_id == subject_id)
        )
        ordered = _newest_first(matching, lambda e: e.created_at, lambda e: e.id)
        return _page(ordered, query, lambda e: e.id, lambda e: replace(e, data=copy.deepcopy(e.data)))


class InMemoryWebhookEndpointRepository(WebhookEndpointRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def insert(self, endpoint: WebhookEndpoint) -> None:
        self._db.state.endpoints[endpoint.id] = endpoint

    async def delete(self, id: str) -> None:
        self._db.state.endpoints.pop(id, None)

    async def find_by_id(self, id: str) -> WebhookEndpoint | None:
        return self._db.state.endpoints.get(id)

    async def list_by_merchant(self, merchant_id: str) -> list[WebhookEndpoint]:
        return sorted(
            (e for e in self._db.state.endpoints.values() if e.merchant_id == merchant_id),
            key=lambda e: (e.created_at, e.id),
        )


class InMemoryWebhookOutbox(WebhookOutbox):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def enqueue(self, delivery: WebhookDelivery) -> None:
        self._db.state.outbox[delivery.id] = delivery

    async def find_due(self, now: datetime, limit: int) -> list[WebhookDelivery]:
        due = sorted(
            (d for d in self._db.state.outbox.values() if d.status == "pending" and d.next_attempt_at <= now),
            key=lambda d: (d.next_attempt_at, d.id),
        )
        return due[:limit]

    async def save(self, delivery: WebhookDelivery) -> None:
        self._db.state.outbox[delivery.id] = delivery

    async def list_by_event(self, event_id: str) -> list[WebhookDelivery]:
        return sorted(
            (d for d in self._db.state.outbox.values() if d.event_id == event_id), key=lambda d: (d.created_at, d.id)
        )


class InMemoryIdempotencyStore(IdempotencyStore):
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    async def try_begin(self, record: IdempotencyRecord) -> IdempotencyRecord | None:
        key = (record.merchant_id, record.key)
        existing = self._db.state.idempotency.get(key)
        if existing is not None:
            return existing
        self._db.state.idempotency[key] = record
        return None

    async def complete(self, merchant_id: str, key: str, status: int, body: str) -> None:
        stored = self._db.state.idempotency.get((merchant_id, key))
        if stored is not None:
            self._db.state.idempotency[(merchant_id, key)] = replace(
                stored, status="completed", response_status=status, response_body=body
            )

    async def release(self, merchant_id: str, key: str) -> None:
        self._db.state.idempotency.pop((merchant_id, key), None)

    async def delete_older_than(self, cutoff: datetime) -> int:
        old = [k for k, r in self._db.state.idempotency.items() if r.created_at < cutoff]
        for k in old:
            del self._db.state.idempotency[k]
        return len(old)


def create_in_memory_repositories(db: InMemoryDatabase) -> Repositories:
    return Repositories(
        merchants=InMemoryMerchantRepository(db),
        payment_intents=InMemoryPaymentIntentRepository(db),
        checkout_sessions=InMemoryCheckoutSessionRepository(db),
        refunds=InMemoryRefundRepository(db),
        ledger=InMemoryLedgerRepository(db),
        events=InMemoryEventRepository(db),
        webhook_endpoints=InMemoryWebhookEndpointRepository(db),
        outbox=InMemoryWebhookOutbox(db),
        idempotency=InMemoryIdempotencyStore(db),
    )


class InMemoryUnitOfWork(UnitOfWork):
    """Транзакции в памяти: выполняются по очереди (как SERIALIZABLE),
    а при ошибке восстанавливается снимок состояния — это и есть ROLLBACK.
    """

    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self._db = db or InMemoryDatabase()
        self._repos = create_in_memory_repositories(self._db)
        self._lock = asyncio.Lock()

    @property
    def repos(self) -> Repositories:
        return self._repos

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Repositories]:
        async with self._lock:
            saved = self._db.snapshot()
            try:
                yield self._repos
            except BaseException:
                self._db.restore(saved)
                raise
