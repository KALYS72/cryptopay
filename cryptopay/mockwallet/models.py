from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Profile = Literal["normal", "insufficient_funds", "declines", "slow", "fails_onchain", "treasury"]
RequestStatus = Literal["pending", "approved", "rejected", "failed", "expired", "canceled"]
TxKind = Literal["payment", "refund"]
TxStatus = Literal["pending", "confirmed", "failed"]

DECIMALS: dict[str, int] = {"usdt": 6, "usdc": 6, "btc": 8, "eth": 18}
"""Валюты, которые «знает» кошелёк, и число знаков после запятой."""


@dataclass(slots=True)
class WalletAccount:
    id: str
    name: str
    address: str
    profile: Profile
    outcome: str
    """Что произойдёт при оплате с этого кошелька — подсказка, как у тестовых карт Stripe."""
    balances: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class PaymentRequest:
    id: str
    reference: str
    """Идентификатор во внешней системе: для CryptoPay это pi_…."""
    merchant_name: str
    description: str | None
    amount: int
    currency: str
    to_address: str
    return_url: str
    status: RequestStatus
    created_at: datetime
    expires_at: datetime
    tx_id: str | None = None
    payer_address: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None


@dataclass(slots=True)
class Transaction:
    id: str
    """Хэш транзакции: 0x + 64 шестнадцатеричных символа, как в Ethereum."""
    kind: TxKind
    from_address: str
    to_address: str
    amount: int
    currency: str
    reference: str | None
    created_at: datetime
    included_height: int
    """Блок, в который транзакция попадёт (пока его нет — она «в мемпуле»)."""
    will_fail: bool
    failure_reason: str | None
    settled: bool = False
    """Учтена ли транзакция в балансах получателя (или возвращены ли деньги отправителю при ошибке)."""


@dataclass(frozen=True, slots=True)
class TransactionView:
    id: str
    kind: TxKind
    status: TxStatus
    confirmations: int
    from_address: str
    to_address: str
    amount: int
    currency: str
    reference: str | None
    created_at: datetime
    included_height: int
    failure_reason: str | None
