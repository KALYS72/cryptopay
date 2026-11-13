"""Схемы запросов (Pydantic) и описания ответов для документации OpenAPI.

Pydantic проверяет форму входящих данных на границе системы; бизнес-правила
(минимальная сумма, допустимые переходы статусов) проверяют сущности домена.
Модели ответов здесь только для Swagger UI: сами ответы строит ObjectSerializer.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

from ...domain.payment_intent import CancellationReason
from ...domain.refund import RefundReason

AmountInput = int | str


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─────────────────────────────── запросы ───────────────────────────────


class RegisterRequest(Strict):
    email: str = Field(min_length=1, max_length=254, examples=["shop@example.com"])
    password: str = Field(min_length=1, max_length=128)
    business_name: str = Field(min_length=1, max_length=100, examples=["Магазин «Пиксель»"])


class LoginRequest(Strict):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class UpdateAccountRequest(Strict):
    business_name: str = Field(min_length=1, max_length=100)


class CreateCheckoutSessionRequest(Strict):
    amount: AmountInput = Field(
        description="Сумма в минимальных единицах валюты: 25 USDT = 25000000 (6 знаков после запятой)",
        examples=["25000000"],
    )
    currency: str = Field(min_length=1, max_length=10, examples=["usdt"])
    description: str | None = Field(None, max_length=500, examples=["Футболка «Пиксель», размер M"])
    success_url: str | None = Field(
        None,
        max_length=2000,
        description="Куда вернуть покупателя после оплаты. Можно вставить {CHECKOUT_SESSION_ID}",
        examples=["https://shop.example/thanks?session={CHECKOUT_SESSION_ID}"],
    )
    cancel_url: str | None = Field(None, max_length=2000, examples=["https://shop.example/cart"])
    metadata: dict[str, str] | None = Field(None, examples=[{"order_id": "1042"}])
    expires_in_minutes: int = Field(60, ge=5, le=1440, description="Сколько живёт страница оплаты")


class CancelPaymentIntentRequest(Strict):
    cancellation_reason: CancellationReason | None = None


class CreateRefundRequest(Strict):
    payment_intent: str = Field(min_length=1, max_length=64, examples=["pi_4Fz9Qk2LmP8xR1tB7cVwQa3e"])
    amount: AmountInput | None = Field(None, description="Не указана — возвращается вся оставшаяся сумма")
    reason: RefundReason | None = None


class CreateWebhookEndpointRequest(Strict):
    url: str = Field(min_length=1, max_length=500, examples=["https://shop.example/webhooks/cryptopay"])
    enabled_events: list[str] = Field(
        min_length=1, max_length=20, examples=[["payment_intent.succeeded", "checkout.session.completed"]]
    )
    description: str | None = Field(None, max_length=200)


class StartPaymentRequest(Strict):
    customer_email: str | None = Field(None, max_length=254, description="Необязательно: для чека")


# ─────────────────────────── ответы (для документации) ───────────────────────────


class ErrorBody(BaseModel):
    type: Literal[
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "idempotency_error",
        "rate_limit_error",
        "api_error",
    ]
    code: str = Field(examples=["parameter_invalid"])
    message: str = Field(examples=["amount: обязательный параметр"])
    param: str | None = Field(None, examples=["amount"])


class ErrorResponse(BaseModel):
    error: ErrorBody


class PaymentErrorObject(BaseModel):
    code: str = Field(examples=["insufficient_funds"])
    message: str


class PaymentMethodObject(BaseModel):
    type: Literal["crypto_wallet"]
    wallet_request: str | None
    transaction: str | None = Field(description="Хэш транзакции в сети")
    payer_address: str | None
    confirmations: int
    required_confirmations: int


class PaymentIntentObject(BaseModel):
    id: str = Field(examples=["pi_4Fz9Qk2LmP8xR1tB7cVwQa3e"])
    object: Literal["payment_intent"]
    amount: str = Field(examples=["25000000"])
    amount_received: str
    amount_refunded: str
    currency: str = Field(examples=["usdt"])
    status: Literal["requires_payment_method", "requires_action", "processing", "succeeded", "canceled"]
    description: str | None
    metadata: dict[str, str]
    checkout_session: str | None
    customer_email: str | None
    payment_method: PaymentMethodObject
    last_payment_error: PaymentErrorObject | None
    cancellation_reason: CancellationReason | None
    created: int = Field(description="Unix-время")
    succeeded_at: int | None
    canceled_at: int | None
    livemode: bool


class CheckoutSessionObject(BaseModel):
    id: str = Field(examples=["cs_9aB3dE5fG7hJ1kL2mN4pQ6rS"])
    object: Literal["checkout.session"]
    url: str | None = Field(description="Страница оплаты — сюда перенаправьте покупателя")
    status: Literal["open", "complete", "expired"]
    payment_status: Literal["unpaid", "paid"]
    payment_intent: str
    amount_total: str
    currency: str
    description: str | None
    success_url: str | None
    cancel_url: str | None
    created: int
    expires_at: int
    completed_at: int | None
    livemode: bool


class RefundObject(BaseModel):
    id: str = Field(examples=["re_2Hk8LmN3pQ5rS7tV9wX1yZ4a"])
    object: Literal["refund"]
    amount: str
    currency: str
    payment_intent: str
    reason: RefundReason | None
    status: Literal["pending", "succeeded", "failed"]
    destination_address: str
    transaction: str | None
    failure_reason: str | None
    created: int
    completed_at: int | None


class BalanceAmount(BaseModel):
    amount: str
    currency: str


class BalanceObject(BaseModel):
    object: Literal["balance"]
    available: list[BalanceAmount]
    livemode: bool


class BalanceTransactionObject(BaseModel):
    id: str
    object: Literal["balance_transaction"]
    type: Literal["payment", "refund", "refund_failure"]
    source: str
    amount: str
    fee: str
    net: str
    currency: str
    description: str
    created: int


class EventData(BaseModel):
    object: dict[str, object]


class EventObject(BaseModel):
    id: str = Field(examples=["evt_7cD9eF1gH3jK5lM7nP9qR2sT"])
    object: Literal["event"]
    type: str = Field(examples=["payment_intent.succeeded"])
    api_version: str
    created: int
    data: EventData
    livemode: bool


class WebhookEndpointObject(BaseModel):
    id: str
    object: Literal["webhook_endpoint"]
    url: str
    enabled_events: list[str]
    description: str | None
    status: Literal["enabled", "disabled"]
    created: int
    secret: str | None = Field(None, description="whsec_… — возвращается только при создании")


class WebhookDeliveryObject(BaseModel):
    id: str
    object: Literal["webhook_delivery"]
    event: str
    webhook_endpoint: str
    url: str
    status: Literal["pending", "delivered", "failed"]
    attempts: int
    next_attempt_at: int | None
    last_error: str | None
    last_status_code: int | None
    delivered_at: int | None
    created: int


def list_model(name: str, item: type[BaseModel]) -> type[BaseModel]:
    """Список в формате Stripe: ``{"object": "list", "data": [...], "has_more": false, "url": "/v1/…"}``."""
    return create_model(
        name,
        object=(cast(Any, Literal["list"]), ...),
        data=(list[item], ...),  # type: ignore[valid-type]
        has_more=(bool, ...),
        url=(str, ...),
    )
