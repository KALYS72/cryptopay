"""Refund — возврат денег покупателю по успешному платежу (полный или частичный)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from .errors import BusinessRuleError, InvalidStateError
from .events import DomainEvent, EventType

RefundStatus = Literal["pending", "succeeded", "failed"]
RefundReason = Literal["requested_by_customer", "duplicate", "fraudulent"]
REFUND_REASONS: tuple[RefundReason, ...] = ("requested_by_customer", "duplicate", "fraudulent")

_TRANSITIONS: Mapping[RefundStatus, tuple[RefundStatus, ...]] = {
    "pending": ("succeeded", "failed"),
    "succeeded": (),
    "failed": (),
}


@dataclass(slots=True)
class RefundProps:
    id: str
    merchant_id: str
    payment_intent_id: str
    amount: int
    currency: str
    reason: RefundReason | None
    status: RefundStatus
    destination_address: str
    """Кошелёк покупателя, с которого пришла оплата: возврат уходит туда же."""
    wallet_tx_id: str | None
    failure_reason: str | None
    created_at: datetime
    completed_at: datetime | None


class Refund:
    __slots__ = ("_pending_events", "_props")

    def __init__(self, props: RefundProps) -> None:
        self._props = props
        self._pending_events: list[DomainEvent] = []

    @classmethod
    def create(
        cls,
        *,
        id: str,
        merchant_id: str,
        payment_intent_id: str,
        amount: int,
        currency: str,
        reason: RefundReason | None,
        destination_address: str,
        created_at: datetime,
    ) -> Refund:
        refund = cls(
            RefundProps(
                id=id,
                merchant_id=merchant_id,
                payment_intent_id=payment_intent_id,
                amount=amount,
                currency=currency,
                reason=reason,
                status="pending",
                destination_address=destination_address,
                wallet_tx_id=None,
                failure_reason=None,
                created_at=created_at,
                completed_at=None,
            )
        )
        refund._record("refund.created", created_at)
        return refund

    @classmethod
    def restore(cls, props: RefundProps) -> Refund:
        return cls(replace(props))

    @property
    def id(self) -> str:
        return self._props.id

    @property
    def merchant_id(self) -> str:
        return self._props.merchant_id

    @property
    def payment_intent_id(self) -> str:
        return self._props.payment_intent_id

    @property
    def amount(self) -> int:
        return self._props.amount

    @property
    def currency(self) -> str:
        return self._props.currency

    @property
    def status(self) -> RefundStatus:
        return self._props.status

    @property
    def destination_address(self) -> str:
        return self._props.destination_address

    @property
    def wallet_tx_id(self) -> str | None:
        return self._props.wallet_tx_id

    @property
    def created_at(self) -> datetime:
        return self._props.created_at

    def attach_transaction(self, tx_id: str) -> None:
        """Кошелёк платформы отправил перевод покупателю — запоминаем транзакцию."""
        if self._props.status != "pending":
            raise InvalidStateError("refund", self._props.status, "pending")
        if self._props.wallet_tx_id not in (None, tx_id):
            raise BusinessRuleError("refund_already_sent", "Для этого возврата уже отправлена другая транзакция")
        self._props.wallet_tx_id = tx_id

    def mark_succeeded(self, now: datetime) -> None:
        self._transition("succeeded")
        self._props.completed_at = now
        self._record("refund.succeeded", now)

    def mark_failed(self, reason: str, now: datetime) -> None:
        self._transition("failed")
        self._props.failure_reason = reason
        self._props.completed_at = now
        self._record("refund.failed", now)

    def pull_events(self) -> list[DomainEvent]:
        events, self._pending_events = self._pending_events, []
        return events

    def snapshot(self) -> RefundProps:
        return replace(self._props)

    def _transition(self, to: RefundStatus) -> None:
        current = self._props.status
        if to not in _TRANSITIONS[current]:
            raise InvalidStateError("refund", current, to)
        self._props.status = to

    def _record(self, type: EventType, occurred_at: datetime) -> None:
        self._pending_events.append(
            DomainEvent(
                type=type,
                merchant_id=self._props.merchant_id,
                subject_id=self._props.id,
                occurred_at=occurred_at,
                subject=self.snapshot(),
            )
        )
