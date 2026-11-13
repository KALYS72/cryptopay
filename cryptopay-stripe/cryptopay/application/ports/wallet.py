"""Порт «Криптокошелёк» — всё, что платёжной системе нужно от внешнего кошелька.

SOLID (D): сервисы зависят от этого абстрактного класса, а не от конкретного кошелька.
Сейчас за портом стоит имитатор (MockWalletAdapter → cryptopay/mockwallet). Чтобы
подключить настоящий кошелёк — MetaMask через WalletConnect, кастодиальный API
биржи, Bitcoin-ноду — пишется новый адаптер; PaymentService и остальные не меняются.

SOLID (I): в порте только то, что нужно платёжной системе: создать запрос на оплату,
узнать его судьбу, посмотреть транзакцию и отправить возврат. Ни балансов чужих
кошельков, ни управления ключами здесь нет.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

WalletRequestStatus = Literal["pending", "approved", "rejected", "failed", "expired", "canceled"]
WalletTxStatus = Literal["pending", "confirmed", "failed"]


@dataclass(frozen=True, slots=True)
class NewPaymentRequest:
    reference: str
    """Наш id (pi_…): по нему кошелёк и мы понимаем, о каком платеже речь."""
    merchant_name: str
    description: str | None
    amount: int
    currency: str
    return_url: str
    """Куда кошелёк вернёт покупателя после подтверждения или отказа."""


@dataclass(frozen=True, slots=True)
class WalletPaymentRequest:
    id: str
    status: WalletRequestStatus
    approve_url: str
    """Страница кошелька, где покупатель подтверждает платёж."""
    tx_id: str | None
    payer_address: str | None
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class WalletTransaction:
    id: str
    status: WalletTxStatus
    confirmations: int
    amount: int
    currency: str
    from_address: str
    to_address: str
    failure_reason: str | None


class WalletOperationError(Exception):
    """Кошелёк понял запрос, но отказался его выполнять (например, на кошельке платформы не хватает денег).

    Недоступность кошелька — другая ошибка: WalletUnavailableError, после неё операцию стоит повторить.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CryptoWallet(ABC):
    @abstractmethod
    async def create_payment_request(self, request: NewPaymentRequest) -> WalletPaymentRequest: ...

    @abstractmethod
    async def get_payment_request(self, request_id: str) -> WalletPaymentRequest | None: ...

    @abstractmethod
    async def cancel_payment_request(self, request_id: str) -> WalletPaymentRequest | None:
        """Отменить ожидающий запрос. Если покупатель уже успел подтвердить — вернуть его текущее состояние."""

    @abstractmethod
    async def get_transaction(self, tx_id: str) -> WalletTransaction | None: ...

    @abstractmethod
    async def send_refund(self, *, reference: str, to_address: str, amount: int, currency: str) -> WalletTransaction:
        """Перевести деньги покупателю с кошелька платформы. Идемпотентно по reference (re_…)."""
