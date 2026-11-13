"""Адаптер порта CryptoWallet для имитатора кошелька MockWallet (паттерн Adapter).

Переводит «язык» кошелька (его модели, ошибки, адреса страниц) в «язык» платёжной системы.
Только этот файл знает, что за портом стоит именно MockWallet; поменять кошелёк на
настоящий — значит написать рядом другой адаптер и выбрать его в container.py.
"""

from __future__ import annotations

from ...application.ports.wallet import (
    CryptoWallet,
    NewPaymentRequest,
    WalletOperationError,
    WalletPaymentRequest,
    WalletTransaction,
)
from ...domain.errors import WalletUnavailableError
from ...mockwallet import MockWallet, MockWalletError
from ...mockwallet.models import PaymentRequest, TransactionView


class MockWalletAdapter(CryptoWallet):
    def __init__(self, wallet: MockWallet, *, public_url: str) -> None:
        self._wallet = wallet
        self._public_url = public_url.rstrip("/")

    async def create_payment_request(self, request: NewPaymentRequest) -> WalletPaymentRequest:
        try:
            created = self._wallet.create_payment_request(
                reference=request.reference,
                merchant_name=request.merchant_name,
                description=request.description,
                amount=request.amount,
                currency=request.currency,
                to_address=self._wallet.treasury.address,  # деньги приходят на кошелёк платформы, как в Stripe
                return_url=request.return_url,
            )
        except MockWalletError as error:
            raise WalletUnavailableError(f"Кошелёк отклонил запрос: {error.message}") from error
        return self._request(created)

    async def get_payment_request(self, request_id: str) -> WalletPaymentRequest | None:
        found = self._wallet.get_payment_request(request_id)
        return self._request(found) if found else None

    async def cancel_payment_request(self, request_id: str) -> WalletPaymentRequest | None:
        found = self._wallet.cancel_payment_request(request_id)
        return self._request(found) if found else None

    async def get_transaction(self, tx_id: str) -> WalletTransaction | None:
        tx = self._wallet.get_transaction(tx_id)
        return self._transaction(tx) if tx else None

    async def send_refund(self, *, reference: str, to_address: str, amount: int, currency: str) -> WalletTransaction:
        try:
            tx = self._wallet.send_from_treasury(
                to_address=to_address, amount=amount, currency=currency, reference=reference
            )
        except MockWalletError as error:
            raise WalletOperationError(error.code, error.message) from error
        return self._transaction(tx)

    def _request(self, request: PaymentRequest) -> WalletPaymentRequest:
        return WalletPaymentRequest(
            id=request.id,
            status=request.status,
            approve_url=f"{self._public_url}/wallet/approve/{request.id}",
            tx_id=request.tx_id,
            payer_address=request.payer_address,
            failure_code=request.failure_code,
            failure_message=request.failure_message,
        )

    @staticmethod
    def _transaction(tx: TransactionView) -> WalletTransaction:
        return WalletTransaction(
            id=tx.id,
            status=tx.status,
            confirmations=tx.confirmations,
            amount=tx.amount,
            currency=tx.currency,
            from_address=tx.from_address,
            to_address=tx.to_address,
            failure_reason=tx.failure_reason,
        )
