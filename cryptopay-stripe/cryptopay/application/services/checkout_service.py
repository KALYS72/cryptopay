"""Страница оплаты (Checkout Session): создание продавцом и оплата покупателем.

Порядок для покупателя — как в Stripe Checkout с перенаправлением на страницу банка (3-D Secure):

1. продавец создаёт сессию → покупатель открывает ``url``;
2. «Оплатить криптокошельком» → создаём запрос в кошельке → перенаправляем покупателя туда;
3. покупатель подтверждает платёж в кошельке → кошелёк возвращает его на страницу оплаты;
4. страница показывает подтверждения сети; когда их достаточно — «Оплачено» и переход на ``success_url``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ...domain.checkout_session import DEFAULT_TTL_MINUTES, CheckoutSession
from ...domain.errors import BusinessRuleError, ConcurrencyError, NotFoundError
from ...domain.merchant import normalize_email
from ...domain.money import Currency
from ...domain.payment_intent import PaymentIntent
from ..currencies import CurrencyRegistry
from ..events.dispatcher import EventPublisher
from ..ports.repositories import ListQuery, Page, UnitOfWork
from ..ports.system import Clock, IdGenerator
from ..ports.wallet import CryptoWallet, NewPaymentRequest
from ..serializers import ObjectSerializer
from .payment_service import PaymentService


@dataclass(frozen=True, slots=True)
class CheckoutConfig:
    required_confirmations: int


@dataclass(frozen=True, slots=True)
class CreateCheckoutSession:
    amount: int
    currency: str
    description: str | None = None
    success_url: str | None = None
    cancel_url: str | None = None
    metadata: Mapping[str, str] | None = None
    expires_in_minutes: int = DEFAULT_TTL_MINUTES


@dataclass(frozen=True, slots=True)
class PublicCheckout:
    """Всё, что видит покупатель на странице оплаты (без данных, которые ему знать не нужно)."""

    session: CheckoutSession
    intent: PaymentIntent
    currency: Currency
    business_name: str
    approve_url: str | None


class CheckoutService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        currencies: CurrencyRegistry,
        wallet: CryptoWallet,
        payments: PaymentService,
        publisher: EventPublisher,
        serializer: ObjectSerializer,
        config: CheckoutConfig,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._currencies = currencies
        self._wallet = wallet
        self._payments = payments
        self._publisher = publisher
        self._serializer = serializer
        self._config = config

    # ── продавец ──
    async def create(self, merchant_id: str, data: CreateCheckoutSession) -> CheckoutSession:
        currency = self._currencies.get(data.currency)
        now = self._clock.now()
        session_id = self._ids.generate("cs")
        intent = PaymentIntent.create(
            id=self._ids.generate("pi"),
            merchant_id=merchant_id,
            amount=data.amount,
            currency=currency,
            description=data.description,
            metadata=data.metadata,
            checkout_session_id=session_id,
            required_confirmations=self._config.required_confirmations,
            created_at=now,
        )
        session = CheckoutSession.create(
            id=session_id,
            merchant_id=merchant_id,
            payment_intent_id=intent.id,
            amount=intent.amount,
            currency=currency.code,
            description=intent.description,
            success_url=data.success_url,
            cancel_url=data.cancel_url,
            created_at=now,
            ttl_minutes=data.expires_in_minutes,
        )
        async with self._uow.transaction() as repos:
            await repos.payment_intents.insert(intent)
            await repos.checkout_sessions.insert(session)
            await self._publisher.publish(intent.pull_events(), repos)
        return session

    async def retrieve(self, merchant_id: str, session_id: str) -> CheckoutSession:
        session = await self._uow.repos.checkout_sessions.find_by_id(session_id)
        if session is None or session.merchant_id != merchant_id:
            raise NotFoundError("Сессия оплаты", session_id)
        return session

    async def list(self, merchant_id: str, query: ListQuery) -> Page[CheckoutSession]:
        return await self._uow.repos.checkout_sessions.page(merchant_id, query)

    async def expire(self, merchant_id: str, session_id: str) -> CheckoutSession:
        """Закрыть страницу оплаты досрочно (POST /v1/checkout/sessions/{id}/expire)."""
        await self.retrieve(merchant_id, session_id)
        return await self._expire(session_id, strict=True)

    async def expire_due(self, session_id: str) -> CheckoutSession | None:
        """Для фоновой задачи: закрыть страницу, у которой вышел срок."""
        return await self._expire(session_id, strict=False)

    # ── покупатель ──
    async def public_view(self, session_id: str) -> PublicCheckout:
        session = await self._uow.repos.checkout_sessions.find_by_id(session_id)
        if session is None:
            raise NotFoundError("Страница оплаты", session_id)
        intent = await self._intent(session)
        if intent.is_waiting_for_wallet():
            await self._payments.sync(intent.id)  # покупатель смотрит на страницу — узнаём новости сразу
        elif session.is_past_expiry(self._clock.now()):
            await self._expire(session_id, strict=False)

        session = await self._uow.repos.checkout_sessions.find_by_id(session_id) or session
        intent = await self._intent(session)
        approve_url = None
        if intent.status == "requires_action" and intent.wallet_request_id:
            request = await self._wallet.get_payment_request(intent.wallet_request_id)
            approve_url = request.approve_url if request and request.status == "pending" else None
        merchant = await self._uow.repos.merchants.find_by_id(session.merchant_id)
        return PublicCheckout(
            session=session,
            intent=intent,
            currency=self._currencies.get(intent.currency),
            business_name=merchant.business_name if merchant else "Магазин",
            approve_url=approve_url,
        )

    async def start_payment(self, session_id: str, customer_email: str | None) -> str:
        """Покупатель нажал «Оплатить криптокошельком». Возвращает адрес страницы кошелька."""
        session = await self._uow.repos.checkout_sessions.find_by_id(session_id)
        if session is None:
            raise NotFoundError("Страница оплаты", session_id)
        if not session.is_open(self._clock.now()):
            raise BusinessRuleError("checkout_session_inactive", "Эта страница оплаты больше не активна")
        email = normalize_email(customer_email) if customer_email and customer_email.strip() else None

        intent = await self._intent(session)
        if intent.status == "requires_action" and intent.wallet_request_id:
            request = await self._wallet.get_payment_request(intent.wallet_request_id)
            if request is not None and request.status == "pending":
                return request.approve_url  # уже ждём подтверждения — ведём туда же
            intent = await self._payments.sync(intent.id) or intent
        if intent.status != "requires_payment_method":
            raise BusinessRuleError("payment_in_progress", "Оплата уже выполняется — дождитесь результата")

        merchant = await self._uow.repos.merchants.find_by_id(session.merchant_id)
        request = await self._wallet.create_payment_request(
            NewPaymentRequest(
                reference=intent.id,
                merchant_name=merchant.business_name if merchant else "Магазин",
                description=intent.description,
                amount=intent.amount,
                currency=intent.currency,
                return_url=self._serializer.checkout_url(session.id),
            )
        )
        async with self._uow.transaction() as repos:
            fresh = await repos.payment_intents.find_by_id(intent.id)
            if fresh is None or fresh.version != intent.version:
                await self._wallet.cancel_payment_request(request.id)  # нас опередил параллельный запрос
                raise ConcurrencyError("Платёж", intent.id)
            fresh.start_wallet_payment(wallet_request_id=request.id, customer_email=email, now=self._clock.now())
            await repos.payment_intents.update(fresh)
            await self._publisher.publish(fresh.pull_events(), repos)
        return request.approve_url

    # ── внутреннее ──
    async def _intent(self, session: CheckoutSession) -> PaymentIntent:
        intent = await self._uow.repos.payment_intents.find_by_id(session.payment_intent_id)
        if intent is None:
            raise NotFoundError("Платёж", session.payment_intent_id)
        return intent

    async def _expire(self, session_id: str, *, strict: bool) -> CheckoutSession:
        session = await self._uow.repos.checkout_sessions.find_by_id(session_id)
        if session is None:
            raise NotFoundError("Сессия оплаты", session_id)
        if session.status != "open":
            if strict:
                raise BusinessRuleError("checkout_session_inactive", "Сессия уже завершена или истекла")
            return session
        intent = await self._intent(session)

        if intent.status == "requires_action" and intent.wallet_request_id:
            # Закрываем запрос в кошельке, чтобы покупатель не оплатил закрытую страницу.
            request = await self._wallet.cancel_payment_request(intent.wallet_request_id)
            if request is not None and request.status == "approved":
                intent = await self._payments.sync(intent.id) or intent
        if intent.status in ("processing", "succeeded"):
            if strict:
                raise BusinessRuleError(
                    "checkout_session_processing", "Покупатель уже отправил перевод — дождитесь его результата"
                )
            return session  # деньги в пути: страницу не закрываем, ждём результат

        async with self._uow.transaction() as repos:
            fresh_session = await repos.checkout_sessions.find_by_id(session_id)
            fresh_intent = await repos.payment_intents.find_by_id(session.payment_intent_id)
            if fresh_session is None or fresh_intent is None or fresh_session.status != "open":
                return fresh_session or session
            if fresh_intent.version != intent.version:
                raise ConcurrencyError("Платёж", intent.id)
            now = self._clock.now()
            fresh_session.expire(now)
            await repos.checkout_sessions.update(fresh_session)
            events = fresh_session.pull_events()
            if fresh_intent.status in ("requires_payment_method", "requires_action"):
                fresh_intent.cancel("abandoned", now)
                await repos.payment_intents.update(fresh_intent)
                events = fresh_intent.pull_events() + events
            await self._publisher.publish(events, repos)
        return fresh_session
