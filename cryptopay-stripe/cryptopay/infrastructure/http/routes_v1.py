"""REST API /v1 для сервера магазина — по образцу Stripe API.

SOLID (S): обработчики только переводят HTTP в вызовы сервисов и обратно.
Ни одного бизнес-правила здесь нет.
"""

# Без `from __future__ import annotations`: FastAPI читает аннотации параметров во время работы.

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response

from ...application.services.checkout_service import CreateCheckoutSession
from ...domain.events import EVENT_TYPES
from ...domain.money import parse_amount
from ...domain.payment_intent import PaymentIntentStatus
from .dependencies import ApiKeyOrSession, ContainerDep
from .helpers import idempotent, json_response, list_query, plain_list, stripe_list
from .schemas import (
    BalanceObject,
    BalanceTransactionObject,
    CancelPaymentIntentRequest,
    CheckoutSessionObject,
    CreateCheckoutSessionRequest,
    CreateRefundRequest,
    CreateWebhookEndpointRequest,
    ErrorResponse,
    EventObject,
    PaymentIntentObject,
    RefundObject,
    WebhookDeliveryObject,
    WebhookEndpointObject,
    list_model,
)

ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Некорректный запрос"},
    401: {"model": ErrorResponse, "description": "Нет или неверный ключ"},
    404: {"model": ErrorResponse, "description": "Объект не найден"},
}
IDEMPOTENCY_NOTE = (
    "\n\nПоддерживает заголовок `Idempotency-Key`: повтор с тем же ключом вернёт сохранённый ответ, "
    "а не выполнит операцию второй раз."
)

Limit = Annotated[int | None, Query(ge=1, le=100, description="Сколько объектов вернуть (1–100, по умолчанию 10)")]
StartingAfter = Annotated[str | None, Query(max_length=64, description="id последнего объекта предыдущей страницы")]


def v1_router() -> APIRouter:
    router = APIRouter(prefix="/v1", responses=ERRORS)

    # ── Checkout Sessions ──
    @router.post(
        "/checkout/sessions",
        tags=["Checkout Sessions"],
        summary="Создать страницу оплаты",
        description="Создаёт сессию оплаты и PaymentIntent. Перенаправьте покупателя на `url`." + IDEMPOTENCY_NOTE,
        status_code=200,
        responses={200: {"model": CheckoutSessionObject}},
        response_class=Response,
    )
    async def create_session(
        body: CreateCheckoutSessionRequest, request: Request, principal: ApiKeyOrSession, container: ContainerDep
    ) -> Response:
        async def produce() -> dict[str, Any]:
            session = await container.services.checkout.create(
                principal.merchant_id,
                CreateCheckoutSession(
                    amount=parse_amount(body.amount),
                    currency=body.currency,
                    description=body.description,
                    success_url=body.success_url,
                    cancel_url=body.cancel_url,
                    metadata=body.metadata,
                    expires_in_minutes=body.expires_in_minutes,
                ),
            )
            return container.serializer.checkout_session(session)

        return await idempotent(request, container, principal, produce)

    @router.get(
        "/checkout/sessions",
        tags=["Checkout Sessions"],
        summary="Список сессий оплаты",
        responses={200: {"model": list_model("CheckoutSessionList", CheckoutSessionObject)}},
        response_class=Response,
    )
    async def list_sessions(
        principal: ApiKeyOrSession, container: ContainerDep, limit: Limit = None, starting_after: StartingAfter = None
    ) -> Response:
        page = await container.services.checkout.list(principal.merchant_id, list_query(limit, starting_after))
        return json_response(stripe_list(page, "/v1/checkout/sessions", container.serializer.checkout_session))

    @router.get(
        "/checkout/sessions/{session_id}",
        tags=["Checkout Sessions"],
        summary="Получить сессию оплаты",
        responses={200: {"model": CheckoutSessionObject}},
        response_class=Response,
    )
    async def get_session(session_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        session = await container.services.checkout.retrieve(principal.merchant_id, session_id)
        return json_response(container.serializer.checkout_session(session))

    @router.post(
        "/checkout/sessions/{session_id}/expire",
        tags=["Checkout Sessions"],
        summary="Закрыть страницу оплаты досрочно",
        description="Неоплаченный PaymentIntent отменяется с причиной `abandoned`.",
        responses={200: {"model": CheckoutSessionObject}},
        response_class=Response,
    )
    async def expire_session(session_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        session = await container.services.checkout.expire(principal.merchant_id, session_id)
        return json_response(container.serializer.checkout_session(session))

    # ── Payment Intents ──
    @router.get(
        "/payment_intents",
        tags=["Payment Intents"],
        summary="Список платежей",
        responses={200: {"model": list_model("PaymentIntentList", PaymentIntentObject)}},
        response_class=Response,
    )
    async def list_intents(
        principal: ApiKeyOrSession,
        container: ContainerDep,
        status: Annotated[PaymentIntentStatus | None, Query()] = None,
        limit: Limit = None,
        starting_after: StartingAfter = None,
    ) -> Response:
        page = await container.services.payments.list(principal.merchant_id, list_query(limit, starting_after), status)
        return json_response(stripe_list(page, "/v1/payment_intents", container.serializer.payment_intent))

    @router.get(
        "/payment_intents/{intent_id}",
        tags=["Payment Intents"],
        summary="Получить платёж",
        responses={200: {"model": PaymentIntentObject}},
        response_class=Response,
    )
    async def get_intent(intent_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        intent = await container.services.payments.retrieve(principal.merchant_id, intent_id)
        return json_response(container.serializer.payment_intent(intent))

    @router.post(
        "/payment_intents/{intent_id}/cancel",
        tags=["Payment Intents"],
        summary="Отменить платёж",
        description="Можно, пока покупатель не отправил перевод (статусы requires_payment_method и requires_action)."
        + IDEMPOTENCY_NOTE,
        responses={200: {"model": PaymentIntentObject}},
        response_class=Response,
    )
    async def cancel_intent(
        intent_id: str,
        request: Request,
        principal: ApiKeyOrSession,
        container: ContainerDep,
        body: Annotated[CancelPaymentIntentRequest | None, Body()] = None,
    ) -> Response:
        reason = body.cancellation_reason if body else None

        async def produce() -> dict[str, Any]:
            intent = await container.services.payments.cancel(principal.merchant_id, intent_id, reason)
            return container.serializer.payment_intent(intent)

        return await idempotent(request, container, principal, produce)

    # ── Refunds ──
    @router.post(
        "/refunds",
        tags=["Refunds"],
        summary="Вернуть деньги покупателю",
        description="Полный или частичный возврат успешного платежа на кошелёк покупателя." + IDEMPOTENCY_NOTE,
        responses={200: {"model": RefundObject}},
        response_class=Response,
    )
    async def create_refund(
        body: CreateRefundRequest, request: Request, principal: ApiKeyOrSession, container: ContainerDep
    ) -> Response:
        async def produce() -> dict[str, Any]:
            refund = await container.services.refunds.create(
                principal.merchant_id,
                body.payment_intent,
                None if body.amount is None else parse_amount(body.amount),
                body.reason,
            )
            return container.serializer.refund(refund)

        return await idempotent(request, container, principal, produce)

    @router.get(
        "/refunds",
        tags=["Refunds"],
        summary="Список возвратов",
        responses={200: {"model": list_model("RefundList", RefundObject)}},
        response_class=Response,
    )
    async def list_refunds(
        principal: ApiKeyOrSession,
        container: ContainerDep,
        payment_intent: Annotated[str | None, Query(max_length=64)] = None,
        limit: Limit = None,
        starting_after: StartingAfter = None,
    ) -> Response:
        page = await container.services.refunds.list(
            principal.merchant_id, list_query(limit, starting_after), payment_intent
        )
        return json_response(stripe_list(page, "/v1/refunds", container.serializer.refund))

    @router.get(
        "/refunds/{refund_id}",
        tags=["Refunds"],
        summary="Получить возврат",
        responses={200: {"model": RefundObject}},
        response_class=Response,
    )
    async def get_refund(refund_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        refund = await container.services.refunds.retrieve(principal.merchant_id, refund_id)
        return json_response(container.serializer.refund(refund))

    # ── Balance ──
    @router.get(
        "/balance",
        tags=["Balance"],
        summary="Баланс продавца",
        description="Сумма всех проводок по каждой валюте (оплаты минус комиссия, минус возвраты).",
        responses={200: {"model": BalanceObject}},
        response_class=Response,
    )
    async def get_balance(principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        balances = await container.services.balance.balances(principal.merchant_id)
        codes = [c.code for c in container.currencies.all()]
        return json_response(container.serializer.balance(balances, codes))

    @router.get(
        "/balance_transactions",
        tags=["Balance"],
        summary="Журнал проводок",
        responses={200: {"model": list_model("BalanceTransactionList", BalanceTransactionObject)}},
        response_class=Response,
    )
    async def list_balance_transactions(
        principal: ApiKeyOrSession, container: ContainerDep, limit: Limit = None, starting_after: StartingAfter = None
    ) -> Response:
        page = await container.services.balance.transactions(principal.merchant_id, list_query(limit, starting_after))
        return json_response(stripe_list(page, "/v1/balance_transactions", container.serializer.balance_transaction))

    # ── Events ──
    @router.get(
        "/events",
        tags=["Events"],
        summary="Журнал событий",
        description="Все события аккаунта, от новых к старым. Те же объекты приходят в вебхуках. "
        "Известные типы: " + ", ".join(f"`{t}`" for t in EVENT_TYPES),
        responses={200: {"model": list_model("EventList", EventObject)}},
        response_class=Response,
    )
    async def list_events(
        principal: ApiKeyOrSession,
        container: ContainerDep,
        type: Annotated[str | None, Query(max_length=64)] = None,
        related_object: Annotated[
            str | None, Query(max_length=64, description="Только события объекта pi_…/cs_…/re_…")
        ] = None,
        limit: Limit = None,
        starting_after: StartingAfter = None,
    ) -> Response:
        page = await container.services.webhooks.list_events(
            principal.merchant_id, list_query(limit, starting_after), type=type, subject_id=related_object
        )
        return json_response(stripe_list(page, "/v1/events", container.serializer.event))

    @router.get(
        "/events/{event_id}",
        tags=["Events"],
        summary="Получить событие",
        responses={200: {"model": EventObject}},
        response_class=Response,
    )
    async def get_event(event_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        event = await container.services.webhooks.get_event(principal.merchant_id, event_id)
        return json_response(container.serializer.event(event))

    @router.get(
        "/events/{event_id}/deliveries",
        tags=["Events"],
        summary="Доставки события на вебхуки",
        responses={200: {"model": list_model("WebhookDeliveryList", WebhookDeliveryObject)}},
        response_class=Response,
    )
    async def list_deliveries(event_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        deliveries = await container.services.webhooks.list_deliveries(principal.merchant_id, event_id)
        url = f"/v1/events/{event_id}/deliveries"
        return json_response(plain_list(deliveries, url, container.serializer.webhook_delivery))

    # ── Webhook Endpoints ──
    @router.post(
        "/webhook_endpoints",
        tags=["Webhook Endpoints"],
        summary="Добавить адрес для вебхуков",
        description="Ответ содержит `secret` (whsec_…) — он показывается только один раз.",
        responses={200: {"model": WebhookEndpointObject}},
        response_class=Response,
    )
    async def create_endpoint(
        body: CreateWebhookEndpointRequest, principal: ApiKeyOrSession, container: ContainerDep
    ) -> Response:
        endpoint = await container.services.webhooks.create_endpoint(
            principal.merchant_id, url=body.url, enabled_events=body.enabled_events, description=body.description
        )
        return json_response(container.serializer.webhook_endpoint(endpoint, include_secret=True))

    @router.get(
        "/webhook_endpoints",
        tags=["Webhook Endpoints"],
        summary="Адреса для вебхуков",
        responses={200: {"model": list_model("WebhookEndpointList", WebhookEndpointObject)}},
        response_class=Response,
    )
    async def list_endpoints(principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        endpoints = await container.services.webhooks.list_endpoints(principal.merchant_id)
        return json_response(plain_list(endpoints, "/v1/webhook_endpoints", container.serializer.webhook_endpoint))

    @router.delete(
        "/webhook_endpoints/{endpoint_id}",
        tags=["Webhook Endpoints"],
        summary="Удалить адрес",
        response_class=Response,
    )
    async def delete_endpoint(endpoint_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        endpoint = await container.services.webhooks.delete_endpoint(principal.merchant_id, endpoint_id)
        return json_response({"id": endpoint.id, "object": "webhook_endpoint", "deleted": True})

    @router.post(
        "/webhook_endpoints/{endpoint_id}/test",
        tags=["Webhook Endpoints"],
        summary="Отправить тестовое событие",
        description="Событие `webhook_endpoint.test` уйдёт только на этот адрес.",
        responses={200: {"model": WebhookDeliveryObject}},
        response_class=Response,
    )
    async def test_endpoint(endpoint_id: str, principal: ApiKeyOrSession, container: ContainerDep) -> Response:
        delivery = await container.services.webhooks.send_test(principal.merchant_id, endpoint_id)
        return json_response(container.serializer.webhook_delivery(delivery))

    return router
