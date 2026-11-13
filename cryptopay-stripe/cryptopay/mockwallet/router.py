"""HTTP-интерфейс имитатора кошелька: страница подтверждения платежа, обозреватель и JSON API для них.

В настоящей жизни это было бы окно расширения MetaMask.
Эндпоинты не входят в документацию CryptoPay API: это другая система.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .models import DECIMALS, PaymentRequest, TransactionView, WalletAccount
from .service import MockWallet, MockWalletError


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(min_length=1, max_length=40)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def account_json(account: WalletAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "name": account.name,
        "address": account.address,
        "profile": account.profile,
        "outcome": account.outcome,
        "balances": {currency: str(amount) for currency, amount in account.balances.items()},
    }


def request_json(request: PaymentRequest) -> dict[str, Any]:
    return {
        "id": request.id,
        "status": request.status,
        "merchant_name": request.merchant_name,
        "description": request.description,
        "amount": str(request.amount),
        "currency": request.currency,
        "to_address": request.to_address,
        "expires_at": _iso(request.expires_at),
        "return_url": request.return_url,
        "tx_id": request.tx_id,
        "payer_address": request.payer_address,
        "failure_code": request.failure_code,
        "failure_message": request.failure_message,
    }


def tx_json(tx: TransactionView) -> dict[str, Any]:
    return {
        "id": tx.id,
        "kind": tx.kind,
        "status": tx.status,
        "confirmations": tx.confirmations,
        "from_address": tx.from_address,
        "to_address": tx.to_address,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "reference": tx.reference,
        "included_height": tx.included_height,
        "created_at": _iso(tx.created_at),
        "failure_reason": tx.failure_reason,
    }


def _error(error: MockWalletError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={"error": {"type": "wallet_error", "code": error.code, "message": error.message}},
    )


def create_wallet_router(
    get_wallet: Callable[[Request], MockWallet],
    *,
    pages_dir: Path,
    write_dependencies: Sequence[Any] = (),
) -> APIRouter:
    router = APIRouter(prefix="/wallet", include_in_schema=False)

    @router.get("")
    async def explorer_page() -> FileResponse:
        return FileResponse(pages_dir / "wallet-explorer.html", headers={"cache-control": "no-cache"})

    @router.get("/approve/{request_id}")
    async def approve_page(request_id: str) -> FileResponse:
        return FileResponse(pages_dir / "wallet-approve.html", headers={"cache-control": "no-cache"})

    @router.get("/api/requests/{request_id}")
    async def get_request(request_id: str, request: Request) -> JSONResponse:
        wallet = get_wallet(request)
        found = wallet.get_payment_request(request_id)
        if found is None:
            return _error(MockWalletError("request_not_found", "Запрос на оплату не найден", status=404))
        return JSONResponse(
            {
                "request": request_json(found),
                "accounts": [account_json(a) for a in wallet.customer_accounts()],
                "decimals": DECIMALS,
                "height": wallet.height(),
            }
        )

    @router.post("/api/requests/{request_id}/approve", dependencies=list(write_dependencies))
    async def approve(request_id: str, body: ApproveBody, request: Request) -> JSONResponse:
        try:
            result = get_wallet(request).approve(request_id, body.account_id)
        except MockWalletError as error:
            return _error(error)
        return JSONResponse({"request": request_json(result)})

    @router.post("/api/requests/{request_id}/reject", dependencies=list(write_dependencies))
    async def reject(request_id: str, request: Request) -> JSONResponse:
        try:
            result = get_wallet(request).reject(request_id)
        except MockWalletError as error:
            return _error(error)
        return JSONResponse({"request": request_json(result)})

    @router.get("/api/explorer")
    async def explorer(request: Request) -> JSONResponse:
        wallet = get_wallet(request)
        return JSONResponse(
            {
                "height": wallet.height(),
                "block_time_seconds": wallet.chain.block_time_seconds,
                "accounts": [account_json(a) for a in wallet.accounts()],
                "transactions": [tx_json(t) for t in wallet.transactions(50)],
                "decimals": DECIMALS,
            }
        )

    @router.post("/api/reset", dependencies=list(write_dependencies))
    async def reset(request: Request) -> JSONResponse:
        get_wallet(request).reset()
        return JSONResponse({"ok": True})

    return router
