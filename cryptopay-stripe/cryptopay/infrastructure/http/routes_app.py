"""Эндпоинты кабинета продавца, страницы оплаты для покупателя и служебные."""

# Без `from __future__ import annotations`: FastAPI читает аннотации параметров во время работы.

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from ... import __version__
from ...application.serializers import unix
from ...application.services.account_service import SignInResult
from ...application.services.checkout_service import CreateCheckoutSession, PublicCheckout
from ...domain.errors import NotFoundError
from ...domain.money import format_units
from .dependencies import ContainerDep, SessionOnly
from .helpers import json_response
from .middleware import RateLimiter
from .schemas import ErrorResponse, LoginRequest, RegisterRequest, StartPaymentRequest, UpdateAccountRequest

AUTH_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


def _signed_in(container: Any, result: SignInResult) -> dict[str, Any]:
    return {"token": result.token, "account": container.serializer.account(result.merchant)}


def dashboard_router(auth_limiter: RateLimiter) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["Кабинет продавца"], responses=AUTH_ERRORS)

    @router.post(
        "/auth/register", summary="Регистрация продавца", status_code=201, dependencies=[Depends(auth_limiter)]
    )
    async def register(body: RegisterRequest, container: ContainerDep) -> JSONResponse:
        result = await container.services.accounts.register(
            email=body.email, password=body.password, business_name=body.business_name
        )
        return JSONResponse(_signed_in(container, result), status_code=201)

    @router.post("/auth/login", summary="Вход в кабинет по e-mail и паролю", dependencies=[Depends(auth_limiter)])
    async def login(body: LoginRequest, container: ContainerDep) -> JSONResponse:
        result = await container.services.accounts.sign_in(email=body.email, password=body.password)
        return JSONResponse(_signed_in(container, result))

    @router.get("/account", summary="Мой аккаунт")
    async def get_account(principal: SessionOnly, container: ContainerDep) -> JSONResponse:
        merchant = await container.services.accounts.get(principal.merchant_id)
        return JSONResponse(container.serializer.account(merchant))

    @router.patch("/account", summary="Изменить название магазина")
    async def update_account(
        body: UpdateAccountRequest, principal: SessionOnly, container: ContainerDep
    ) -> JSONResponse:
        merchant = await container.services.accounts.rename(principal.merchant_id, body.business_name)
        return JSONResponse(container.serializer.account(merchant))

    @router.post(
        "/account/api_key",
        summary="Выпустить новый секретный ключ",
        description="Старый ключ сразу перестаёт работать. Новый показывается один раз.",
    )
    async def roll_api_key(principal: SessionOnly, container: ContainerDep) -> JSONResponse:
        issued = await container.services.accounts.roll_api_key(principal.merchant_id)
        return JSONResponse({"secret": issued.secret, "account": container.serializer.account(issued.merchant)})

    @router.get("/dashboard/overview", summary="Главная кабинета: баланс, оборот за неделю, платежи по статусам")
    async def overview(principal: SessionOnly, container: ContainerDep) -> JSONResponse:
        data = await container.services.balance.overview(principal.merchant_id)
        volume: dict[str, dict[str, str]] = {}
        for item in data.volume:
            volume.setdefault(item.currency, {})[item.day.isoformat()] = str(item.amount)
        return JSONResponse(
            {
                "balances": {code: str(amount) for code, amount in data.balances.items()},
                "days": [d.isoformat() for d in data.days],
                "volume": volume,
                "payments_by_status": data.payments_by_status,
            }
        )

    @router.get("/config", summary="Настройки, нужные интерфейсу", tags=["Служебное"])
    async def config(container: ContainerDep) -> JSONResponse:
        cfg = container.config
        demo = {"email": cfg.demo.email, "password": cfg.demo.password} if container.demo_merchant_id else None
        return JSONResponse(
            {
                "currencies": [
                    {
                        "code": c.code,
                        "symbol": c.symbol,
                        "name": c.name,
                        "decimals": c.decimals,
                        "min_amount": str(c.min_amount),
                    }
                    for c in container.currencies.all()
                ],
                "required_confirmations": cfg.payments.required_confirmations,
                "fee_bps": cfg.payments.fee_bps,
                "block_time_seconds": cfg.payments.block_time_seconds,
                "storage": container.storage,
                "demo": demo,
                "version": __version__,
            }
        )

    return router


def public_checkout_json(container: Any, view: PublicCheckout) -> dict[str, Any]:
    """То, что видит покупатель. Внутренние данные продавца (metadata, комиссия) сюда не попадают."""
    session, intent, currency = view.session, view.intent, view.currency
    error = intent.last_payment_error
    return {
        "id": session.id,
        "status": session.status,
        "business_name": view.business_name,
        "description": intent.description,
        "amount": str(intent.amount),
        "amount_display": format_units(intent.amount, currency.decimals),
        "currency": currency.code,
        "currency_symbol": currency.symbol,
        "currency_name": currency.name,
        "decimals": currency.decimals,
        "expires_at": unix(session.expires_at),
        "success_url": session.success_redirect_url(),
        "cancel_url": session.cancel_url,
        "payment": {
            "status": intent.status,
            "confirmations": intent.confirmations,
            "required_confirmations": intent.required_confirmations,
            "transaction": intent.wallet_tx_id,
            "payer_address": intent.payer_address,
            "approve_url": view.approve_url,
            "last_payment_error": {"code": error.code, "message": error.message} if error else None,
        },
    }


def checkout_router(page_limiter: RateLimiter, pay_limiter: RateLimiter) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["Страница оплаты"])

    @router.get(
        "/checkout/{session_id}",
        summary="Данные страницы оплаты для покупателя",
        description="Пока покупатель смотрит на страницу, сервер сразу сверяет платёж с кошельком.",
        dependencies=[Depends(page_limiter)],
        responses={404: {"model": ErrorResponse}},
    )
    async def get_checkout(session_id: str, container: ContainerDep) -> Response:
        view = await container.services.checkout.public_view(session_id)
        return json_response(public_checkout_json(container, view))

    @router.post(
        "/checkout/{session_id}/pay",
        summary="Оплатить криптокошельком",
        description="Создаёт запрос на оплату в кошельке и возвращает адрес страницы кошелька для перехода.",
        dependencies=[Depends(pay_limiter)],
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def pay(session_id: str, body: StartPaymentRequest, container: ContainerDep) -> JSONResponse:
        url = await container.services.checkout.start_payment(session_id, body.customer_email)
        return JSONResponse({"redirect_url": url})

    @router.post(
        "/demo/checkout",
        summary="Демо: страница оплаты от демо-магазина",
        description="Для кнопки «Попробовать оплату» на главной: создаёт сессию на 25 USDT от демо-аккаунта.",
        dependencies=[Depends(pay_limiter)],
        tags=["Служебное"],
    )
    async def demo_checkout(container: ContainerDep) -> JSONResponse:
        if container.demo_merchant_id is None:
            raise NotFoundError("Демо-аккаунт")
        session = await container.services.checkout.create(
            container.demo_merchant_id,
            CreateCheckoutSession(
                amount=25_000_000,
                currency="usdt",
                description="Худи «Пиксель», размер M",
                success_url=None,
                cancel_url=f"{container.config.public_url}/",
                metadata={"source": "demo"},
                expires_in_minutes=30,
            ),
        )
        return JSONResponse({"redirect_url": container.serializer.checkout_url(session.id)})

    return router


def health_router() -> APIRouter:
    router = APIRouter(tags=["Служебное"])

    @router.get("/healthz", summary="Проверка работоспособности (для Render и Docker)")
    async def healthz(container: ContainerDep) -> JSONResponse:
        database = "ok"
        try:
            await container.check_database()
        except Exception:
            database = "unavailable"
        status = 200 if database == "ok" else 503
        return JSONResponse(
            {
                "status": "ok" if status == 200 else "degraded",
                "version": __version__,
                "storage": container.storage,
                "database": database,
                "wallet": {"type": "mock", "height": container.mock_wallet.height()},
            },
            status_code=status,
        )

    return router
