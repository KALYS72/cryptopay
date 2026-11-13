"""MockWallet: счета, запросы на оплату, транзакции и балансы — всё в памяти процесса.

Состояние в памяти — сознательное решение: это имитатор, а не часть платёжной системы.
После перезапуска тестовые балансы возвращаются к исходным, как песочница.
Все методы синхронные и не уступают управление циклу событий, поэтому каждый
выполняется атомарно, без гонок между запросами.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from .chain import MockChain
from .models import DECIMALS, PaymentRequest, Transaction, TransactionView, TxKind, TxStatus, WalletAccount
from .seed import TREASURY_ID, build_accounts

REQUEST_TTL_MINUTES = 15
SLOW_EXTRA_BLOCKS = 4
"""На столько блоков позже попадает в цепочку транзакция «медленного» кошелька."""
MAX_HISTORY = 500


class MockWalletError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class MockWallet:
    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        block_time_seconds: float = 3.0,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._now = now
        self.chain = MockChain(genesis=now(), block_time_seconds=block_time_seconds)
        self._new_id = id_factory or _random_id
        self._accounts = build_accounts()
        self._requests: dict[str, PaymentRequest] = {}
        self._transactions: dict[str, Transaction] = {}

    # ── счета ──
    @property
    def treasury(self) -> WalletAccount:
        return self._accounts[TREASURY_ID]

    def accounts(self) -> list[WalletAccount]:
        self._settle()
        return [replace(a, balances=dict(a.balances)) for a in self._accounts.values()]

    def customer_accounts(self) -> list[WalletAccount]:
        return [a for a in self.accounts() if a.profile != "treasury"]

    def height(self) -> int:
        return self.chain.height(self._now())

    # ── запросы на оплату (то, что в настоящем кошельке выглядит как всплывающее окно «Подтвердить») ──
    def create_payment_request(
        self,
        *,
        reference: str,
        merchant_name: str,
        description: str | None,
        amount: int,
        currency: str,
        to_address: str,
        return_url: str,
    ) -> PaymentRequest:
        if currency not in DECIMALS:
            raise MockWalletError("unsupported_currency", f"Кошелёк не поддерживает валюту {currency}")
        if amount <= 0:
            raise MockWalletError("invalid_amount", "Сумма должна быть больше нуля")
        now = self._now()
        request = PaymentRequest(
            id=self._new_id("wpr"),
            reference=reference,
            merchant_name=merchant_name,
            description=description,
            amount=amount,
            currency=currency,
            to_address=to_address,
            return_url=return_url,
            status="pending",
            created_at=now,
            expires_at=now + timedelta(minutes=REQUEST_TTL_MINUTES),
        )
        self._requests[request.id] = request
        self._trim()
        return replace(request)

    def get_payment_request(self, request_id: str) -> PaymentRequest | None:
        request = self._requests.get(request_id)
        if request is None:
            return None
        if request.status == "pending" and self._now() >= request.expires_at:
            request.status = "expired"
            request.failure_code = "wallet_request_expired"
            request.failure_message = "Время на подтверждение платежа в кошельке истекло"
        return replace(request)

    def cancel_payment_request(self, request_id: str) -> PaymentRequest | None:
        """Магазин больше не ждёт оплату (страница оплаты истекла). Подтвердить такой запрос уже нельзя."""
        request = self.get_payment_request(request_id)
        if request is None:
            return None
        if request.status == "pending":
            stored = self._requests[request_id]
            stored.status = "canceled"
            stored.failure_code = "wallet_request_canceled"
            stored.failure_message = "Запрос на оплату отменён магазином"
            return replace(stored)
        return request

    def approve(self, request_id: str, account_id: str) -> PaymentRequest:
        """Покупатель нажал «Подтвердить» в кошельке. Исход зависит от выбранного тестового кошелька."""
        request = self._pending_request(request_id)
        account = self._accounts.get(account_id)
        if account is None or account.profile == "treasury":
            raise MockWalletError("account_not_found", "Такого кошелька нет", status=404)
        self._settle()

        stored = self._requests[request_id]
        stored.payer_address = account.address
        if account.profile == "declines":
            stored.status = "failed"
            stored.failure_code = "wallet_declined"
            stored.failure_message = "Кошелёк отклонил платёж: аккаунт заморожен проверкой безопасности"
            return replace(stored)
        if account.balances.get(request.currency, 0) < request.amount:
            stored.status = "failed"
            stored.failure_code = "insufficient_funds"
            stored.failure_message = "Недостаточно средств в кошельке"
            return replace(stored)

        tx = self._broadcast(
            kind="payment",
            sender=account,
            to_address=request.to_address,
            amount=request.amount,
            currency=request.currency,
            reference=request.reference,
        )
        stored.status = "approved"
        stored.tx_id = tx.id
        return replace(stored)

    def reject(self, request_id: str) -> PaymentRequest:
        """Покупатель нажал «Отклонить» — как код 4001 «User rejected the request» в MetaMask."""
        self._pending_request(request_id)
        stored = self._requests[request_id]
        stored.status = "rejected"
        stored.failure_code = "payment_rejected"
        stored.failure_message = "Покупатель отклонил платёж в кошельке"
        return replace(stored)

    # ── транзакции ──
    def get_transaction(self, tx_id: str) -> TransactionView | None:
        self._settle()
        tx = self._transactions.get(tx_id)
        return self._view(tx) if tx else None

    def send_from_treasury(self, *, to_address: str, amount: int, currency: str, reference: str) -> TransactionView:
        """Перевод с кошелька платформы (для возвратов). Идемпотентен по reference:
        повторный вызов с тем же reference вернёт уже созданную транзакцию, а не отправит деньги дважды.
        """
        self._settle()
        existing = next((t for t in self._transactions.values() if t.reference == reference), None)
        if existing is not None:
            return self._view(existing)
        if currency not in DECIMALS:
            raise MockWalletError("unsupported_currency", f"Кошелёк не поддерживает валюту {currency}")
        treasury = self.treasury
        if treasury.balances.get(currency, 0) < amount:
            raise MockWalletError("insufficient_funds", "На кошельке платформы недостаточно средств")
        tx = self._broadcast(
            kind="refund", sender=treasury, to_address=to_address, amount=amount, currency=currency, reference=reference
        )
        return self._view(tx)

    def transactions(self, limit: int = 50) -> list[TransactionView]:
        self._settle()
        latest = sorted(self._transactions.values(), key=lambda t: t.created_at, reverse=True)
        return [self._view(t) for t in latest[:limit]]

    def reset(self) -> None:
        """Вернуть тестовые балансы к исходным (кнопка в обозревателе)."""
        self._accounts = build_accounts()
        self._requests.clear()
        self._transactions.clear()

    # ── внутреннее ──
    def _pending_request(self, request_id: str) -> PaymentRequest:
        request = self.get_payment_request(request_id)
        if request is None:
            raise MockWalletError("request_not_found", "Запрос на оплату не найден", status=404)
        if request.status != "pending":
            raise MockWalletError("request_not_pending", f"Запрос уже обработан (статус: {request.status})", status=409)
        return request

    def _broadcast(
        self, *, kind: TxKind, sender: WalletAccount, to_address: str, amount: int, currency: str, reference: str
    ) -> Transaction:
        """Отправить транзакцию: деньги списываются сразу (чтобы их нельзя было потратить дважды),
        а получателю зачисляются, когда транзакция попадёт в блок."""
        now = self._now()
        height = self.chain.height(now)
        extra = SLOW_EXTRA_BLOCKS if sender.profile == "slow" else 0
        will_fail = sender.profile == "fails_onchain"
        sender.balances[currency] = sender.balances.get(currency, 0) - amount
        tx = Transaction(
            id=self._new_id("tx"),
            kind=kind,
            from_address=sender.address,
            to_address=to_address,
            amount=amount,
            currency=currency,
            reference=reference,
            created_at=now,
            included_height=height + 1 + extra,
            will_fail=will_fail,
            failure_reason="Выполнение транзакции прервано: execution reverted" if will_fail else None,
        )
        self._transactions[tx.id] = tx
        self._trim()
        return tx

    def _settle(self) -> None:
        """Учесть в балансах транзакции, которые уже попали в блок."""
        height = self.height()
        by_address = {a.address: a for a in self._accounts.values()}
        for tx in self._transactions.values():
            if tx.settled or tx.included_height > height:
                continue
            tx.settled = True
            target = by_address.get(tx.from_address if tx.will_fail else tx.to_address)
            if target is not None:  # при ошибке деньги возвращаются отправителю
                target.balances[tx.currency] = target.balances.get(tx.currency, 0) + tx.amount

    def _view(self, tx: Transaction) -> TransactionView:
        confirmations = self.chain.confirmations(tx.included_height, self._now())
        status: TxStatus = "pending" if confirmations == 0 else "failed" if tx.will_fail else "confirmed"
        return TransactionView(
            id=tx.id,
            kind=tx.kind,
            status=status,
            confirmations=0 if tx.will_fail else confirmations,
            from_address=tx.from_address,
            to_address=tx.to_address,
            amount=tx.amount,
            currency=tx.currency,
            reference=tx.reference,
            created_at=tx.created_at,
            included_height=tx.included_height,
            failure_reason=tx.failure_reason if status == "failed" else None,
        )

    def _trim(self) -> None:
        """Имитатор живёт долго — не даём истории расти бесконечно (удаляем только завершённое)."""
        if len(self._requests) > MAX_HISTORY:
            finished = sorted((r for r in self._requests.values() if r.status != "pending"), key=lambda r: r.created_at)
            for request in finished[: len(self._requests) - MAX_HISTORY]:
                del self._requests[request.id]
        if len(self._transactions) > MAX_HISTORY:
            settled = sorted((t for t in self._transactions.values() if t.settled), key=lambda t: t.created_at)
            for tx in settled[: len(self._transactions) - MAX_HISTORY]:
                del self._transactions[tx.id]


def _random_id(kind: str) -> str:
    if kind == "tx":
        return "0x" + secrets.token_hex(32)
    return f"{kind}_{secrets.token_urlsafe(18).replace('-', 'x').replace('_', 'y')}"
