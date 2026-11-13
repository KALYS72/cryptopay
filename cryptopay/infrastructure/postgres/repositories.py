"""Репозитории PostgreSQL.

SOLID (S): здесь только SQL и перевод строк таблиц в сущности — бизнес-правил нет.
SOLID (L): взаимозаменяемы с реализацией в памяти (общие контрактные тесты).
Все запросы параметризованы (%s) — SQL-инъекция невозможна.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

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
    PaymentError,
    PaymentIntent,
    PaymentIntentProps,
    PaymentIntentStatus,
)
from ...domain.refund import Refund, RefundProps
from ...domain.webhook_endpoint import WebhookEndpoint
from .database import ConnectionQueryable, PoolQueryable, Queryable, Row, is_unique_violation

T = TypeVar("T")


def _page(rows: list[Row], query: ListQuery, to_item: Callable[[Row], T]) -> Page[T]:
    """Запрашиваем на одну строку больше лимита: если она пришла — есть следующая страница."""
    return Page(items=[to_item(r) for r in rows[: query.limit]], has_more=len(rows) > query.limit)


# ───────────────────────── маппинг строк таблиц в сущности ─────────────────────────


def _to_merchant(r: Row) -> Merchant:
    return Merchant.restore(
        MerchantProps(
            id=r["id"],
            email=r["email"],
            business_name=r["business_name"],
            password_hash=r["password_hash"],
            api_key_hash=r["api_key_hash"],
            api_key_last4=r["api_key_last4"],
            api_key_created_at=r["api_key_created_at"],
            created_at=r["created_at"],
        )
    )


def _to_intent(r: Row) -> PaymentIntent:
    error = PaymentError(r["last_error_code"], r["last_error_message"] or "") if r["last_error_code"] else None
    return PaymentIntent.restore(
        PaymentIntentProps(
            id=r["id"],
            merchant_id=r["merchant_id"],
            amount=int(r["amount"]),  # NUMERIC(78,0) приходит как Decimal — переводим в точный int
            currency=r["currency"],
            description=r["description"],
            metadata=dict(r["metadata"]),
            status=r["status"],
            checkout_session_id=r["checkout_session_id"],
            customer_email=r["customer_email"],
            wallet_request_id=r["wallet_request_id"],
            wallet_tx_id=r["wallet_tx_id"],
            payer_address=r["payer_address"],
            confirmations=r["confirmations"],
            required_confirmations=r["required_confirmations"],
            amount_received=int(r["amount_received"]),
            amount_refunded=int(r["amount_refunded"]),
            last_payment_error=error,
            cancellation_reason=r["cancellation_reason"],
            created_at=r["created_at"],
            succeeded_at=r["succeeded_at"],
            canceled_at=r["canceled_at"],
            version=r["version"],
        )
    )


def _to_session(r: Row) -> CheckoutSession:
    return CheckoutSession.restore(
        CheckoutSessionProps(
            id=r["id"],
            merchant_id=r["merchant_id"],
            payment_intent_id=r["payment_intent_id"],
            amount=int(r["amount"]),
            currency=r["currency"],
            description=r["description"],
            success_url=r["success_url"],
            cancel_url=r["cancel_url"],
            status=r["status"],
            payment_status=r["payment_status"],
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            completed_at=r["completed_at"],
        )
    )


def _to_refund(r: Row) -> Refund:
    return Refund.restore(
        RefundProps(
            id=r["id"],
            merchant_id=r["merchant_id"],
            payment_intent_id=r["payment_intent_id"],
            amount=int(r["amount"]),
            currency=r["currency"],
            reason=r["reason"],
            status=r["status"],
            destination_address=r["destination_address"],
            wallet_tx_id=r["wallet_tx_id"],
            failure_reason=r["failure_reason"],
            created_at=r["created_at"],
            completed_at=r["completed_at"],
        )
    )


def _to_entry(r: Row) -> BalanceTransaction:
    return BalanceTransaction(
        id=r["id"],
        merchant_id=r["merchant_id"],
        type=r["type"],
        source_id=r["source_id"],
        amount=int(r["amount"]),
        fee=int(r["fee"]),
        net=int(r["net"]),
        currency=r["currency"],
        description=r["description"],
        created_at=r["created_at"],
    )


def _to_event(r: Row) -> EventRecord:
    return EventRecord(
        id=r["id"],
        merchant_id=r["merchant_id"],
        type=r["type"],
        subject_id=r["subject_id"],
        data=r["data"],
        created_at=r["created_at"],
    )


def _to_endpoint(r: Row) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=r["id"],
        merchant_id=r["merchant_id"],
        url=r["url"],
        secret=r["secret"],
        enabled_events=tuple(r["enabled_events"]),
        description=r["description"],
        status=r["status"],
        created_at=r["created_at"],
    )


def _to_delivery(r: Row) -> WebhookDelivery:
    return WebhookDelivery(
        id=r["id"],
        merchant_id=r["merchant_id"],
        event_id=r["event_id"],
        endpoint_id=r["endpoint_id"],
        event_type=r["event_type"],
        url=r["url"],
        status=r["status"],
        attempts=r["attempts"],
        next_attempt_at=r["next_attempt_at"],
        last_error=r["last_error"],
        last_status_code=r["last_status_code"],
        delivered_at=r["delivered_at"],
        created_at=r["created_at"],
    )


def _to_idempotency(r: Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        merchant_id=r["merchant_id"],
        key=r["key"],
        request_hash=r["request_hash"],
        status=r["status"],
        response_status=r["response_status"],
        response_body=r["response_body"],
        created_at=r["created_at"],
    )


# ───────────────────────────────── репозитории ─────────────────────────────────


class PgMerchantRepository(MerchantRepository):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def insert(self, merchant: Merchant) -> None:
        s = merchant.snapshot()
        try:
            await self._db.execute(
                """INSERT INTO merchants (id, email, business_name, password_hash, api_key_hash, api_key_last4,
                                          api_key_created_at, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    s.id,
                    s.email,
                    s.business_name,
                    s.password_hash,
                    s.api_key_hash,
                    s.api_key_last4,
                    s.api_key_created_at,
                    s.created_at,
                ),
            )
        except psycopg.Error as error:
            if is_unique_violation(error):
                raise ConflictError("Аккаунт с таким e-mail уже есть", code="email_taken", param="email") from error
            raise

    async def update(self, merchant: Merchant) -> None:
        s = merchant.snapshot()
        await self._db.execute(
            """UPDATE merchants SET business_name = %s, api_key_hash = %s, api_key_last4 = %s, api_key_created_at = %s
                WHERE id = %s""",
            (s.business_name, s.api_key_hash, s.api_key_last4, s.api_key_created_at, s.id),
        )

    async def find_by_id(self, id: str) -> Merchant | None:
        row = await self._db.fetch_one("SELECT * FROM merchants WHERE id = %s", (id,))
        return _to_merchant(row) if row else None

    async def find_by_email(self, email: str) -> Merchant | None:
        row = await self._db.fetch_one("SELECT * FROM merchants WHERE email = %s", (email,))
        return _to_merchant(row) if row else None

    async def find_by_api_key_hash(self, key_hash: str) -> Merchant | None:
        row = await self._db.fetch_one("SELECT * FROM merchants WHERE api_key_hash = %s", (key_hash,))
        return _to_merchant(row) if row else None


class PgPaymentIntentRepository(PaymentIntentRepository):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    @staticmethod
    def _params(s: PaymentIntentProps) -> tuple[Any, ...]:
        error = s.last_payment_error
        return (
            s.status,
            s.customer_email,
            s.wallet_request_id,
            s.wallet_tx_id,
            s.payer_address,
            s.confirmations,
            s.amount_received,
            s.amount_refunded,
            error.code if error else None,
            error.message if error else None,
            s.cancellation_reason,
            s.succeeded_at,
            s.canceled_at,
        )

    async def insert(self, intent: PaymentIntent) -> None:
        s = intent.snapshot()
        await self._db.execute(
            """INSERT INTO payment_intents (status, customer_email, wallet_request_id, wallet_tx_id, payer_address,
                                            confirmations, amount_received, amount_refunded, last_error_code,
                                            last_error_message, cancellation_reason, succeeded_at, canceled_at,
                                            id, merchant_id, amount, currency, description, metadata,
                                            checkout_session_id, required_confirmations, created_at, version)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                *self._params(s),
                s.id,
                s.merchant_id,
                s.amount,
                s.currency,
                s.description,
                Jsonb(s.metadata),
                s.checkout_session_id,
                s.required_confirmations,
                s.created_at,
                s.version,
            ),
        )

    async def update(self, intent: PaymentIntent) -> None:
        """Оптимистическая блокировка: обновляем, только если версия в БД та же, что мы прочитали."""
        s = intent.snapshot()
        try:
            changed = await self._db.execute(
                """UPDATE payment_intents
                      SET status = %s, customer_email = %s, wallet_request_id = %s, wallet_tx_id = %s,
                          payer_address = %s, confirmations = %s, amount_received = %s, amount_refunded = %s,
                          last_error_code = %s, last_error_message = %s, cancellation_reason = %s,
                          succeeded_at = %s, canceled_at = %s, version = version + 1
                    WHERE id = %s AND version = %s""",
                (*self._params(s), s.id, s.version),
            )
        except psycopg.Error as error:
            if is_unique_violation(error):
                raise ConflictError(
                    "Эта транзакция уже засчитана другому платежу", code="transaction_reused"
                ) from error
            raise
        if changed != 1:
            raise ConcurrencyError("Платёж", s.id)
        intent.bump_version()

    async def find_by_id(self, id: str) -> PaymentIntent | None:
        row = await self._db.fetch_one("SELECT * FROM payment_intents WHERE id = %s", (id,))
        return _to_intent(row) if row else None

    async def page(
        self, merchant_id: str, query: ListQuery, status: PaymentIntentStatus | None = None
    ) -> Page[PaymentIntent]:
        rows = await self._db.fetch_all(
            """SELECT * FROM payment_intents
                WHERE merchant_id = %s AND (%s::text IS NULL OR status = %s)
                  AND (%s::text IS NULL
                       OR (created_at, id) < (SELECT created_at, id FROM payment_intents WHERE id = %s))
                ORDER BY created_at DESC, id DESC
                LIMIT %s""",
            (merchant_id, status, status, query.starting_after, query.starting_after, query.limit + 1),
        )
        return _page(rows, query, _to_intent)

    async def list_waiting_for_wallet(self, limit: int) -> list[PaymentIntent]:
        rows = await self._db.fetch_all(
            """SELECT * FROM payment_intents WHERE status IN ('requires_action', 'processing')
                ORDER BY created_at, id LIMIT %s""",
            (limit,),
        )
        return [_to_intent(r) for r in rows]

    async def count_by_status(self, merchant_id: str) -> dict[PaymentIntentStatus, int]:
        counts: dict[PaymentIntentStatus, int] = dict.fromkeys(PAYMENT_INTENT_STATUSES, 0)
        for row in await self._db.fetch_all(
            "SELECT status, count(*)::int AS count FROM payment_intents WHERE merchant_id = %s GROUP BY status",
            (merchant_id,),
        ):
            counts[row["status"]] = row["count"]
        return counts


class PgCheckoutSessionRepository(CheckoutSessionRepository):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def insert(self, session: CheckoutSession) -> None:
        s = session.snapshot()
        await self._db.execute(
            """INSERT INTO checkout_sessions (id, merchant_id, payment_intent_id, amount, currency, description,
                                              success_url, cancel_url, status, payment_status, created_at,
                                              expires_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                s.id,
                s.merchant_id,
                s.payment_intent_id,
                s.amount,
                s.currency,
                s.description,
                s.success_url,
                s.cancel_url,
                s.status,
                s.payment_status,
                s.created_at,
                s.expires_at,
                s.completed_at,
            ),
        )

    async def update(self, session: CheckoutSession) -> None:
        s = session.snapshot()
        await self._db.execute(
            "UPDATE checkout_sessions SET status = %s, payment_status = %s, completed_at = %s WHERE id = %s",
            (s.status, s.payment_status, s.completed_at, s.id),
        )

    async def find_by_id(self, id: str) -> CheckoutSession | None:
        row = await self._db.fetch_one("SELECT * FROM checkout_sessions WHERE id = %s", (id,))
        return _to_session(row) if row else None

    async def page(self, merchant_id: str, query: ListQuery) -> Page[CheckoutSession]:
        rows = await self._db.fetch_all(
            """SELECT * FROM checkout_sessions
                WHERE merchant_id = %s
                  AND (%s::text IS NULL
                       OR (created_at, id) < (SELECT created_at, id FROM checkout_sessions WHERE id = %s))
                ORDER BY created_at DESC, id DESC
                LIMIT %s""",
            (merchant_id, query.starting_after, query.starting_after, query.limit + 1),
        )
        return _page(rows, query, _to_session)

    async def find_past_expiry(self, now: datetime, limit: int) -> list[CheckoutSession]:
        rows = await self._db.fetch_all(
            "SELECT * FROM checkout_sessions WHERE status = 'open' AND expires_at <= %s "
            "ORDER BY expires_at, id LIMIT %s",
            (now, limit),
        )
        return [_to_session(r) for r in rows]


class PgRefundRepository(RefundRepository):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def insert(self, refund: Refund) -> None:
        s = refund.snapshot()
        await self._db.execute(
            """INSERT INTO refunds (id, merchant_id, payment_intent_id, amount, currency, reason, status,
                                    destination_address, wallet_tx_id, failure_reason, created_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                s.id,
                s.merchant_id,
                s.payment_intent_id,
                s.amount,
                s.currency,
                s.reason,
                s.status,
                s.destination_address,
                s.wallet_tx_id,
                s.failure_reason,
                s.created_at,
                s.completed_at,
            ),
        )

    async def update(self, refund: Refund) -> None:
        s = refund.snapshot()
        await self._db.execute(
            "UPDATE refunds SET status = %s, wallet_tx_id = %s, failure_reason = %s, completed_at = %s WHERE id = %s",
            (s.status, s.wallet_tx_id, s.failure_reason, s.completed_at, s.id),
        )

    async def find_by_id(self, id: str) -> Refund | None:
        row = await self._db.fetch_one("SELECT * FROM refunds WHERE id = %s", (id,))
        return _to_refund(row) if row else None

    async def page(self, merchant_id: str, query: ListQuery, payment_intent_id: str | None = None) -> Page[Refund]:
        rows = await self._db.fetch_all(
            """SELECT * FROM refunds
                WHERE merchant_id = %s AND (%s::text IS NULL OR payment_intent_id = %s)
                  AND (%s::text IS NULL OR (created_at, id) < (SELECT created_at, id FROM refunds WHERE id = %s))
                ORDER BY created_at DESC, id DESC
                LIMIT %s""",
            (
                merchant_id,
                payment_intent_id,
                payment_intent_id,
                query.starting_after,
                query.starting_after,
                query.limit + 1,
            ),
        )
        return _page(rows, query, _to_refund)

    async def list_pending(self, limit: int) -> list[Refund]:
        rows = await self._db.fetch_all(
            "SELECT * FROM refunds WHERE status = 'pending' ORDER BY created_at, id LIMIT %s", (limit,)
        )
        return [_to_refund(r) for r in rows]


class PgLedgerRepository(LedgerRepository):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def append(self, entry: BalanceTransaction) -> None:
        try:
            await self._db.execute(
                """INSERT INTO balance_transactions (id, merchant_id, type, source_id, amount, fee, net, currency,
                                                     description, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    entry.id,
                    entry.merchant_id,
                    entry.type,
                    entry.source_id,
                    entry.amount,
                    entry.fee,
                    entry.net,
                    entry.currency,
                    entry.description,
                    entry.created_at,
                ),
            )
        except psycopg.Error as error:
            if is_unique_violation(error):
                raise ConflictError("Проводка по этому платежу уже есть", code="duplicate_entry") from error
            raise

    async def page(self, merchant_id: str, query: ListQuery) -> Page[BalanceTransaction]:
        rows = await self._db.fetch_all(
            """SELECT * FROM balance_transactions
                WHERE merchant_id = %s
                  AND (%s::text IS NULL
                       OR (created_at, id) < (SELECT created_at, id FROM balance_transactions WHERE id = %s))
                ORDER BY created_at DESC, id DESC
                LIMIT %s""",
            (merchant_id, query.starting_after, query.starting_after, query.limit + 1),
        )
        return _page(rows, query, _to_entry)

    async def balances(self, merchant_id: str) -> dict[str, int]:
        rows = await self._db.fetch_all(
            "SELECT currency, sum(net)::text AS total FROM balance_transactions "
            "WHERE merchant_id = %s GROUP BY currency",
            (merchant_id,),
        )
        return {r["currency"]: int(r["total"]) for r in rows}

    async def daily_volume(self, merchant_id: str, since: datetime) -> list[DailyVolume]:
        rows = await self._db.fetch_all(
            """SELECT (created_at AT TIME ZONE 'UTC')::date AS day, currency, sum(amount)::text AS total
                 FROM balance_transactions
                WHERE merchant_id = %s AND type = 'payment' AND created_at >= %s
                GROUP BY day, currency
                ORDER BY day, currency""",
            (merchant_id, since),
        )
        return [DailyVolume(day=r["day"], currency=r["currency"], amount=int(r["total"])) for r in rows]


class PgEventRepository(EventRepository):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def append(self, event: EventRecord) -> None:
        await self._db.execute(
            "INSERT INTO events (id, merchant_id, type, subject_id, data, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (event.id, event.merchant_id, event.type, event.subject_id, Jsonb(event.data), event.created_at),
        )

    async def find_by_id(self, id: str) -> EventRecord | None:
        row = await self._db.fetch_one("SELECT * FROM events WHERE id = %s", (id,))
        return _to_event(row) if row else None

    async def page(
        self, merchant_id: str, query: ListQuery, *, type: str | None = None, subject_id: str | None = None
    ) -> Page[EventRecord]:
        rows = await self._db.fetch_all(
            """SELECT * FROM events
                WHERE merchant_id = %s AND (%s::text IS NULL OR type = %s) AND (%s::text IS NULL OR subject_id = %s)
                  AND (%s::text IS NULL OR (created_at, id) < (SELECT created_at, id FROM events WHERE id = %s))
                ORDER BY created_at DESC, id DESC
                LIMIT %s""",
            (
                merchant_id,
                type,
                type,
                subject_id,
                subject_id,
                query.starting_after,
                query.starting_after,
                query.limit + 1,
            ),
        )
        return _page(rows, query, _to_event)


class PgWebhookEndpointRepository(WebhookEndpointRepository):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def insert(self, endpoint: WebhookEndpoint) -> None:
        await self._db.execute(
            """INSERT INTO webhook_endpoints (id, merchant_id, url, secret, enabled_events, description, status,
                                              created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                endpoint.id,
                endpoint.merchant_id,
                endpoint.url,
                endpoint.secret,
                list(endpoint.enabled_events),
                endpoint.description,
                endpoint.status,
                endpoint.created_at,
            ),
        )

    async def delete(self, id: str) -> None:
        await self._db.execute("DELETE FROM webhook_endpoints WHERE id = %s", (id,))

    async def find_by_id(self, id: str) -> WebhookEndpoint | None:
        row = await self._db.fetch_one("SELECT * FROM webhook_endpoints WHERE id = %s", (id,))
        return _to_endpoint(row) if row else None

    async def list_by_merchant(self, merchant_id: str) -> list[WebhookEndpoint]:
        rows = await self._db.fetch_all(
            "SELECT * FROM webhook_endpoints WHERE merchant_id = %s ORDER BY created_at, id", (merchant_id,)
        )
        return [_to_endpoint(r) for r in rows]


class PgWebhookOutbox(WebhookOutbox):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def enqueue(self, delivery: WebhookDelivery) -> None:
        d = delivery
        await self._db.execute(
            """INSERT INTO webhook_deliveries (id, merchant_id, event_id, endpoint_id, event_type, url, status,
                                               attempts, next_attempt_at, last_error, last_status_code, delivered_at,
                                               created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                d.id,
                d.merchant_id,
                d.event_id,
                d.endpoint_id,
                d.event_type,
                d.url,
                d.status,
                d.attempts,
                d.next_attempt_at,
                d.last_error,
                d.last_status_code,
                d.delivered_at,
                d.created_at,
            ),
        )

    async def find_due(self, now: datetime, limit: int) -> list[WebhookDelivery]:
        rows = await self._db.fetch_all(
            """SELECT * FROM webhook_deliveries WHERE status = 'pending' AND next_attempt_at <= %s
                ORDER BY next_attempt_at, id LIMIT %s""",
            (now, limit),
        )
        return [_to_delivery(r) for r in rows]

    async def save(self, delivery: WebhookDelivery) -> None:
        d = delivery
        await self._db.execute(
            """UPDATE webhook_deliveries
                  SET status = %s, attempts = %s, next_attempt_at = %s, last_error = %s, last_status_code = %s,
                      delivered_at = %s
                WHERE id = %s""",
            (d.status, d.attempts, d.next_attempt_at, d.last_error, d.last_status_code, d.delivered_at, d.id),
        )

    async def list_by_event(self, event_id: str) -> list[WebhookDelivery]:
        rows = await self._db.fetch_all(
            "SELECT * FROM webhook_deliveries WHERE event_id = %s ORDER BY created_at, id", (event_id,)
        )
        return [_to_delivery(r) for r in rows]


class PgIdempotencyStore(IdempotencyStore):
    def __init__(self, db: Queryable) -> None:
        self._db = db

    async def try_begin(self, record: IdempotencyRecord) -> IdempotencyRecord | None:
        # INSERT … ON CONFLICT DO NOTHING атомарен: из двух одновременных запросов ключ займёт ровно один.
        inserted = await self._db.execute(
            """INSERT INTO idempotency_keys (merchant_id, key, request_hash, status, created_at)
               VALUES (%s, %s, %s, 'processing', %s)
               ON CONFLICT (merchant_id, key) DO NOTHING""",
            (record.merchant_id, record.key, record.request_hash, record.created_at),
        )
        if inserted == 1:
            return None
        row = await self._db.fetch_one(
            "SELECT * FROM idempotency_keys WHERE merchant_id = %s AND key = %s", (record.merchant_id, record.key)
        )
        return _to_idempotency(row) if row else None

    async def complete(self, merchant_id: str, key: str, status: int, body: str) -> None:
        await self._db.execute(
            """UPDATE idempotency_keys SET status = 'completed', response_status = %s, response_body = %s
                WHERE merchant_id = %s AND key = %s""",
            (status, body, merchant_id, key),
        )

    async def release(self, merchant_id: str, key: str) -> None:
        await self._db.execute("DELETE FROM idempotency_keys WHERE merchant_id = %s AND key = %s", (merchant_id, key))

    async def delete_older_than(self, cutoff: datetime) -> int:
        return await self._db.execute("DELETE FROM idempotency_keys WHERE created_at < %s", (cutoff,))


def create_postgres_repositories(db: Queryable) -> Repositories:
    return Repositories(
        merchants=PgMerchantRepository(db),
        payment_intents=PgPaymentIntentRepository(db),
        checkout_sessions=PgCheckoutSessionRepository(db),
        refunds=PgRefundRepository(db),
        ledger=PgLedgerRepository(db),
        events=PgEventRepository(db),
        webhook_endpoints=PgWebhookEndpointRepository(db),
        outbox=PgWebhookOutbox(db),
        idempotency=PgIdempotencyStore(db),
    )


class PostgresUnitOfWork(UnitOfWork):
    def __init__(self, pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]]) -> None:
        self._pool = pool
        self._repos = create_postgres_repositories(PoolQueryable(pool))

    @property
    def repos(self) -> Repositories:
        return self._repos

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Repositories]:
        """Одно соединение на всю транзакцию; исключение внутри блока — ROLLBACK."""
        async with self._pool.connection() as conn, conn.transaction():
            yield create_postgres_repositories(ConnectionQueryable(conn))
