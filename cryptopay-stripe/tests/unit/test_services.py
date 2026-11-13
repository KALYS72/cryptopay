"""Прикладной слой: сверка с кошельком, возвраты, истечение страниц, идемпотентность, вебхуки.

Сервисы вызываются напрямую (без HTTP). Кошелёк — настоящий MockWallet, его «сеть»
двигается подменными часами; для редких аварий — заглушка кошелька StubWallet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace

import pytest

from cryptopay.application.jobs.webhook_delivery import WEBHOOK_MAX_ATTEMPTS
from cryptopay.application.ports.repositories import ListQuery
from cryptopay.application.ports.wallet import (
    CryptoWallet,
    NewPaymentRequest,
    WalletOperationError,
    WalletPaymentRequest,
    WalletTransaction,
)
from cryptopay.application.services.checkout_service import CreateCheckoutSession
from cryptopay.container import ContainerOverrides, create_container
from cryptopay.domain.errors import (
    AuthenticationError,
    BusinessRuleError,
    ConflictError,
    IdempotencyError,
    InvalidStateError,
    NotFoundError,
)
from cryptopay.infrastructure.memory.repositories import InMemoryUnitOfWork
from tests.support.fakes import FakeClock, PlainPasswordHasher, SequentialIds
from tests.support.harness import Harness, test_config

AMOUNT = 25_000_000


async def merchant(h: Harness, email: str = "shop@example.com") -> str:
    result = await h.container.services.accounts.register(
        email=email, password="correct-horse", business_name="Магазин"
    )
    return result.merchant.id


async def session_for(h: Harness, merchant_id: str, **overrides: object) -> tuple[str, str]:
    data = CreateCheckoutSession(amount=AMOUNT, currency="usdt", description="Худи")
    data = replace(data, **overrides)  # type: ignore[arg-type]
    session = await h.container.services.checkout.create(merchant_id, data)
    return session.id, session.payment_intent_id


async def pay(h: Harness, session_id: str, account: str = "alice") -> str:
    """Покупатель нажал «Оплатить» и подтвердил платёж в кошельке выбранным тестовым кошельком."""
    url = await h.container.services.checkout.start_payment(session_id, "buyer@example.com")
    request_id = url.rsplit("/", 1)[-1]
    h.wallet.approve(request_id, account)
    return request_id


async def status(h: Harness, intent_id: str) -> str:
    intent = await h.container.uow.repos.payment_intents.find_by_id(intent_id)
    assert intent is not None
    return intent.status


# ───────────────────────────── сверка платежа с кошельком ─────────────────────────────


async def test_payment_succeeds_after_required_confirmations(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id)
    await h.run_jobs()
    assert await status(h, intent_id) == "processing"
    await h.mine(2)
    assert await status(h, intent_id) == "processing"  # 2 подтверждения из 3
    await h.mine(1)
    assert await status(h, intent_id) == "succeeded"

    session = await h.container.services.checkout.retrieve(m, session_id)
    assert session.status == "complete"
    balances = await h.container.services.balance.balances(m)
    assert balances == {"usdt": AMOUNT - AMOUNT // 100}


@pytest.mark.parametrize(
    ("account", "code"),
    [("bob", "insufficient_funds"), ("carol", "wallet_declined")],
)
async def test_wallet_refusals_become_payment_errors(h: Harness, account: str, code: str) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id, account)
    await h.run_jobs()
    intent = await h.container.services.payments.retrieve(m, intent_id)
    assert intent.status == "requires_payment_method"
    assert intent.last_payment_error is not None and intent.last_payment_error.code == code


async def test_rejected_in_wallet_then_paid_with_another_wallet(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    url = await h.container.services.checkout.start_payment(session_id, None)
    h.wallet.reject(url.rsplit("/", 1)[-1])
    view = await h.container.services.checkout.public_view(session_id)
    assert view.intent.last_payment_error is not None
    assert view.intent.last_payment_error.code == "payment_rejected"
    # вторая попытка тем же PaymentIntent
    await pay(h, session_id, "alice")
    await h.mine(3)
    assert await status(h, intent_id) == "succeeded"


async def test_transaction_failing_onchain(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id, "eve")
    await h.run_jobs()
    assert await status(h, intent_id) == "processing"
    await h.mine(1)
    intent = await h.container.services.payments.retrieve(m, intent_id)
    assert intent.status == "requires_payment_method"
    assert intent.last_payment_error is not None and intent.last_payment_error.code == "transaction_failed"
    assert await h.container.services.balance.balances(m) == {}


async def test_slow_network_still_succeeds(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id, "dave")
    await h.mine(4)
    assert await status(h, intent_id) == "processing"
    await h.mine(4)
    assert await status(h, intent_id) == "succeeded"


async def test_repeated_sync_does_not_double_count(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id)
    await h.mine(3)
    for _ in range(3):
        await h.container.services.payments.sync(intent_id)
        await h.run_jobs()
    page = await h.container.services.balance.transactions(m, ListQuery())
    assert [e.type for e in page.items] == ["payment"]


async def test_events_have_object_snapshot_and_order(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id)
    await h.mine(3)
    page = await h.container.services.webhooks.list_events(m, ListQuery(limit=20), subject_id=intent_id)
    oldest_first = list(reversed(page.items))
    assert [e.type for e in oldest_first] == [
        "payment_intent.created",
        "payment_intent.requires_action",
        "payment_intent.processing",
        "payment_intent.succeeded",
    ]
    assert [e.data["status"] for e in oldest_first] == [
        "requires_payment_method",
        "requires_action",
        "processing",
        "succeeded",
    ]


async def test_cannot_start_payment_twice(h: Harness) -> None:
    m = await merchant(h)
    session_id, _ = await session_for(h, m)
    first = await h.container.services.checkout.start_payment(session_id, None)
    second = await h.container.services.checkout.start_payment(session_id, None)
    assert first == second  # ведём на тот же запрос в кошельке
    h.wallet.approve(first.rsplit("/", 1)[-1], "alice")
    with pytest.raises(BusinessRuleError) as caught:
        await h.container.services.checkout.start_payment(session_id, None)
    assert caught.value.code == "payment_in_progress"


async def test_foreign_merchant_sees_not_found(h: Harness) -> None:
    owner = await merchant(h)
    stranger = await merchant(h, "other@example.com")
    _, intent_id = await session_for(h, owner)
    with pytest.raises(NotFoundError):
        await h.container.services.payments.retrieve(stranger, intent_id)


# ───────────────────────────── отмена и истечение ─────────────────────────────


async def test_session_expiry_cancels_intent_and_wallet_request(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m, expires_in_minutes=5)
    url = await h.container.services.checkout.start_payment(session_id, None)
    h.clock.advance_minutes(6)
    await h.run_jobs()
    session = await h.container.services.checkout.retrieve(m, session_id)
    intent = await h.container.services.payments.retrieve(m, intent_id)
    assert session.status == "expired"
    assert intent.status == "canceled" and intent.snapshot().cancellation_reason == "abandoned"
    request = h.wallet.get_payment_request(url.rsplit("/", 1)[-1])
    assert request is not None and request.status == "canceled"  # оплатить закрытую страницу уже нельзя


async def test_session_is_not_expired_while_money_is_in_flight(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m, expires_in_minutes=5)
    await pay(h, session_id, "dave")
    await h.run_jobs()
    h.clock.advance_minutes(6)
    await h.container.jobs[2].run()  # только задача истечения страниц
    assert (await h.container.services.checkout.retrieve(m, session_id)).status == "open"
    await h.run_jobs()
    assert await status(h, intent_id) == "succeeded"
    assert (await h.container.services.checkout.retrieve(m, session_id)).status == "complete"


async def test_cancel_payment_closes_wallet_request(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    url = await h.container.services.checkout.start_payment(session_id, None)
    intent = await h.container.services.payments.cancel(m, intent_id, "requested_by_customer")
    assert intent.status == "canceled"
    assert (await h.container.services.checkout.retrieve(m, session_id)).status == "expired"
    request = h.wallet.get_payment_request(url.rsplit("/", 1)[-1])
    assert request is not None and request.status == "canceled"


async def test_cannot_cancel_when_customer_already_approved(h: Harness) -> None:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id)  # подтвердил, но сервер ещё не сверялся
    with pytest.raises(InvalidStateError):
        await h.container.services.payments.cancel(m, intent_id, None)
    assert await status(h, intent_id) == "processing"


# ───────────────────────────── возвраты ─────────────────────────────


async def paid_intent(h: Harness) -> tuple[str, str]:
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await pay(h, session_id)
    await h.mine(3)
    assert await status(h, intent_id) == "succeeded"
    return m, intent_id


async def test_partial_and_full_refund(h: Harness) -> None:
    m, intent_id = await paid_intent(h)
    alice_before = next(a for a in h.wallet.accounts() if a.id == "alice").balances["usdt"]
    refund = await h.container.services.refunds.create(m, intent_id, 10_000_000, "requested_by_customer")
    assert refund.status == "pending" and refund.wallet_tx_id is not None
    await h.mine(3)
    refund = await h.container.services.refunds.retrieve(m, refund.id)
    assert refund.status == "succeeded"
    alice_after = next(a for a in h.wallet.accounts() if a.id == "alice").balances["usdt"]
    assert alice_after - alice_before == 10_000_000

    # остаток: 25 − 10 = 15 USDT, а на балансе 24.75 − 10 = 14.75 — не хватает, комиссия не возвращается
    with pytest.raises(BusinessRuleError) as caught:
        await h.container.services.refunds.create(m, intent_id, None, None)
    assert caught.value.code == "balance_insufficient"
    with pytest.raises(BusinessRuleError) as caught:
        await h.container.services.refunds.create(m, intent_id, 16_000_000, None)
    assert caught.value.code == "amount_too_large"


async def test_refund_failure_restores_balance(h: Harness) -> None:
    m, intent_id = await paid_intent(h)
    h.wallet.treasury.balances["usdt"] = 0  # на кошельке платформы пусто
    refund = await h.container.services.refunds.create(m, intent_id, 5_000_000, None)
    assert refund.status == "failed"
    intent = await h.container.services.payments.retrieve(m, intent_id)
    assert intent.amount_refunded == 0
    assert await h.container.services.balance.balances(m) == {"usdt": AMOUNT - AMOUNT // 100}
    types = [e.type for e in (await h.container.services.balance.transactions(m, ListQuery())).items]
    assert types == ["refund_failure", "refund", "payment"]


async def test_refund_requires_succeeded_payment(h: Harness) -> None:
    m = await merchant(h)
    _, intent_id = await session_for(h, m)
    with pytest.raises(BusinessRuleError) as caught:
        await h.container.services.refunds.create(m, intent_id, None, None)
    assert caught.value.code == "charge_not_refundable"


# ───────────────────────────── аккаунт ─────────────────────────────


async def test_register_login_and_api_key(h: Harness) -> None:
    accounts = h.container.services.accounts
    await accounts.register(email="Shop@Example.com", password="correct-horse", business_name="Магазин")
    with pytest.raises(ConflictError):
        await accounts.register(email="shop@example.com", password="correct-horse", business_name="Другой")
    with pytest.raises(AuthenticationError):
        await accounts.sign_in(email="shop@example.com", password="wrong-password")
    with pytest.raises(AuthenticationError):
        await accounts.sign_in(email="nobody@example.com", password="correct-horse")
    signed = await accounts.sign_in(email="SHOP@example.com", password="correct-horse")
    assert accounts.authenticate(signed.token) == signed.merchant.id

    first = await accounts.roll_api_key(signed.merchant.id)
    assert first.secret.startswith("sk_test_")
    assert await accounts.authenticate_api_key(first.secret) == signed.merchant.id
    second = await accounts.roll_api_key(signed.merchant.id)
    assert await accounts.authenticate_api_key(first.secret) is None  # старый ключ отозван
    assert await accounts.authenticate_api_key(second.secret) == signed.merchant.id
    assert await accounts.authenticate_api_key("pk_test_whatever") is None


async def test_demo_account_is_created_once() -> None:
    config = test_config({"DEMO_ACCOUNT": "true"})
    uow = InMemoryUnitOfWork()
    overrides = ContainerOverrides(clock=FakeClock(), ids=SequentialIds(), uow=uow, passwords=PlainPasswordHasher())
    first = await create_container(config, overrides)
    second = await create_container(config, overrides)
    assert first.demo_merchant_id is not None and first.demo_merchant_id == second.demo_merchant_id


# ───────────────────────────── идемпотентность ─────────────────────────────


async def test_idempotency_replays_and_detects_reuse(h: Harness) -> None:
    service = h.container.services.idempotency
    calls = 0

    async def operation() -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return 200, json.dumps({"n": calls})

    first = await service.execute(merchant_id="acct_1", key="k1", request_hash="A", operation=operation)
    again = await service.execute(merchant_id="acct_1", key="k1", request_hash="A", operation=operation)
    assert (first.body, first.replayed) == ('{"n": 1}', False)
    assert (again.body, again.replayed) == ('{"n": 1}', True)
    assert calls == 1
    with pytest.raises(IdempotencyError) as caught:
        await service.execute(merchant_id="acct_1", key="k1", request_hash="B", operation=operation)
    assert caught.value.code == "idempotency_key_reused"
    # ключи у разных продавцов независимы
    other = await service.execute(merchant_id="acct_2", key="k1", request_hash="B", operation=operation)
    assert other.replayed is False


async def test_idempotency_key_released_after_error(h: Harness) -> None:
    service = h.container.services.idempotency

    async def failing() -> tuple[int, str]:
        raise BusinessRuleError("x", "ошибка")

    async def ok() -> tuple[int, str]:
        return 200, "{}"

    with pytest.raises(BusinessRuleError):
        await service.execute(merchant_id="a", key="k", request_hash="A", operation=failing)
    result = await service.execute(merchant_id="a", key="k", request_hash="A", operation=ok)
    assert result.replayed is False


async def test_idempotency_key_in_use(h: Harness) -> None:
    service = h.container.services.idempotency

    async def nested() -> tuple[int, str]:
        with pytest.raises(IdempotencyError) as caught:
            await service.execute(merchant_id="a", key="k", request_hash="A", operation=nested)
        assert caught.value.code == "idempotency_key_in_use"
        return 200, "{}"

    await service.execute(merchant_id="a", key="k", request_hash="A", operation=nested)


async def test_idempotency_keys_expire_after_a_day(h: Harness) -> None:
    service = h.container.services.idempotency

    async def ok() -> tuple[int, str]:
        return 200, "{}"

    await service.execute(merchant_id="a", key="k", request_hash="A", operation=ok)
    h.clock.advance_minutes(24 * 60 + 1)
    assert await service.cleanup() == 1


# ───────────────────────────── вебхуки ─────────────────────────────


async def test_webhooks_respect_subscriptions_and_are_signed(h: Harness) -> None:
    m = await merchant(h)
    endpoint = await h.container.services.webhooks.create_endpoint(
        m, url="https://shop.example/hooks", enabled_events=["payment_intent.succeeded"], description=None
    )
    session_id, _ = await session_for(h, m)
    await pay(h, session_id)
    await h.mine(3)
    await h.run_jobs()
    assert h.webhooks.event_types() == ["payment_intent.succeeded"]

    request = h.webhooks.sent[0]
    timestamp, signature = (part.split("=", 1)[1] for part in request.headers["cryptopay-signature"].split(","))
    expected = hmac.new(endpoint.secret.encode(), f"{timestamp}.{request.body}".encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    event = json.loads(request.body)
    assert event["object"] == "event" and event["data"]["object"]["status"] == "succeeded"


async def test_webhook_retries_with_backoff_then_gives_up(h: Harness) -> None:
    m = await merchant(h)
    await h.container.services.webhooks.create_endpoint(
        m, url="https://shop.example/hooks", enabled_events=["*"], description=None
    )
    await session_for(h, m, expires_in_minutes=1440)
    h.webhooks.fail_with_status = 500
    await h.run_jobs()
    assert len(h.webhooks.sent) == 1
    await h.run_jobs()
    assert len(h.webhooks.sent) == 1  # следующая попытка — только через минуту
    for minutes in (1, 5, 15, 60, 360):
        h.clock.advance_minutes(minutes)
        await h.run_jobs()
    assert len(h.webhooks.sent) == WEBHOOK_MAX_ATTEMPTS
    h.clock.advance_minutes(360)
    await h.run_jobs()
    assert len(h.webhooks.sent) == WEBHOOK_MAX_ATTEMPTS  # больше не пытаемся
    event = (await h.container.services.webhooks.list_events(m, ListQuery())).items[0]
    deliveries = await h.container.services.webhooks.list_deliveries(m, event.id)
    assert deliveries[0].status == "failed" and deliveries[0].attempts == WEBHOOK_MAX_ATTEMPTS


async def test_deleted_endpoint_stops_deliveries(h: Harness) -> None:
    m = await merchant(h)
    endpoint = await h.container.services.webhooks.create_endpoint(
        m, url="https://shop.example/hooks", enabled_events=["*"], description=None
    )
    await session_for(h, m)
    await h.container.services.webhooks.delete_endpoint(m, endpoint.id)
    await h.run_jobs()
    assert h.webhooks.sent == []


async def test_endpoint_limits_and_ssrf_policy(h: Harness) -> None:
    m = await merchant(h)
    production = await create_container(
        test_config({"APP_ENV": "production", "ALLOW_HTTP_WEBHOOKS": "false"}),
        ContainerOverrides(uow=InMemoryUnitOfWork(), passwords=PlainPasswordHasher()),
    )
    for url in (
        "http://shop.example/h",
        "https://169.254.169.254/latest",
        "https://localhost/h",
        "https://[::ffff:127.0.0.1]/",
    ):
        with pytest.raises(Exception, match=r"URL|адрес"):
            await production.services.webhooks.create_endpoint(m, url=url, enabled_events=["*"], description=None)
    for i in range(5):
        await h.container.services.webhooks.create_endpoint(
            m, url=f"https://shop.example/{i}", enabled_events=["*"], description=None
        )
    with pytest.raises(BusinessRuleError):
        await h.container.services.webhooks.create_endpoint(
            m, url="https://x.example", enabled_events=["*"], description=None
        )


async def test_test_event_goes_only_to_chosen_endpoint(h: Harness) -> None:
    m = await merchant(h)
    services = h.container.services.webhooks
    first = await services.create_endpoint(m, url="https://a.example/h", enabled_events=["*"], description=None)
    await services.create_endpoint(m, url="https://b.example/h", enabled_events=["*"], description=None)
    await services.send_test(m, first.id)
    await h.run_jobs()
    assert [r.url for r in h.webhooks.sent] == ["https://a.example/h"]
    assert h.webhooks.event_types() == ["webhook_endpoint.test"]


# ───────────────────────────── аварии кошелька ─────────────────────────────


class StubWallet(CryptoWallet):
    """Кошелёк, который ведёт себя как задано в тесте: подменяет сумму, «теряет» транзакцию и т. п."""

    def __init__(self) -> None:
        self.tx: WalletTransaction | None = None
        self.refund_error: WalletOperationError | None = None

    async def create_payment_request(self, request: NewPaymentRequest) -> WalletPaymentRequest:
        return WalletPaymentRequest(
            id="wpr_stub", status="approved", approve_url="http://w/approve", tx_id="0xstub",
            payer_address="0xpayer", failure_code=None, failure_message=None,
        )  # fmt: skip

    async def get_payment_request(self, request_id: str) -> WalletPaymentRequest | None:
        return await self.create_payment_request(None)  # type: ignore[arg-type]

    async def cancel_payment_request(self, request_id: str) -> WalletPaymentRequest | None:
        return None

    async def get_transaction(self, tx_id: str) -> WalletTransaction | None:
        return self.tx

    async def send_refund(self, *, reference: str, to_address: str, amount: int, currency: str) -> WalletTransaction:
        if self.refund_error:
            raise self.refund_error
        raise AssertionError("not used")


async def stub_harness() -> tuple[Harness, StubWallet]:
    from tests.support.harness import create_harness

    stub = StubWallet()
    h = await create_harness(wallet=stub)
    return h, stub


@pytest.mark.parametrize(
    ("tx", "code"),
    [
        (None, "transaction_not_found"),
        (WalletTransaction("0xstub", "confirmed", 5, 1, "usdt", "0xpayer", "0xt", None), "amount_mismatch"),
        (WalletTransaction("0xstub", "confirmed", 5, AMOUNT, "btc", "0xpayer", "0xt", None), "amount_mismatch"),
    ],
)
async def test_server_does_not_trust_the_wallet(tx: WalletTransaction | None, code: str) -> None:
    h, stub = await stub_harness()
    stub.tx = tx
    m = await merchant(h)
    session_id, intent_id = await session_for(h, m)
    await h.container.services.checkout.start_payment(session_id, None)
    await h.run_jobs()
    intent = await h.container.services.payments.retrieve(m, intent_id)
    assert intent.status == "requires_payment_method"
    assert intent.last_payment_error is not None and intent.last_payment_error.code == code
    await h.client.aclose()
