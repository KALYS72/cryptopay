"""PaymentIntent — «намерение оплатить»: главная сущность системы, как в Stripe.

Один PaymentIntent = одна сумма, которую продавец хочет получить. Покупатель может
пытаться оплатить её несколько раз (например, сначала не хватило денег в кошельке,
потом выбрал другой) — неудачная попытка возвращает намерение в
``requires_payment_method``, а не «ломает» его.

Жизненный цикл — конечный автомат (см. docs/diagrams/09-state-payment-intent.puml).
Все переходы проходят через один метод ``_transition()``, который сверяется
с таблицей ``_TRANSITIONS``: провести успешным отменённый платёж физически
невозможно, такая попытка закончится InvalidStateError.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from .errors import BusinessRuleError, InvalidStateError, ValidationError
from .events import DomainEvent, EventType
from .money import Currency

PaymentIntentStatus = Literal["requires_payment_method", "requires_action", "processing", "succeeded", "canceled"]
PAYMENT_INTENT_STATUSES: tuple[PaymentIntentStatus, ...] = (
    "requires_payment_method",
    "requires_action",
    "processing",
    "succeeded",
    "canceled",
)

_TRANSITIONS: Mapping[PaymentIntentStatus, tuple[PaymentIntentStatus, ...]] = {
    # ждём, когда покупатель выберет кошелёк
    "requires_payment_method": ("requires_action", "canceled"),
    # запрос отправлен в кошелёк, покупатель должен подтвердить его там
    "requires_action": ("processing", "requires_payment_method", "canceled"),
    # перевод отправлен в сеть, ждём подтверждений; отменить уже нельзя — деньги в пути
    "processing": ("succeeded", "requires_payment_method"),
    "succeeded": (),
    "canceled": (),
}

CancellationReason = Literal["requested_by_customer", "abandoned", "duplicate", "fraudulent"]
CANCELLATION_REASONS: tuple[CancellationReason, ...] = ("requested_by_customer", "abandoned", "duplicate", "fraudulent")

MAX_DESCRIPTION_LENGTH = 500
MAX_METADATA_KEYS = 20
MAX_METADATA_KEY_LENGTH = 40
MAX_METADATA_VALUE_LENGTH = 500


@dataclass(frozen=True, slots=True)
class PaymentError:
    """Причина последней неудачной попытки (last_payment_error в Stripe)."""

    code: str
    message: str


def validate_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    """Метаданные — произвольные пары «ключ — значение» продавца (номер заказа и т. п.)."""
    result = dict(metadata or {})
    if len(result) > MAX_METADATA_KEYS:
        raise ValidationError(f"В metadata не больше {MAX_METADATA_KEYS} ключей", param="metadata")
    for key, value in result.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationError("Ключи и значения metadata должны быть строками", param="metadata")
        if not key or len(key) > MAX_METADATA_KEY_LENGTH:
            raise ValidationError(f"Ключ metadata — от 1 до {MAX_METADATA_KEY_LENGTH} символов", param="metadata")
        if len(value) > MAX_METADATA_VALUE_LENGTH:
            raise ValidationError(
                f"Значение metadata — не длиннее {MAX_METADATA_VALUE_LENGTH} символов", param=f"metadata[{key}]"
            )
    return result


@dataclass(slots=True)
class PaymentIntentProps:
    id: str
    merchant_id: str
    amount: int
    """Сумма в минимальных единицах валюты."""
    currency: str
    description: str | None
    metadata: dict[str, str]
    status: PaymentIntentStatus
    checkout_session_id: str | None
    customer_email: str | None
    wallet_request_id: str | None
    """Запрос на оплату в кошельке покупателя (пока он не подтверждён)."""
    wallet_tx_id: str | None
    """Транзакция в сети, которую создал кошелёк после подтверждения."""
    payer_address: str | None
    confirmations: int
    required_confirmations: int
    amount_received: int
    amount_refunded: int
    last_payment_error: PaymentError | None
    cancellation_reason: CancellationReason | None
    created_at: datetime
    succeeded_at: datetime | None
    canceled_at: datetime | None
    version: int
    """Версия для оптимистической блокировки."""


class PaymentIntent:
    __slots__ = ("_pending_events", "_props")

    def __init__(self, props: PaymentIntentProps) -> None:
        """Не вызывайте напрямую: используйте PaymentIntent.create() или PaymentIntent.restore()."""
        self._props = props
        self._pending_events: list[DomainEvent] = []

    @classmethod
    def create(
        cls,
        *,
        id: str,
        merchant_id: str,
        amount: int,
        currency: Currency,
        description: str | None,
        metadata: Mapping[str, str] | None,
        checkout_session_id: str | None,
        required_confirmations: int,
        created_at: datetime,
    ) -> PaymentIntent:
        if amount < currency.min_amount:
            raise BusinessRuleError(
                "amount_too_small",
                f"Минимальная сумма платежа в {currency.symbol} — {currency.min_amount} минимальных единиц",
                param="amount",
            )
        text = (description or "").strip() or None
        if text is not None and len(text) > MAX_DESCRIPTION_LENGTH:
            raise ValidationError(f"Описание длиннее {MAX_DESCRIPTION_LENGTH} символов", param="description")
        intent = cls(
            PaymentIntentProps(
                id=id,
                merchant_id=merchant_id,
                amount=amount,
                currency=currency.code,
                description=text,
                metadata=validate_metadata(metadata),
                status="requires_payment_method",
                checkout_session_id=checkout_session_id,
                customer_email=None,
                wallet_request_id=None,
                wallet_tx_id=None,
                payer_address=None,
                confirmations=0,
                required_confirmations=required_confirmations,
                amount_received=0,
                amount_refunded=0,
                last_payment_error=None,
                cancellation_reason=None,
                created_at=created_at,
                succeeded_at=None,
                canceled_at=None,
                version=0,
            )
        )
        intent._record("payment_intent.created", created_at)
        return intent

    @classmethod
    def restore(cls, props: PaymentIntentProps) -> PaymentIntent:
        """Восстановление из хранилища: никаких событий и проверок, данные уже валидны."""
        return cls(replace(props, metadata=dict(props.metadata)))

    # ── чтение состояния ──
    @property
    def id(self) -> str:
        return self._props.id

    @property
    def merchant_id(self) -> str:
        return self._props.merchant_id

    @property
    def amount(self) -> int:
        return self._props.amount

    @property
    def currency(self) -> str:
        return self._props.currency

    @property
    def status(self) -> PaymentIntentStatus:
        return self._props.status

    @property
    def checkout_session_id(self) -> str | None:
        return self._props.checkout_session_id

    @property
    def wallet_request_id(self) -> str | None:
        return self._props.wallet_request_id

    @property
    def wallet_tx_id(self) -> str | None:
        return self._props.wallet_tx_id

    @property
    def payer_address(self) -> str | None:
        return self._props.payer_address

    @property
    def confirmations(self) -> int:
        return self._props.confirmations

    @property
    def required_confirmations(self) -> int:
        return self._props.required_confirmations

    @property
    def amount_received(self) -> int:
        return self._props.amount_received

    @property
    def amount_refunded(self) -> int:
        return self._props.amount_refunded

    @property
    def last_payment_error(self) -> PaymentError | None:
        return self._props.last_payment_error

    @property
    def description(self) -> str | None:
        return self._props.description

    @property
    def created_at(self) -> datetime:
        return self._props.created_at

    @property
    def version(self) -> int:
        return self._props.version

    @property
    def refundable_amount(self) -> int:
        return self._props.amount_received - self._props.amount_refunded

    def is_waiting_for_wallet(self) -> bool:
        """Есть незавершённая попытка оплаты, результат которой надо узнать у кошелька."""
        return self._props.status in ("requires_action", "processing")

    # ── бизнес-правила ──
    def start_wallet_payment(self, *, wallet_request_id: str, customer_email: str | None, now: datetime) -> None:
        """Покупатель выбрал оплату криптокошельком: запрос на подтверждение отправлен в кошелёк."""
        self._transition("requires_action")
        self._props.wallet_request_id = wallet_request_id
        self._props.customer_email = customer_email
        self._props.last_payment_error = None
        self._record("payment_intent.requires_action", now)

    def mark_processing(self, *, tx_id: str, payer_address: str, now: datetime) -> None:
        """Покупатель подтвердил платёж, кошелёк отправил транзакцию в сеть."""
        self._transition("processing")
        self._props.wallet_tx_id = tx_id
        self._props.payer_address = payer_address
        self._props.confirmations = 0
        self._record("payment_intent.processing", now)

    def update_confirmations(self, confirmations: int) -> bool:
        """Новые блоки поверх транзакции. Возвращает True, если число подтверждений изменилось."""
        if self._props.status != "processing":
            raise InvalidStateError("payment_intent", self._props.status, "processing")
        changed = confirmations != self._props.confirmations
        self._props.confirmations = max(0, confirmations)
        return changed

    def has_enough_confirmations(self) -> bool:
        return self._props.confirmations >= self._props.required_confirmations

    def mark_succeeded(self, now: datetime) -> None:
        self._require_transition("succeeded")
        if not self.has_enough_confirmations():
            raise BusinessRuleError("not_enough_confirmations", "Транзакция ещё не набрала нужное число подтверждений")
        self._transition("succeeded")
        self._props.amount_received = self._props.amount
        self._props.succeeded_at = now
        self._record("payment_intent.succeeded", now)

    def fail_payment(self, *, code: str, message: str, now: datetime) -> None:
        """Попытка не удалась: покупатель отклонил запрос, не хватило денег, транзакция упала в сети.

        Намерение возвращается в requires_payment_method — можно попробовать ещё раз.
        """
        self._transition("requires_payment_method")
        self._props.last_payment_error = PaymentError(code=code, message=message)
        self._props.wallet_request_id = None
        self._props.wallet_tx_id = None
        self._props.payer_address = None
        self._props.confirmations = 0
        self._record("payment_intent.payment_failed", now)

    def cancel(self, reason: CancellationReason | None, now: datetime) -> None:
        self._transition("canceled")
        self._props.cancellation_reason = reason
        self._props.canceled_at = now
        self._record("payment_intent.canceled", now)

    def reserve_refund(self, amount: int) -> None:
        """Возврат части или всей суммы. Вернуть больше, чем получено, нельзя."""
        if self._props.status != "succeeded":
            raise BusinessRuleError(
                "charge_not_refundable", "Вернуть можно только успешный платёж", param="payment_intent"
            )
        if amount <= 0:
            raise ValidationError("Сумма возврата должна быть больше нуля", param="amount")
        if amount > self.refundable_amount:
            raise BusinessRuleError(
                "amount_too_large",
                f"Сумма возврата больше доступной: можно вернуть не больше {self.refundable_amount}",
                param="amount",
            )
        self._props.amount_refunded += amount

    def release_refund(self, amount: int) -> None:
        """Возврат не удался — деньги снова считаются полученными."""
        self._props.amount_refunded = max(0, self._props.amount_refunded - amount)

    def pull_events(self) -> list[DomainEvent]:
        """Забрать накопленные события (вызывает прикладной слой после сохранения)."""
        events, self._pending_events = self._pending_events, []
        return events

    def bump_version(self) -> None:
        """Вызывается репозиторием после успешного UPDATE."""
        self._props.version += 1

    def snapshot(self) -> PaymentIntentProps:
        """Копия состояния: изменить объект «в обход» его методов через неё нельзя."""
        return replace(self._props, metadata=dict(self._props.metadata))

    # ── внутреннее ──
    def _require_transition(self, to: PaymentIntentStatus) -> None:
        current = self._props.status
        if to not in _TRANSITIONS[current]:
            raise InvalidStateError("payment_intent", current, to)

    def _transition(self, to: PaymentIntentStatus) -> None:
        self._require_transition(to)
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
