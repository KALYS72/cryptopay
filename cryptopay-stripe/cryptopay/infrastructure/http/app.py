"""Сборка HTTP-приложения (FastAPI).

Функция получает готовый контейнер зависимостей — поэтому в тестах её можно запустить
с фейковыми часами и хранилищем в памяти, не поднимая ни сеть, ни базу.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from ... import __version__
from ...container import Container, create_container
from ...mockwallet import MockWallet
from ...mockwallet.router import create_wallet_router
from ...paths import PUBLIC_DIR
from ..config import AppConfig
from ..scheduler import Scheduler
from .errors import install_error_handlers
from .middleware import AccessLogMiddleware, BodySizeLimitMiddleware, RateLimiter, SecurityHeadersMiddleware
from .routes_app import checkout_router, dashboard_router, health_router
from .routes_v1 import v1_router

logger = logging.getLogger("cryptopay")

API_DESCRIPTION = """
REST API платёжной системы **CryptoPay** — устроен по образцу Stripe API.

Покупатель платит из криптокошелька; в этой версии кошелёк и сеть — **имитация (MockWallet)**,
поэтому всё работает в тестовом режиме (`livemode: false`) без настоящих денег.

**Как принять оплату**
1. `POST /v1/checkout/sessions` — создать страницу оплаты, получить `url`.
2. Перенаправить покупателя на `url`. Он выберет тестовый кошелёк и подтвердит платёж.
3. Дождаться вебхука `checkout.session.completed` (или `payment_intent.succeeded`).

**Авторизация.** Секретный ключ `sk_test_…` из кабинета: `Authorization: Bearer sk_test_…`
(или `curl -u sk_test_…:`). Кабинет использует токен сессии из `POST /api/auth/login`.

**Суммы** — целые числа в минимальных единицах валюты, строкой: 25 USDT = `"25000000"`.

**Ошибки** — `{"error": {"type", "code", "message", "param"}}`, как у Stripe.

**Вебхуки.** Заголовок `CryptoPay-Signature: t=<unix>,v1=<hex>`, где
`v1 = HMAC_SHA256(whsec_…, "<t>.<тело запроса>")`. Повторы при ошибке: 1 мин, 5 мин, 15 мин, 1 ч, 6 ч.
"""

TAGS = [
    {"name": "Checkout Sessions", "description": "Страница оплаты, на которую магазин отправляет покупателя"},
    {"name": "Payment Intents", "description": "Платежи и их статусы"},
    {"name": "Refunds", "description": "Возвраты покупателю"},
    {"name": "Balance", "description": "Баланс и журнал проводок"},
    {"name": "Events", "description": "Журнал событий — то же, что приходит в вебхуках"},
    {"name": "Webhook Endpoints", "description": "Адреса на сервере магазина для вебхуков"},
    {"name": "Кабинет продавца", "description": "Вход, аккаунт, ключи (только сессия кабинета)"},
    {"name": "Страница оплаты", "description": "Для покупателя, без авторизации"},
    {"name": "Служебное"},
]

SWAGGER_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CryptoPay API</title>
  <link rel="icon" href="/assets/img/logo.svg">
  <link rel="stylesheet" href="/assets/vendor/swagger-ui/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="/assets/vendor/swagger-ui/swagger-ui-bundle.js"></script>
  <script src="/assets/js/swagger-init.js"></script>
</body>
</html>
"""


class CachedStaticFiles(StaticFiles):
    """Статика фронтенда; в production браузер кэширует её на час."""

    def __init__(self, *, directory: os.PathLike[str] | str, max_age: int) -> None:
        super().__init__(directory=directory)
        self.max_age = max_age

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = f"public, max-age={self.max_age}" if self.max_age else "no-cache"
        return response


def _wallet_of(request: Request) -> MockWallet:
    container: Container = request.app.state.container
    return container.mock_wallet


def create_app(container: Container | None = None, *, config: AppConfig | None = None) -> FastAPI:
    """Готовый контейнер → приложение для тестов; только config → контейнер создаётся при старте (production)."""
    if container is None and config is None:
        raise ValueError("create_app() needs a container or a config")
    cfg = container.config if container is not None else config
    assert cfg is not None
    production = cfg.env == "production"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container is not None:  # тесты: контейнером и фоновыми задачами управляет тест
            yield
            return
        owned = await create_container(cfg)
        app.state.container = owned
        scheduler = Scheduler(owned.jobs, cfg.worker.interval_seconds)
        if cfg.worker.enabled:
            scheduler.start()
        logger.info(
            f"CryptoPay запущен: {cfg.public_url}",
            extra={"url": cfg.public_url, "port": cfg.port, "storage": owned.storage, "version": __version__},
        )
        try:
            yield
        finally:
            logger.info("shutting down")
            await scheduler.stop()
            await owned.close()

    app = FastAPI(
        title="CryptoPay API",
        version=__version__,
        description=API_DESCRIPTION,
        license_info={"name": "MIT"},
        openapi_tags=TAGS,
        openapi_url="/api/openapi.json",
        docs_url=None,
        redoc_url=None,
        servers=[{"url": cfg.public_url, "description": "Этот сервер"}],
        lifespan=lifespan,
    )
    if container is not None:
        app.state.container = container

    # Порядок важен: добавленный последним middleware оборачивает все остальные.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=32 * 1024)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, production=production)
    install_error_handlers(app)

    def limiter(per_minute: int) -> RateLimiter:
        return RateLimiter(per_minute, enabled=cfg.rate_limit_enabled, trust_proxy=cfg.trust_proxy)

    app.include_router(health_router())
    app.include_router(v1_router())
    app.include_router(dashboard_router(limiter(20)))
    app.include_router(checkout_router(limiter(120), limiter(30)))
    # Имитатор кошелька — «чужая» система; подключаем его роутер, передав способ найти экземпляр.
    app.include_router(
        create_wallet_router(_wallet_of, pages_dir=PUBLIC_DIR, write_dependencies=[Depends(limiter(60))])
    )

    # ── Фронтенд: статические страницы без сборки (HTML + ES-модули) ──
    app.mount(
        "/assets", CachedStaticFiles(directory=PUBLIC_DIR / "assets", max_age=3600 if production else 0), name="assets"
    )

    def page(file: str) -> FileResponse:
        return FileResponse(PUBLIC_DIR / file, headers={"cache-control": "no-cache"})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return page("index.html")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return page("dashboard.html")

    @app.get("/checkout/{session_id}", include_in_schema=False)
    async def checkout(session_id: str) -> FileResponse:
        return page("checkout.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        return RedirectResponse("/assets/img/logo.svg", status_code=301)

    @app.get("/docs", include_in_schema=False)
    async def docs() -> HTMLResponse:
        """Swagger UI: файлы лежат у нас (public/assets/vendor), а не на CDN, — работает и без интернета."""
        return HTMLResponse(SWAGGER_PAGE)

    return app
