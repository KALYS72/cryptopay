"""Баланс продавца — журнал проводок (BalanceTransaction, как в Stripe).

Баланс нигде не хранится одним числом: он всегда равен сумме ``net`` всех проводок.
Так невозможно «потерять» деньги при сбое посередине операции — проводка либо
записана вместе с платежом в одной транзакции БД, либо нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .errors import ValidationError

BalanceTransactionType = Literal["payment", "refund", "refund_failure"]

MAX_FEE_BPS = 10_000


def calculate_fee(amount: int, fee_bps: int) -> int:
    """Комиссия платформы в базисных пунктах (100 bps = 1 %), округление вверх до минимальной единицы."""
    if not 0 <= fee_bps <= MAX_FEE_BPS:
        raise ValidationError("Комиссия — от 0 до 10000 базисных пунктов", param="fee_bps")
    return -(-amount * fee_bps // 10_000)


@dataclass(frozen=True, slots=True)
class BalanceTransaction:
    id: str
    merchant_id: str
    type: BalanceTransactionType
    source_id: str
    """Что породило проводку: pi_… для платежа, re_… для возврата."""
    amount: int
    """Со знаком: + деньги пришли, − ушли."""
    fee: int
    net: int
    """amount − fee: на столько изменился баланс."""
    currency: str
    description: str
    created_at: datetime

    @classmethod
    def for_payment(
        cls,
        *,
        id: str,
        merchant_id: str,
        payment_intent_id: str,
        amount: int,
        currency: str,
        fee_bps: int,
        now: datetime,
    ) -> BalanceTransaction:
        fee = calculate_fee(amount, fee_bps)
        return cls(
            id=id,
            merchant_id=merchant_id,
            type="payment",
            source_id=payment_intent_id,
            amount=amount,
            fee=fee,
            net=amount - fee,
            currency=currency,
            description="Оплата",
            created_at=now,
        )

    @classmethod
    def for_refund(
        cls, *, id: str, merchant_id: str, refund_id: str, amount: int, currency: str, now: datetime
    ) -> BalanceTransaction:
        """Комиссия за исходный платёж при возврате не возвращается — так же, как у Stripe."""
        return cls(
            id=id,
            merchant_id=merchant_id,
            type="refund",
            source_id=refund_id,
            amount=-amount,
            fee=0,
            net=-amount,
            currency=currency,
            description="Возврат",
            created_at=now,
        )

    @classmethod
    def for_refund_failure(
        cls, *, id: str, merchant_id: str, refund_id: str, amount: int, currency: str, now: datetime
    ) -> BalanceTransaction:
        """Возврат не дошёл до покупателя — списанные деньги возвращаются на баланс."""
        return cls(
            id=id,
            merchant_id=merchant_id,
            type="refund_failure",
            source_id=refund_id,
            amount=amount,
            fee=0,
            net=amount,
            currency=currency,
            description="Возврат не прошёл",
            created_at=now,
        )
