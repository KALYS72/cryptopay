"""КОНТРАКТНЫЕ ТЕСТЫ репозиториев — SOLID (L), принцип подстановки Лисков в действии.

Один и тот же набор проверок прогоняется для хранилища в памяти и для PostgreSQL
(классы-наследники в test_memory.py и test_postgres.py подставляют свою фикстуру ``uow``).
Если обе реализации его проходят, прикладной слой может работать с любой из них.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from cryptopay.application.currencies import DEFAULT_CURRENCIES
from cryptopay.application.ports.repositories import (
    EventRecord,
    IdempotencyRecord,
    ListQuery,
    UnitOfWork,
    WebhookDelivery,
)
from cryptopay.domain.checkout_session import CheckoutSession
from cryptopay.domain.errors import ConcurrencyError, ConflictError
from cryptopay.domain.ledger import BalanceTransaction
from cryptopay.domain.merchant import Merchant
from cryptopay.domain.payment_intent import PaymentIntent
from cryptopay.domain.refund import Refund
from cryptopay.domain.webhook_endpoint import WebhookEndpoint
from tests.support.fakes import T0, SequentialIds

ids = SequentialIds()
USDT, ETH = DEFAULT_CURRENCIES[0], DEFAULT_CURRENCIES[3]


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


async def new_merchant(uow: UnitOfWork, email: str | None = None) -> Merchant:
    merchant_id = ids.generate("acct")
    merchant = Merchant.register(
        id=merchant_id,
        email=email or f"{merchant_id}@example.com",
        business_name="Магазин",
        password_hash="hash",
        created_at=T0,
    )
    await uow.repos.merchants.insert(merchant)
    return merchant


def build_intent(merchant: Merchant, minute: float = 0, amount: int = 25_000_000) -> PaymentIntent:
    return PaymentIntent.create(
        id=ids.generate("pi"),
        merchant_id=merchant.id,
        amount=amount,
        currency=USDT,
        description="Худи",
        metadata={"order_id": "1042"},
        checkout_session_id=None,
        required_confirmations=3,
        created_at=at(minute),
    )


async def succeeded_intent(uow: UnitOfWork, merchant: Merchant) -> PaymentIntent:
    intent = build_intent(merchant)
    await uow.repos.payment_intents.insert(intent)
    intent.start_wallet_payment(wallet_request_id="wpr", customer_email=None, now=T0)
    intent.mark_processing(tx_id=ids.generate("txn"), payer_address="0xpayer", now=T0)
    intent.update_confirmations(3)
    intent.mark_succeeded(T0)
    await uow.repos.payment_intents.update(intent)
    return intent


def delivery(merchant_id: str, event_id: str, minute: float = 0) -> WebhookDelivery:
    return WebhookDelivery(
        id=ids.generate("whd"),
        merchant_id=merchant_id,
        event_id=event_id,
        endpoint_id="we_x",
        event_type="payment_intent.succeeded",
        url="https://shop.example/hooks",
        status="pending",
        attempts=0,
        next_attempt_at=at(minute),
        last_error=None,
        last_status_code=None,
        delivered_at=None,
        created_at=at(minute),
    )


def event(merchant_id: str, subject_id: str, type: str = "payment_intent.created", minute: float = 0) -> EventRecord:
    return EventRecord(
        id=ids.generate("evt"),
        merchant_id=merchant_id,
        type=type,
        subject_id=subject_id,
        data={"id": subject_id, "amount": "25000000", "nested": {"ok": True}, "name": "Кириллица"},
        created_at=at(minute),
    )


class RepositoryContract:
    """Базовый класс с тестами. Наследник обязан объявить фикстуру ``uow``."""

    # ── продавцы ──
    async def test_merchant_round_trip_and_unique_email(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow, "unique@example.com")
        found = await uow.repos.merchants.find_by_email("unique@example.com")
        assert found is not None and found.id == merchant.id and found.business_name == "Магазин"
        with pytest.raises(ConflictError):
            await new_merchant(uow, "unique@example.com")

    async def test_merchant_api_key_lookup(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        merchant.set_api_key(key_hash="hash-of-key", last4="ab12", now=at(1))
        await uow.repos.merchants.update(merchant)
        found = await uow.repos.merchants.find_by_api_key_hash("hash-of-key")
        assert found is not None and found.id == merchant.id and found.api_key_last4 == "ab12"
        assert await uow.repos.merchants.find_by_api_key_hash("other") is None

    # ── платежи ──
    async def test_intent_round_trip_keeps_big_amounts_and_metadata(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        intent = PaymentIntent.create(
            id=ids.generate("pi"),
            merchant_id=merchant.id,
            amount=2**200,  # больше uint64 и точности float
            currency=ETH,
            description=None,
            metadata={"заказ": "№ 1042"},
            checkout_session_id=None,
            required_confirmations=3,
            created_at=T0,
        )
        await uow.repos.payment_intents.insert(intent)
        found = await uow.repos.payment_intents.find_by_id(intent.id)
        assert found is not None
        assert found.amount == 2**200
        assert found.snapshot().metadata == {"заказ": "№ 1042"}

    async def test_optimistic_locking(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        intent = build_intent(merchant)
        await uow.repos.payment_intents.insert(intent)
        first = await uow.repos.payment_intents.find_by_id(intent.id)
        second = await uow.repos.payment_intents.find_by_id(intent.id)
        assert first is not None and second is not None
        first.cancel(None, T0)
        await uow.repos.payment_intents.update(first)
        assert first.version == 1
        second.start_wallet_payment(wallet_request_id="wpr", customer_email=None, now=T0)
        with pytest.raises(ConcurrencyError):
            await uow.repos.payment_intents.update(second)

    async def test_one_transaction_cannot_pay_two_intents(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        first, second = build_intent(merchant), build_intent(merchant)
        for intent in (first, second):
            await uow.repos.payment_intents.insert(intent)
            intent.start_wallet_payment(wallet_request_id="wpr", customer_email=None, now=T0)
        first.mark_processing(tx_id="0xsame", payer_address="0xp", now=T0)
        await uow.repos.payment_intents.update(first)
        second.mark_processing(tx_id="0xsame", payer_address="0xp", now=T0)
        with pytest.raises(ConflictError):
            async with uow.transaction() as repos:
                await repos.payment_intents.update(second)

    async def test_error_is_persisted(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        intent = build_intent(merchant)
        await uow.repos.payment_intents.insert(intent)
        intent.start_wallet_payment(wallet_request_id="wpr", customer_email="a@b.c", now=T0)
        intent.fail_payment(code="insufficient_funds", message="Недостаточно средств", now=T0)
        await uow.repos.payment_intents.update(intent)
        found = await uow.repos.payment_intents.find_by_id(intent.id)
        assert found is not None and found.last_payment_error is not None
        assert found.last_payment_error.code == "insufficient_funds"
        assert found.snapshot().customer_email == "a@b.c"

    async def test_cursor_pagination_and_status_filter(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        other = await new_merchant(uow)
        intents = [build_intent(merchant, minute=i) for i in range(5)]
        for intent in intents:
            await uow.repos.payment_intents.insert(intent)
        await uow.repos.payment_intents.insert(build_intent(other))
        intents[1].cancel(None, T0)
        await uow.repos.payment_intents.update(intents[1])

        first = await uow.repos.payment_intents.page(merchant.id, ListQuery(limit=2))
        assert [i.id for i in first.items] == [intents[4].id, intents[3].id] and first.has_more
        second = await uow.repos.payment_intents.page(merchant.id, ListQuery(limit=2, starting_after=intents[3].id))
        assert [i.id for i in second.items] == [intents[2].id, intents[1].id] and second.has_more
        last = await uow.repos.payment_intents.page(merchant.id, ListQuery(limit=2, starting_after=intents[1].id))
        assert [i.id for i in last.items] == [intents[0].id] and not last.has_more
        canceled = await uow.repos.payment_intents.page(merchant.id, ListQuery(), "canceled")
        assert [i.id for i in canceled.items] == [intents[1].id]
        unknown = await uow.repos.payment_intents.page(merchant.id, ListQuery(starting_after="pi_missing"))
        assert unknown.items == [] and not unknown.has_more
        counts = await uow.repos.payment_intents.count_by_status(merchant.id)
        assert counts["requires_payment_method"] == 4 and counts["canceled"] == 1 and counts["succeeded"] == 0

    async def test_waiting_for_wallet(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        waiting, idle = build_intent(merchant), build_intent(merchant)
        for intent in (waiting, idle):
            await uow.repos.payment_intents.insert(intent)
        waiting.start_wallet_payment(wallet_request_id="wpr", customer_email=None, now=T0)
        await uow.repos.payment_intents.update(waiting)
        found = await uow.repos.payment_intents.list_waiting_for_wallet(10)
        assert waiting.id in [i.id for i in found] and idle.id not in [i.id for i in found]

    # ── страницы оплаты ──
    async def test_sessions_past_expiry(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        intents = [build_intent(merchant) for _ in range(2)]
        sessions = []
        for intent, ttl in zip(intents, (5, 60), strict=True):
            await uow.repos.payment_intents.insert(intent)
            session = CheckoutSession.create(
                id=ids.generate("cs"),
                merchant_id=merchant.id,
                payment_intent_id=intent.id,
                amount=intent.amount,
                currency="usdt",
                description=None,
                success_url="https://shop.example/ok",
                cancel_url=None,
                created_at=T0,
                ttl_minutes=ttl,
            )
            await uow.repos.checkout_sessions.insert(session)
            sessions.append(session)
        due = await uow.repos.checkout_sessions.find_past_expiry(at(10), 10)
        assert [s.id for s in due] == [sessions[0].id]
        sessions[0].expire(at(10))
        await uow.repos.checkout_sessions.update(sessions[0])
        assert await uow.repos.checkout_sessions.find_past_expiry(at(10), 10) == []
        page = await uow.repos.checkout_sessions.page(merchant.id, ListQuery())
        assert {s.id for s in page.items} == {s.id for s in sessions}
        found = await uow.repos.checkout_sessions.find_by_id(sessions[1].id)
        assert found is not None and found.success_url == "https://shop.example/ok"

    # ── возвраты ──
    async def test_refunds_by_intent_and_pending(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        intent = await succeeded_intent(uow, merchant)
        refund = Refund.create(
            id=ids.generate("re"),
            merchant_id=merchant.id,
            payment_intent_id=intent.id,
            amount=1_000_000,
            currency="usdt",
            reason="duplicate",
            destination_address="0xpayer",
            created_at=T0,
        )
        await uow.repos.refunds.insert(refund)
        assert [r.id for r in await uow.repos.refunds.list_pending(10)] == [refund.id]
        refund.attach_transaction("0xrefund")
        refund.mark_succeeded(at(1))
        await uow.repos.refunds.update(refund)
        assert await uow.repos.refunds.list_pending(10) == []
        page = await uow.repos.refunds.page(merchant.id, ListQuery(), intent.id)
        assert len(page.items) == 1 and page.items[0].wallet_tx_id == "0xrefund" and page.items[0].status == "succeeded"
        assert (await uow.repos.refunds.page(merchant.id, ListQuery(), "pi_other")).items == []

    # ── проводки ──
    async def test_ledger_balances_and_one_payment_entry(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        payment = BalanceTransaction.for_payment(
            id=ids.generate("txn"),
            merchant_id=merchant.id,
            payment_intent_id="pi_1",
            amount=25_000_000,
            currency="usdt",
            fee_bps=100,
            now=at(1),
        )
        refund = BalanceTransaction.for_refund(
            id=ids.generate("txn"),
            merchant_id=merchant.id,
            refund_id="re_1",
            amount=5_000_000,
            currency="usdt",
            now=at(2),
        )
        await uow.repos.ledger.append(payment)
        await uow.repos.ledger.append(refund)
        assert await uow.repos.ledger.balances(merchant.id) == {"usdt": 25_000_000 - 250_000 - 5_000_000}
        with pytest.raises(ConflictError):  # вторая проводка «оплата» по тому же платежу
            async with uow.transaction() as repos:
                await repos.ledger.append(replace(payment, id=ids.generate("txn")))
        page = await uow.repos.ledger.page(merchant.id, ListQuery(limit=1))
        assert [e.type for e in page.items] == ["refund"] and page.has_more
        volume = await uow.repos.ledger.daily_volume(merchant.id, T0)
        assert [(v.day, v.currency, v.amount) for v in volume] == [(T0.date(), "usdt", 25_000_000)]

    # ── события и доставки ──
    async def test_events_filters_and_json(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        first = event(merchant.id, "pi_a", minute=0)
        second = event(merchant.id, "pi_b", type="payment_intent.succeeded", minute=1)
        for record in (first, second):
            await uow.repos.events.append(record)
        found = await uow.repos.events.find_by_id(first.id)
        assert found is not None and found.data == first.data
        assert [e.id for e in (await uow.repos.events.page(merchant.id, ListQuery())).items] == [second.id, first.id]
        by_type = await uow.repos.events.page(merchant.id, ListQuery(), type="payment_intent.succeeded")
        assert [e.id for e in by_type.items] == [second.id]
        by_subject = await uow.repos.events.page(merchant.id, ListQuery(), subject_id="pi_a")
        assert [e.id for e in by_subject.items] == [first.id]

    async def test_outbox_due_and_save(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        record = event(merchant.id, "pi_a")
        await uow.repos.events.append(record)
        now_due, later = delivery(merchant.id, record.id, 0), delivery(merchant.id, record.id, 30)
        await uow.repos.outbox.enqueue(now_due)
        await uow.repos.outbox.enqueue(later)
        assert [d.id for d in await uow.repos.outbox.find_due(at(1), 10)] == [now_due.id]
        await uow.repos.outbox.save(
            replace(now_due, status="delivered", attempts=1, delivered_at=at(1), last_status_code=200)
        )
        assert await uow.repos.outbox.find_due(at(1), 10) == []
        deliveries = await uow.repos.outbox.list_by_event(record.id)
        assert [d.status for d in deliveries] == ["delivered", "pending"]

    async def test_webhook_endpoints(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        endpoint = WebhookEndpoint(
            id=ids.generate("we"),
            merchant_id=merchant.id,
            url="https://shop.example/hooks",
            secret="whsec_x",
            enabled_events=("payment_intent.succeeded", "refund.failed"),
            description="Основной",
            status="enabled",
            created_at=T0,
        )
        await uow.repos.webhook_endpoints.insert(endpoint)
        assert await uow.repos.webhook_endpoints.list_by_merchant(merchant.id) == [endpoint]
        await uow.repos.webhook_endpoints.delete(endpoint.id)
        assert await uow.repos.webhook_endpoints.find_by_id(endpoint.id) is None

    # ── идемпотентность ──
    async def test_idempotency_store(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        record = IdempotencyRecord(
            merchant_id=merchant.id,
            key="order-1042",
            request_hash="h1",
            status="processing",
            response_status=None,
            response_body=None,
            created_at=T0,
        )
        assert await uow.repos.idempotency.try_begin(record) is None
        existing = await uow.repos.idempotency.try_begin(replace(record, request_hash="h2"))
        assert existing is not None and existing.request_hash == "h1" and existing.status == "processing"
        await uow.repos.idempotency.complete(merchant.id, "order-1042", 200, '{"ok":true}')
        completed = await uow.repos.idempotency.try_begin(record)
        assert completed is not None and completed.response_status == 200 and completed.response_body == '{"ok":true}'
        await uow.repos.idempotency.release(merchant.id, "order-1042")
        assert await uow.repos.idempotency.try_begin(record) is None
        assert await uow.repos.idempotency.delete_older_than(at(1)) == 1

    # ── транзакции ──
    async def test_transaction_rolls_back_everything(self, uow: UnitOfWork) -> None:
        merchant = await new_merchant(uow)
        intent = build_intent(merchant)
        with pytest.raises(RuntimeError):
            async with uow.transaction() as repos:
                await repos.payment_intents.insert(intent)
                await repos.events.append(event(merchant.id, intent.id))
                raise RuntimeError("сбой посередине")
        assert await uow.repos.payment_intents.find_by_id(intent.id) is None
        assert (await uow.repos.events.page(merchant.id, ListQuery())).items == []
