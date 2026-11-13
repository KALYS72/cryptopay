"""Фоновые задачи, которые доводят до конца то, что не может закончиться за один HTTP-запрос."""

from __future__ import annotations

from ..ports.repositories import UnitOfWork
from ..ports.system import Clock
from ..services.checkout_service import CheckoutService
from ..services.idempotency_service import IdempotencyService
from ..services.payment_service import PaymentService
from ..services.refund_service import RefundService
from .base import Job, JobError, JobReport


class PaymentSyncJob(Job):
    """Сверка с кошельком всех платежей, которые ждут подтверждения покупателя или подтверждений сети.

    Страница оплаты тоже сверяет «свой» платёж, пока покупатель на неё смотрит, но покупатель
    может закрыть вкладку сразу после подтверждения — тогда платёж доведёт до конца эта задача.
    """

    name = "payment-sync"

    def __init__(self, *, uow: UnitOfWork, payments: PaymentService, batch_size: int = 50) -> None:
        self._uow = uow
        self._payments = payments
        self._batch_size = batch_size

    async def run(self) -> JobReport:
        report = JobReport()
        for intent in await self._uow.repos.payment_intents.list_waiting_for_wallet(self._batch_size):
            report.processed += 1
            try:
                updated = await self._payments.sync(intent.id)
            except Exception as error:
                report.errors.append(JobError(id=intent.id, message=str(error)))
                continue
            if updated is not None and (updated.status != intent.status or updated.version != intent.version):
                report.changed += 1
        return report


class RefundSyncJob(Job):
    """Отправка возвратов, которые не удалось отправить сразу, и проверка их подтверждений."""

    name = "refund-sync"

    def __init__(self, *, uow: UnitOfWork, refunds: RefundService, batch_size: int = 50) -> None:
        self._uow = uow
        self._refunds = refunds
        self._batch_size = batch_size

    async def run(self) -> JobReport:
        report = JobReport()
        for refund in await self._uow.repos.refunds.list_pending(self._batch_size):
            report.processed += 1
            try:
                updated = await self._refunds.dispatch(refund.id)
            except Exception as error:
                report.errors.append(JobError(id=refund.id, message=str(error)))
                continue
            if updated is not None and updated.status != refund.status:
                report.changed += 1
        return report


class SessionExpiryJob(Job):
    """Закрывает страницы оплаты с истёкшим сроком; неоплаченный платёж отменяется (abandoned)."""

    name = "session-expiry"

    def __init__(self, *, uow: UnitOfWork, clock: Clock, checkout: CheckoutService, batch_size: int = 50) -> None:
        self._uow = uow
        self._clock = clock
        self._checkout = checkout
        self._batch_size = batch_size

    async def run(self) -> JobReport:
        report = JobReport()
        for session in await self._uow.repos.checkout_sessions.find_past_expiry(self._clock.now(), self._batch_size):
            report.processed += 1
            try:
                updated = await self._checkout.expire_due(session.id)
            except Exception as error:
                report.errors.append(JobError(id=session.id, message=str(error)))
                continue
            if updated is not None and updated.status != session.status:
                report.changed += 1
        return report


class IdempotencyCleanupJob(Job):
    """Удаляет сохранённые ответы идемпотентных запросов старше 24 часов."""

    name = "idempotency-cleanup"

    def __init__(self, idempotency: IdempotencyService) -> None:
        self._idempotency = idempotency

    async def run(self) -> JobReport:
        removed = await self._idempotency.cleanup()
        return JobReport(processed=removed, changed=removed)
