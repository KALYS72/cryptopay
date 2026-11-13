"""Платежи (PaymentIntent): просмотр, отмена и сверка с кошельком.

Сверка (``sync``) — сердце системы. Покупателю и кошельку на слово не верим:
сервер сам спрашивает у кошелька, что стало с запросом на оплату, и сам смотрит
транзакцию — её сумму, валюту, статус и число подтверждений. Только после
нужного числа подтверждений платёж становится успешным.

SOLID (S): здесь только правила платежа. HTTP — в routes, SQL — в репозиториях,
устройство кошелька — в адаптере за портом CryptoWallet.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.checkout_session import CheckoutSession
from ...domain.errors import InvalidStateError, NotFoundError
from ...domain.ledger import BalanceTransaction
from ...domain.payment_intent import CancellationReason, PaymentIntent, PaymentIntentStatus
from ..events.dispatcher import EventPublisher
from ..ports.repositories import ListQuery, Page, Repositories, UnitOfWork
from ..ports.system import Clock, IdGenerator
from ..ports.wallet import CryptoWallet, WalletPaymentRequest, WalletTransaction

DEFAULT_FAILURE_MESSAGES: dict[str, str] = {
    "payment_rejected": "Покупатель отклонил платёж в кошельке",
    "insufficient_funds": "Недостаточно средств в кошельке",
    "wallet_declined": "Кошелёк отклонил платёж",
    "wallet_request_expired": "Время на подтверждение платежа в кошельке истекло",
    "wallet_request_canceled": "Запрос на оплату отменён",
    "wallet_request_not_found": "Кошелёк не нашёл запрос на оплату",
    "transaction_failed": "Транзакция не прошла в сети",
    "transaction_not_found": "Транзакция пропала из сети",
    "amount_mismatch": "Сумма или валюта транзакции не совпадает с платежом",
}


@dataclass(frozen=True, slots=True)
class PaymentConfig:
    fee_bps: int
    """Комиссия платформы в базисных пунктах: 100 = 1 %."""


@dataclass(frozen=True, slots=True)
class WalletObservation:
    """Что мы узнали у кошелька. Запросы к кошельку делаются ДО транзакции БД — она не держится открытой,
    пока внешний сервис думает."""

    request: WalletPaymentRequest | None
    transaction: WalletTransaction | None


class PaymentService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        wallet: CryptoWallet,
        publisher: EventPublisher,
        config: PaymentConfig,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._wallet = wallet
        self._publisher = publisher
        self._config = config

    # ── чтение ──
    async def retrieve(self, merchant_id: str, intent_id: str) -> PaymentIntent:
        intent = await self._uow.repos.payment_intents.find_by_id(intent_id)
        # Чужой платёж — «не найден», а не «запрещено»: не подтверждаем, что такой id существует.
        if intent is None or intent.merchant_id != merchant_id:
            raise NotFoundError("Платёж", intent_id)
        return intent

    async def list(
        self, merchant_id: str, query: ListQuery, status: PaymentIntentStatus | None = None
    ) -> Page[PaymentIntent]:
        return await self._uow.repos.payment_intents.page(merchant_id, query, status)

    # ── отмена ──
    async def cancel(self, merchant_id: str, intent_id: str, reason: CancellationReason | None) -> PaymentIntent:
        intent = await self.retrieve(merchant_id, intent_id)
        if intent.status == "requires_action" and intent.wallet_request_id:
            # Сначала закрываем запрос в кошельке, иначе покупатель мог бы оплатить отменённый платёж.
            request = await self._wallet.cancel_payment_request(intent.wallet_request_id)
            if request is not None and request.status == "approved":
                await self.sync(intent.id)
                raise InvalidStateError("payment_intent", "processing", "canceled")

        async with self._uow.transaction() as repos:
            fresh = await self._load(repos, intent_id)
            fresh.cancel(reason, self._clock.now())
            await repos.payment_intents.update(fresh)
            events = fresh.pull_events()
            session = await self._session_of(repos, fresh)
            if session is not None and session.status == "open":
                session.expire(self._clock.now())
                await repos.checkout_sessions.update(session)
                events += session.pull_events()
            await self._publisher.publish(events, repos)
        return fresh

    # ── сверка с кошельком ──
    async def sync(self, intent_id: str) -> PaymentIntent | None:
        """Узнать у кошелька судьбу текущей попытки оплаты и применить результат.

        Безопасно вызывать сколько угодно раз и параллельно: если объект успели
        изменить, пока мы ждали кошелёк, результат этого прохода просто отбрасывается.
        """
        intent = await self._uow.repos.payment_intents.find_by_id(intent_id)
        if intent is None or not intent.is_waiting_for_wallet():
            return intent
        observation = await self._observe(intent)

        async with self._uow.transaction() as repos:
            fresh = await repos.payment_intents.find_by_id(intent_id)
            if fresh is None or fresh.version != intent.version:
                return fresh  # кто-то уже обработал это состояние
            if not self._apply(fresh, observation):
                return fresh
            await repos.payment_intents.update(fresh)
            events = fresh.pull_events()
            if fresh.status == "succeeded":
                await repos.ledger.append(
                    BalanceTransaction.for_payment(
                        id=self._ids.generate("txn"),
                        merchant_id=fresh.merchant_id,
                        payment_intent_id=fresh.id,
                        amount=fresh.amount_received,
                        currency=fresh.currency,
                        fee_bps=self._config.fee_bps,
                        now=self._clock.now(),
                    )
                )
                session = await self._session_of(repos, fresh)
                if session is not None and session.status == "open":
                    session.complete(self._clock.now())
                    await repos.checkout_sessions.update(session)
                    events += session.pull_events()
            await self._publisher.publish(events, repos)
        return fresh

    async def _observe(self, intent: PaymentIntent) -> WalletObservation:
        request: WalletPaymentRequest | None = None
        tx_id = intent.wallet_tx_id
        if intent.status == "requires_action" and intent.wallet_request_id:
            request = await self._wallet.get_payment_request(intent.wallet_request_id)
            if request is not None and request.status == "approved":
                tx_id = request.tx_id
        transaction = await self._wallet.get_transaction(tx_id) if tx_id else None
        return WalletObservation(request=request, transaction=transaction)

    def _apply(self, intent: PaymentIntent, seen: WalletObservation) -> bool:
        """Применить наблюдение к платежу. Возвращает True, если платёж изменился."""
        now = self._clock.now()
        changed = False
        if intent.status == "requires_action":
            request = seen.request
            if request is None:
                self._fail(intent, "wallet_request_not_found", None)
                return True
            if request.status == "pending":
                return False
            if request.status != "approved" or not request.tx_id or not request.payer_address:
                self._fail(intent, request.failure_code or "wallet_declined", request.failure_message)
                return True
            intent.mark_processing(tx_id=request.tx_id, payer_address=request.payer_address, now=now)
            changed = True

        tx = seen.transaction
        if tx is None:
            self._fail(intent, "transaction_not_found", None)
            return True
        if tx.status == "failed":
            self._fail(intent, "transaction_failed", tx.failure_reason)
            return True
        if tx.amount != intent.amount or tx.currency != intent.currency:
            self._fail(intent, "amount_mismatch", None)
            return True
        changed = intent.update_confirmations(tx.confirmations) or changed
        if tx.status == "confirmed" and intent.has_enough_confirmations():
            intent.mark_succeeded(now)
            changed = True
        return changed

    def _fail(self, intent: PaymentIntent, code: str, message: str | None) -> None:
        text = message or DEFAULT_FAILURE_MESSAGES.get(code, "Платёж не прошёл")
        intent.fail_payment(code=code, message=text, now=self._clock.now())

    # ── вспомогательное ──
    @staticmethod
    async def _load(repos: Repositories, intent_id: str) -> PaymentIntent:
        intent = await repos.payment_intents.find_by_id(intent_id)
        if intent is None:
            raise NotFoundError("Платёж", intent_id)
        return intent

    @staticmethod
    async def _session_of(repos: Repositories, intent: PaymentIntent) -> CheckoutSession | None:
        if intent.checkout_session_id is None:
            return None
        return await repos.checkout_sessions.find_by_id(intent.checkout_session_id)
