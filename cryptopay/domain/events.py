"""Доменные события — факты, которые уже произошли с объектами системы.

Сущности (PaymentIntent, CheckoutSession, Refund) только «записывают» события,
а кто и как на них реагирует — журнал событий, вебхуки продавцу, что угодно ещё —
решают подписчики в прикладном слое. Это паттерн Observer и принцип
открытости/закрытости: новая реакция = новый подписчик, сами сущности не меняются.

Названия событий — как у Stripe: ``<объект>.<что произошло>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

EventType = Literal[
    "payment_intent.created",
    "payment_intent.requires_action",
    "payment_intent.processing",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "payment_intent.canceled",
    "checkout.session.completed",
    "checkout.session.expired",
    "refund.created",
    "refund.succeeded",
    "refund.failed",
]

EVENT_TYPES: tuple[EventType, ...] = (
    "payment_intent.created",
    "payment_intent.requires_action",
    "payment_intent.processing",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "payment_intent.canceled",
    "checkout.session.completed",
    "checkout.session.expired",
    "refund.created",
    "refund.succeeded",
    "refund.failed",
)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    type: EventType
    merchant_id: str
    subject_id: str
    """id объекта, с которым это произошло (pi_…, cs_…, re_…)."""
    occurred_at: datetime
    subject: object
    """Снимок состояния объекта в момент события (копия его свойств).

    Из снимка прикладной слой строит поле ``data.object`` события — ровно так,
    как объект выглядел, когда событие случилось, а не когда его отправили.
    """
