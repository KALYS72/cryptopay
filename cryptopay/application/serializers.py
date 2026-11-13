"""Объекты API в формате Stripe: ``{"id": "pi_…", "object": "payment_intent", "created": 1762678800, …}``.

Одни и те же функции строят и ответы REST API, и поле ``data.object`` вебхуков —
поэтому магазин получает в вебхуке ровно тот же объект, что вернул бы GET-запрос.

Суммы — строки, а не числа: 1 ETH = 10^18 wei не помещается в число JavaScript
без потери точности (у Stripe суммы целые в центах, и такой проблемы нет).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.checkout_session import CheckoutSession, CheckoutSessionProps
from ..domain.ledger import BalanceTransaction
from ..domain.merchant import Merchant
from ..domain.payment_intent import PaymentIntent, PaymentIntentProps
from ..domain.refund import Refund, RefundProps
from ..domain.webhook_endpoint import WebhookEndpoint
from .ports.repositories import EventRecord, WebhookDelivery

API_VERSION = "2026-11-01"


def unix(moment: datetime | None) -> int | None:
    return None if moment is None else int(moment.timestamp())


class ObjectSerializer:
    def __init__(self, public_url: str) -> None:
        self._public_url = public_url.rstrip("/")

    def checkout_url(self, session_id: str) -> str:
        return f"{self._public_url}/checkout/{session_id}"

    # ── объекты ──
    def payment_intent(self, intent: PaymentIntent | PaymentIntentProps) -> dict[str, Any]:
        p = intent.snapshot() if isinstance(intent, PaymentIntent) else intent
        error = p.last_payment_error
        return {
            "id": p.id,
            "object": "payment_intent",
            "amount": str(p.amount),
            "amount_received": str(p.amount_received),
            "amount_refunded": str(p.amount_refunded),
            "currency": p.currency,
            "status": p.status,
            "description": p.description,
            "metadata": dict(p.metadata),
            "checkout_session": p.checkout_session_id,
            "customer_email": p.customer_email,
            "payment_method": {
                "type": "crypto_wallet",
                "wallet_request": p.wallet_request_id,
                "transaction": p.wallet_tx_id,
                "payer_address": p.payer_address,
                "confirmations": p.confirmations,
                "required_confirmations": p.required_confirmations,
            },
            "last_payment_error": {"code": error.code, "message": error.message} if error else None,
            "cancellation_reason": p.cancellation_reason,
            "created": unix(p.created_at),
            "succeeded_at": unix(p.succeeded_at),
            "canceled_at": unix(p.canceled_at),
            "livemode": False,
        }

    def checkout_session(self, session: CheckoutSession | CheckoutSessionProps) -> dict[str, Any]:
        s = session.snapshot() if isinstance(session, CheckoutSession) else session
        return {
            "id": s.id,
            "object": "checkout.session",
            "url": self.checkout_url(s.id) if s.status == "open" else None,
            "status": s.status,
            "payment_status": s.payment_status,
            "payment_intent": s.payment_intent_id,
            "amount_total": str(s.amount),
            "currency": s.currency,
            "description": s.description,
            "success_url": s.success_url,
            "cancel_url": s.cancel_url,
            "created": unix(s.created_at),
            "expires_at": unix(s.expires_at),
            "completed_at": unix(s.completed_at),
            "livemode": False,
        }

    def refund(self, refund: Refund | RefundProps) -> dict[str, Any]:
        r = refund.snapshot() if isinstance(refund, Refund) else refund
        return {
            "id": r.id,
            "object": "refund",
            "amount": str(r.amount),
            "currency": r.currency,
            "payment_intent": r.payment_intent_id,
            "reason": r.reason,
            "status": r.status,
            "destination_address": r.destination_address,
            "transaction": r.wallet_tx_id,
            "failure_reason": r.failure_reason,
            "created": unix(r.created_at),
            "completed_at": unix(r.completed_at),
        }

    def balance_transaction(self, entry: BalanceTransaction) -> dict[str, Any]:
        return {
            "id": entry.id,
            "object": "balance_transaction",
            "type": entry.type,
            "source": entry.source_id,
            "amount": str(entry.amount),
            "fee": str(entry.fee),
            "net": str(entry.net),
            "currency": entry.currency,
            "description": entry.description,
            "created": unix(entry.created_at),
        }

    def balance(self, balances: dict[str, int], currencies: list[str]) -> dict[str, Any]:
        return {
            "object": "balance",
            "available": [{"amount": str(balances.get(code, 0)), "currency": code} for code in currencies],
            "livemode": False,
        }

    def event(self, record: EventRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "object": "event",
            "type": record.type,
            "api_version": API_VERSION,
            "created": unix(record.created_at),
            "data": {"object": record.data},
            "livemode": False,
        }

    def webhook_endpoint(self, endpoint: WebhookEndpoint, *, include_secret: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": endpoint.id,
            "object": "webhook_endpoint",
            "url": endpoint.url,
            "enabled_events": list(endpoint.enabled_events),
            "description": endpoint.description,
            "status": endpoint.status,
            "created": unix(endpoint.created_at),
        }
        if include_secret:
            result["secret"] = endpoint.secret
        return result

    def webhook_delivery(self, delivery: WebhookDelivery) -> dict[str, Any]:
        return {
            "id": delivery.id,
            "object": "webhook_delivery",
            "event": delivery.event_id,
            "webhook_endpoint": delivery.endpoint_id,
            "url": delivery.url,
            "status": delivery.status,
            "attempts": delivery.attempts,
            "next_attempt_at": unix(delivery.next_attempt_at) if delivery.status == "pending" else None,
            "last_error": delivery.last_error,
            "last_status_code": delivery.last_status_code,
            "delivered_at": unix(delivery.delivered_at),
            "created": unix(delivery.created_at),
        }

    def account(self, merchant: Merchant) -> dict[str, Any]:
        last4 = merchant.api_key_last4
        return {
            "id": merchant.id,
            "object": "account",
            "email": merchant.email,
            "business_name": merchant.business_name,
            "api_key": {"preview": f"sk_test_…{last4}", "created": unix(merchant.api_key_created_at)}
            if last4
            else None,
            "created": unix(merchant.created_at),
        }

    def subject(self, subject: object) -> dict[str, Any]:
        """JSON объекта из снимка, который сущность приложила к доменному событию."""
        if isinstance(subject, PaymentIntentProps):
            return self.payment_intent(subject)
        if isinstance(subject, CheckoutSessionProps):
            return self.checkout_session(subject)
        if isinstance(subject, RefundProps):
            return self.refund(subject)
        raise TypeError(f"Неизвестный тип объекта события: {type(subject).__name__}")
