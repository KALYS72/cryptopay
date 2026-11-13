"""CheckoutSession — страница оплаты, которую CryptoPay показывает покупателю (как Stripe Checkout).

Продавец создаёт сессию через API, получает ``url`` и перенаправляет туда покупателя.
Сессия «оборачивает» PaymentIntent: хранит, куда вернуть покупателя после оплаты
(``success_url``) или при отказе (``cancel_url``), и сколько живёт страница.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from .errors import InvalidStateError, ValidationError
from .events import DomainEvent, EventType

CheckoutSessionStatus = Literal["open", "complete", "expired"]
CHECKOUT_SESSION_STATUSES: tuple[CheckoutSessionStatus, ...] = ("open", "complete", "expired")
PaymentStatus = Literal["unpaid", "paid"]

_TRANSITIONS: Mapping[CheckoutSessionStatus, tuple[CheckoutSessionStatus, ...]] = {
    "open": ("complete", "expired"),
    "complete": (),
    "expired": (),
}

MIN_TTL_MINUTES = 5
MAX_TTL_MINUTES = 24 * 60
DEFAULT_TTL_MINUTES = 60
MAX_URL_LENGTH = 2000
SESSION_ID_PLACEHOLDER = "{CHECKOUT_SESSION_ID}"
"""Как у Stripe: в success_url можно вставить этот шаблон, и мы подставим id сессии."""

_URL_RE = re.compile(r"https?://[^\s/?#]+[^\s]*")


def validate_redirect_url(value: str | None, param: str) -> str | None:
    url = (value or "").strip()
    if not url:
        return None
    if len(url) > MAX_URL_LENGTH or _URL_RE.fullmatch(url) is None:
        raise ValidationError("Адрес должен быть полным URL, например https://shop.example/thanks", param=param)
    return url


@dataclass(slots=True)
class CheckoutSessionProps:
    id: str
    merchant_id: str
    payment_intent_id: str
    amount: int
    currency: str
    description: str | None
    success_url: str | None
    cancel_url: str | None
    status: CheckoutSessionStatus
    payment_status: PaymentStatus
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None


class CheckoutSession:
    __slots__ = ("_pending_events", "_props")

    def __init__(self, props: CheckoutSessionProps) -> None:
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
        description: str | None,
        success_url: str | None,
        cancel_url: str | None,
        created_at: datetime,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> CheckoutSession:
        if isinstance(ttl_minutes, bool) or not MIN_TTL_MINUTES <= ttl_minutes <= MAX_TTL_MINUTES:
            raise ValidationError(
                f"Срок жизни страницы оплаты — от {MIN_TTL_MINUTES} минут до {MAX_TTL_MINUTES // 60} часов",
                param="expires_in_minutes",
            )
        return cls(
            CheckoutSessionProps(
                id=id,
                merchant_id=merchant_id,
                payment_intent_id=payment_intent_id,
                amount=amount,
                currency=currency,
                description=description,
                success_url=validate_redirect_url(success_url, "success_url"),
                cancel_url=validate_redirect_url(cancel_url, "cancel_url"),
                status="open",
                payment_status="unpaid",
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=ttl_minutes),
                completed_at=None,
            )
        )

    @classmethod
    def restore(cls, props: CheckoutSessionProps) -> CheckoutSession:
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
    def status(self) -> CheckoutSessionStatus:
        return self._props.status

    @property
    def expires_at(self) -> datetime:
        return self._props.expires_at

    @property
    def success_url(self) -> str | None:
        return self._props.success_url

    @property
    def cancel_url(self) -> str | None:
        return self._props.cancel_url

    @property
    def created_at(self) -> datetime:
        return self._props.created_at

    def is_open(self, now: datetime) -> bool:
        return self._props.status == "open" and now < self._props.expires_at

    def is_past_expiry(self, now: datetime) -> bool:
        return self._props.status == "open" and now >= self._props.expires_at

    def success_redirect_url(self) -> str | None:
        url = self._props.success_url
        return url.replace(SESSION_ID_PLACEHOLDER, self._props.id) if url else None

    def complete(self, now: datetime) -> None:
        self._transition("complete")
        self._props.payment_status = "paid"
        self._props.completed_at = now
        self._record("checkout.session.completed", now)

    def expire(self, now: datetime) -> None:
        self._transition("expired")
        self._record("checkout.session.expired", now)

    def pull_events(self) -> list[DomainEvent]:
        events, self._pending_events = self._pending_events, []
        return events

    def snapshot(self) -> CheckoutSessionProps:
        return replace(self._props)

    def _transition(self, to: CheckoutSessionStatus) -> None:
        current = self._props.status
        if to not in _TRANSITIONS[current]:
            raise InvalidStateError("checkout_session", current, to)
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
