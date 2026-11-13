"""Доменный слой: деньги, конечные автоматы, бизнес-правила. Без сети, базы и фреймворков."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cryptopay.application.currencies import DEFAULT_CURRENCIES
from cryptopay.domain.checkout_session import CheckoutSession, validate_redirect_url
from cryptopay.domain.errors import AppError, BusinessRuleError, InvalidStateError, ValidationError
from cryptopay.domain.ledger import BalanceTransaction, calculate_fee
from cryptopay.domain.merchant import Merchant, normalize_business_name, normalize_email
from cryptopay.domain.money import format_units, parse_amount, parse_units
from cryptopay.domain.payment_intent import PaymentIntent, PaymentIntentStatus, validate_metadata
from cryptopay.domain.refund import Refund
from cryptopay.domain.webhook_endpoint import WebhookEndpoint, normalize_enabled_events
from tests.support.fakes import T0

USDT = DEFAULT_CURRENCIES[0]


def make_intent(amount: int = 25_000_000, confirmations: int = 3) -> PaymentIntent:
    return PaymentIntent.create(
        id="pi_1",
        merchant_id="acct_1",
        amount=amount,
        currency=USDT,
        description="  Худи  ",
        metadata={"order_id": "1042"},
        checkout_session_id="cs_1",
        required_confirmations=confirmations,
        created_at=T0,
    )


def to_processing(intent: PaymentIntent) -> PaymentIntent:
    intent.start_wallet_payment(wallet_request_id="wpr_1", customer_email=None, now=T0)
    intent.mark_processing(tx_id="0xabc", payer_address="0xpayer", now=T0)
    return intent


def to_succeeded(intent: PaymentIntent) -> PaymentIntent:
    to_processing(intent)
    intent.update_confirmations(intent.required_confirmations)
    intent.mark_succeeded(T0)
    return intent


# ───────────────────────────── деньги ─────────────────────────────


class TestMoney:
    @pytest.mark.parametrize(("raw", "expected"), [(25000000, 25_000_000), ("25000000", 25_000_000), (" 7 ", 7)])
    def test_parse_amount_accepts_integers_and_digit_strings(self, raw: int | str, expected: int) -> None:
        assert parse_amount(raw) == expected

    def test_amount_beyond_float_precision_is_exact(self) -> None:
        huge = "123456789012345678901234567"  # больше 2^53 и точности float
        assert parse_amount(huge) == int(huge)
        assert str(parse_amount(huge)) == huge

    @pytest.mark.parametrize("raw", ["", "0", "-5", "12.5", "1e6", "abc", "١٢", "9" * 31])
    def test_parse_amount_rejects_bad_values(self, raw: str) -> None:
        with pytest.raises(ValidationError) as caught:
            parse_amount(raw)
        assert caught.value.param == "amount"

    def test_parse_amount_rejects_bool(self) -> None:
        # bool в Python — подкласс int: True не должен превратиться в сумму 1
        with pytest.raises(ValidationError):
            parse_amount(True)

    def test_parse_units_and_format_units_round_trip(self) -> None:
        assert parse_units("12.5", 6) == 12_500_000
        assert parse_units("0,0001", 18) == 10**14
        assert format_units(12_500_000, 6) == "12.5"
        assert format_units(10**18, 18) == "1"
        assert format_units(-1500, 3) == "-1.5"

    def test_parse_units_rejects_too_many_decimals(self) -> None:
        with pytest.raises(ValidationError):
            parse_units("1.1234567", 6)


# ───────────────────────────── PaymentIntent ─────────────────────────────


class TestPaymentIntent:
    def test_create_records_event_and_normalizes(self) -> None:
        intent = make_intent()
        assert intent.status == "requires_payment_method"
        assert intent.description == "Худи"
        events = intent.pull_events()
        assert [e.type for e in events] == ["payment_intent.created"]
        assert intent.pull_events() == []

    def test_amount_below_minimum_is_rejected(self) -> None:
        with pytest.raises(BusinessRuleError) as caught:
            make_intent(amount=USDT.min_amount - 1)
        assert caught.value.code == "amount_too_small"

    def test_happy_path_through_all_statuses(self) -> None:
        intent = make_intent()
        intent.start_wallet_payment(wallet_request_id="wpr_1", customer_email="a@b.c", now=T0)
        assert intent.status == "requires_action"
        intent.mark_processing(tx_id="0xabc", payer_address="0xpayer", now=T0)
        assert intent.status == "processing"
        assert intent.update_confirmations(2) is True
        assert intent.update_confirmations(2) is False
        with pytest.raises(BusinessRuleError):
            intent.mark_succeeded(T0)  # всего 2 подтверждения из 3
        intent.update_confirmations(3)
        intent.mark_succeeded(T0)
        assert intent.status == "succeeded"
        assert intent.amount_received == intent.amount
        assert [e.type for e in intent.pull_events()] == [
            "payment_intent.created",
            "payment_intent.requires_action",
            "payment_intent.processing",
            "payment_intent.succeeded",
        ]

    def test_failed_attempt_returns_to_requires_payment_method(self) -> None:
        intent = to_processing(make_intent())
        intent.fail_payment(code="transaction_failed", message="Транзакция не прошла", now=T0)
        assert intent.status == "requires_payment_method"
        assert intent.last_payment_error is not None and intent.last_payment_error.code == "transaction_failed"
        assert intent.wallet_tx_id is None and intent.payer_address is None and intent.confirmations == 0
        # новая попытка стирает старую ошибку
        intent.start_wallet_payment(wallet_request_id="wpr_2", customer_email=None, now=T0)
        assert intent.last_payment_error is None

    @pytest.mark.parametrize(
        ("prepare", "action"),
        [
            ("requires_payment_method", "processing"),
            ("requires_payment_method", "succeeded"),
            ("processing", "canceled"),  # деньги уже в пути — отменить нельзя
            ("succeeded", "canceled"),
            ("succeeded", "requires_payment_method"),
            ("canceled", "requires_action"),
        ],
    )
    def test_forbidden_transitions(self, prepare: PaymentIntentStatus, action: str) -> None:
        intent = make_intent()
        if prepare == "processing":
            to_processing(intent)
        elif prepare == "succeeded":
            to_succeeded(intent)
        elif prepare == "canceled":
            intent.cancel("abandoned", T0)
        with pytest.raises(InvalidStateError) as caught:
            if action == "processing":
                intent.mark_processing(tx_id="0x1", payer_address="0x2", now=T0)
            elif action == "succeeded":
                intent.mark_succeeded(T0)
            elif action == "canceled":
                intent.cancel(None, T0)
            elif action == "requires_payment_method":
                intent.fail_payment(code="x", message="y", now=T0)
            else:
                intent.start_wallet_payment(wallet_request_id="w", customer_email=None, now=T0)
        assert caught.value.code == "payment_intent_unexpected_state"

    def test_cancel_from_requires_action(self) -> None:
        intent = make_intent()
        intent.start_wallet_payment(wallet_request_id="wpr_1", customer_email=None, now=T0)
        intent.cancel("requested_by_customer", T0 + timedelta(minutes=1))
        assert intent.status == "canceled"
        assert intent.snapshot().cancellation_reason == "requested_by_customer"

    def test_refund_limits(self) -> None:
        intent = to_succeeded(make_intent())
        intent.reserve_refund(10_000_000)
        assert intent.refundable_amount == 15_000_000
        with pytest.raises(BusinessRuleError) as caught:
            intent.reserve_refund(15_000_001)
        assert caught.value.code == "amount_too_large"
        intent.release_refund(10_000_000)
        assert intent.refundable_amount == 25_000_000

    def test_only_succeeded_payment_can_be_refunded(self) -> None:
        with pytest.raises(BusinessRuleError) as caught:
            make_intent().reserve_refund(1)
        assert caught.value.code == "charge_not_refundable"

    def test_snapshot_is_a_copy(self) -> None:
        intent = make_intent()
        snapshot = intent.snapshot()
        snapshot.metadata["order_id"] = "hacked"
        assert intent.snapshot().metadata["order_id"] == "1042"

    def test_event_carries_state_at_the_moment_of_event(self) -> None:
        intent = make_intent()
        intent.start_wallet_payment(wallet_request_id="wpr_1", customer_email=None, now=T0)
        intent.mark_processing(tx_id="0xabc", payer_address="0xpayer", now=T0)
        events = intent.pull_events()
        statuses = [getattr(e.subject, "status", None) for e in events]
        assert statuses == ["requires_payment_method", "requires_action", "processing"]

    @pytest.mark.parametrize(
        "metadata",
        [{f"k{i}": "v" for i in range(21)}, {"": "v"}, {"k" * 41: "v"}, {"k": "v" * 501}, {"k": 1}],
    )
    def test_metadata_limits(self, metadata: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            validate_metadata(metadata)  # type: ignore[arg-type]


# ───────────────────────────── CheckoutSession ─────────────────────────────


def make_session(
    ttl: int = 60, success_url: str | None = "https://shop.example/ok?s={CHECKOUT_SESSION_ID}"
) -> CheckoutSession:
    return CheckoutSession.create(
        id="cs_1",
        merchant_id="acct_1",
        payment_intent_id="pi_1",
        amount=25_000_000,
        currency="usdt",
        description=None,
        success_url=success_url,
        cancel_url=None,
        created_at=T0,
        ttl_minutes=ttl,
    )


class TestCheckoutSession:
    def test_success_url_placeholder_is_replaced(self) -> None:
        assert make_session().success_redirect_url() == "https://shop.example/ok?s=cs_1"

    @pytest.mark.parametrize("ttl", [4, 1441, True])
    def test_ttl_range(self, ttl: int) -> None:
        with pytest.raises(ValidationError):
            make_session(ttl=ttl)

    @pytest.mark.parametrize("url", ["shop.example/ok", "javascript:alert(1)", "ftp://x.y", "https:// x", "https://"])
    def test_redirect_url_must_be_http(self, url: str) -> None:
        with pytest.raises(ValidationError):
            validate_redirect_url(url, "success_url")

    def test_expiry_and_transitions(self) -> None:
        session = make_session()
        assert session.is_open(T0)
        assert session.is_past_expiry(T0 + timedelta(minutes=60))
        session.complete(T0)
        assert session.status == "complete" and session.snapshot().payment_status == "paid"
        with pytest.raises(InvalidStateError):
            session.expire(T0)
        assert [e.type for e in session.pull_events()] == ["checkout.session.completed"]


# ───────────────────────────── Refund, проводки, вебхуки, продавец ─────────────────────────────


class TestRefund:
    def test_lifecycle(self) -> None:
        refund = Refund.create(
            id="re_1",
            merchant_id="acct_1",
            payment_intent_id="pi_1",
            amount=5,
            currency="usdt",
            reason=None,
            destination_address="0xpayer",
            created_at=T0,
        )
        refund.attach_transaction("0xtx")
        refund.attach_transaction("0xtx")  # повтор с той же транзакцией — не ошибка
        with pytest.raises(BusinessRuleError):
            refund.attach_transaction("0xother")
        refund.mark_succeeded(T0)
        with pytest.raises(InvalidStateError):
            refund.mark_failed("x", T0)
        assert [e.type for e in refund.pull_events()] == ["refund.created", "refund.succeeded"]


class TestLedger:
    @pytest.mark.parametrize(
        ("amount", "bps", "fee"), [(25_000_000, 100, 250_000), (1, 100, 1), (199, 100, 2), (5, 0, 0)]
    )
    def test_fee_rounds_up(self, amount: int, bps: int, fee: int) -> None:
        assert calculate_fee(amount, bps) == fee

    def test_fee_bps_range(self) -> None:
        with pytest.raises(ValidationError):
            calculate_fee(1, 10_001)

    def test_entries_keep_net_equal_amount_minus_fee(self) -> None:
        payment = BalanceTransaction.for_payment(
            id="t1", merchant_id="a", payment_intent_id="pi", amount=1000, currency="usdt", fee_bps=150, now=T0
        )
        refund = BalanceTransaction.for_refund(
            id="t2", merchant_id="a", refund_id="re", amount=400, currency="usdt", now=T0
        )
        assert (payment.amount, payment.fee, payment.net) == (1000, 15, 985)
        assert (refund.amount, refund.fee, refund.net) == (-400, 0, -400)


class TestWebhookEndpoint:
    def test_star_means_all_events(self) -> None:
        assert normalize_enabled_events(["refund.failed", "*"]) == ("*",)

    def test_unknown_event_rejected(self) -> None:
        with pytest.raises(ValidationError):
            normalize_enabled_events(["payment_intent.teleported"])

    def test_accepts_only_subscribed_events(self) -> None:
        endpoint = WebhookEndpoint(
            id="we_1",
            merchant_id="a",
            url="https://x.y",
            secret="whsec",
            enabled_events=("payment_intent.succeeded",),
            description=None,
            status="enabled",
            created_at=T0,
        )
        assert endpoint.accepts("payment_intent.succeeded")
        assert not endpoint.accepts("refund.created")


class TestMerchant:
    def test_email_and_name_normalized(self) -> None:
        merchant = Merchant.register(
            id="acct_1",
            email="  Shop@Example.COM ",
            business_name="  Магазин   «Пиксель» ",
            password_hash="h",
            created_at=T0,
        )
        assert merchant.email == "shop@example.com"
        assert merchant.business_name == "Магазин «Пиксель»"

    @pytest.mark.parametrize("email", ["no-at-sign", "a@b", "a b@c.d", "@x.y"])
    def test_bad_email(self, email: str) -> None:
        with pytest.raises(ValidationError):
            normalize_email(email)

    def test_bad_name(self) -> None:
        with pytest.raises(AppError):
            normalize_business_name("x")
