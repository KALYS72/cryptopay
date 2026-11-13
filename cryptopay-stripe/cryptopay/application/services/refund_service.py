"""Возвраты: деньги уходят покупателю с кошелька платформы, баланс продавца уменьшается.

Возврат проходит в два шага, потому что внешний кошелёк нельзя «включить» в транзакцию БД:

1. транзакция БД: проверить правила, записать возврат (pending), списать сумму с баланса;
2. вне транзакции: попросить кошелёк отправить перевод — идемпотентно, по id возврата.

Если сервер упадёт между шагами, фоновая задача повторит шаг 2 — а благодаря
идемпотентности кошелёк не отправит деньги дважды.
"""

from __future__ import annotations

import contextlib

from ...domain.errors import AppError, BusinessRuleError, NotFoundError
from ...domain.ledger import BalanceTransaction
from ...domain.refund import Refund, RefundReason
from ..events.dispatcher import EventPublisher
from ..ports.repositories import ListQuery, Page, UnitOfWork
from ..ports.system import Clock, IdGenerator
from ..ports.wallet import CryptoWallet, WalletOperationError, WalletTransaction


class RefundService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        wallet: CryptoWallet,
        publisher: EventPublisher,
        required_confirmations: int,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._wallet = wallet
        self._publisher = publisher
        self._required_confirmations = required_confirmations

    async def create(
        self, merchant_id: str, payment_intent_id: str, amount: int | None, reason: RefundReason | None
    ) -> Refund:
        async with self._uow.transaction() as repos:
            intent = await repos.payment_intents.find_by_id(payment_intent_id)
            if intent is None or intent.merchant_id != merchant_id:
                raise NotFoundError("Платёж", payment_intent_id)
            refund_amount = intent.refundable_amount if amount is None else amount
            intent.reserve_refund(refund_amount)  # проверяет статус и сумму
            balance = (await repos.ledger.balances(merchant_id)).get(intent.currency, 0)
            if balance < refund_amount:
                raise BusinessRuleError(
                    "balance_insufficient", "На балансе недостаточно средств для возврата", param="amount"
                )
            now = self._clock.now()
            refund = Refund.create(
                id=self._ids.generate("re"),
                merchant_id=merchant_id,
                payment_intent_id=intent.id,
                amount=refund_amount,
                currency=intent.currency,
                reason=reason,
                destination_address=intent.payer_address or "",
                created_at=now,
            )
            await repos.refunds.insert(refund)
            await repos.payment_intents.update(intent)
            await repos.ledger.append(
                BalanceTransaction.for_refund(
                    id=self._ids.generate("txn"),
                    merchant_id=merchant_id,
                    refund_id=refund.id,
                    amount=refund_amount,
                    currency=intent.currency,
                    now=now,
                )
            )
            await self._publisher.publish(refund.pull_events(), repos)

        with contextlib.suppress(AppError):  # пробуем отправить сразу; не вышло — повторит фоновая задача
            await self.dispatch(refund.id)
        return await self.retrieve(merchant_id, refund.id)

    async def retrieve(self, merchant_id: str, refund_id: str) -> Refund:
        refund = await self._uow.repos.refunds.find_by_id(refund_id)
        if refund is None or refund.merchant_id != merchant_id:
            raise NotFoundError("Возврат", refund_id)
        return refund

    async def list(self, merchant_id: str, query: ListQuery, payment_intent_id: str | None = None) -> Page[Refund]:
        return await self._uow.repos.refunds.page(merchant_id, query, payment_intent_id)

    async def dispatch(self, refund_id: str) -> Refund | None:
        """Продвинуть возврат: отправить перевод, если ещё не отправлен, или проверить подтверждения."""
        refund = await self._uow.repos.refunds.find_by_id(refund_id)
        if refund is None or refund.status != "pending":
            return refund

        tx: WalletTransaction | None
        if refund.wallet_tx_id is None:
            try:
                tx = await self._wallet.send_refund(
                    reference=refund.id,
                    to_address=refund.destination_address,
                    amount=refund.amount,
                    currency=refund.currency,
                )
            except WalletOperationError as error:
                return await self._fail(refund.id, error.message)
            async with self._uow.transaction() as repos:
                fresh = await repos.refunds.find_by_id(refund.id)
                if fresh is None or fresh.status != "pending":
                    return fresh
                fresh.attach_transaction(tx.id)
                await repos.refunds.update(fresh)
                refund = fresh
        else:
            tx = await self._wallet.get_transaction(refund.wallet_tx_id)

        if tx is None or tx.status == "failed":
            reason = tx.failure_reason if tx and tx.failure_reason else "Перевод покупателю не прошёл в сети"
            return await self._fail(refund.id, reason)
        if tx.status == "confirmed" and tx.confirmations >= self._required_confirmations:
            async with self._uow.transaction() as repos:
                fresh = await repos.refunds.find_by_id(refund.id)
                if fresh is None or fresh.status != "pending":
                    return fresh
                fresh.mark_succeeded(self._clock.now())
                await repos.refunds.update(fresh)
                await self._publisher.publish(fresh.pull_events(), repos)
                return fresh
        return refund

    async def _fail(self, refund_id: str, reason: str) -> Refund | None:
        """Возврат не удался: деньги возвращаются на баланс продавца, платёж снова можно вернуть."""
        async with self._uow.transaction() as repos:
            refund = await repos.refunds.find_by_id(refund_id)
            if refund is None or refund.status != "pending":
                return refund
            now = self._clock.now()
            refund.mark_failed(reason, now)
            await repos.refunds.update(refund)
            intent = await repos.payment_intents.find_by_id(refund.payment_intent_id)
            if intent is not None:
                intent.release_refund(refund.amount)
                await repos.payment_intents.update(intent)
            await repos.ledger.append(
                BalanceTransaction.for_refund_failure(
                    id=self._ids.generate("txn"),
                    merchant_id=refund.merchant_id,
                    refund_id=refund.id,
                    amount=refund.amount,
                    currency=refund.currency,
                    now=now,
                )
            )
            await self._publisher.publish(refund.pull_events(), repos)
            return refund
