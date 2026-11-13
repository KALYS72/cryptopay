"""КОРЕНЬ КОМПОЗИЦИИ (Composition Root) — единственное место, где конкретные классы знакомятся друг с другом.

Сервисы получают зависимости через конструктор (Dependency Injection) и не создают их сами.

SOLID (D) целиком виден здесь: слева абстракции, справа выбранные реализации.
Хотите настоящий кошелёк вместо имитатора — пишется новый адаптер порта CryptoWallet
и меняется одна строка ниже; бизнес-логика остаётся нетронутой. Так же с базой данных.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .application.currencies import DEFAULT_CURRENCIES, CurrencyRegistry
from .application.events.dispatcher import EventDispatcher
from .application.events.webhook_fanout import WebhookFanout
from .application.jobs.base import Job
from .application.jobs.sync_jobs import IdempotencyCleanupJob, PaymentSyncJob, RefundSyncJob, SessionExpiryJob
from .application.jobs.webhook_delivery import WebhookDeliveryJob
from .application.ports.repositories import UnitOfWork
from .application.ports.security import PasswordHasher
from .application.ports.system import Clock, IdGenerator
from .application.ports.wallet import CryptoWallet
from .application.ports.webhooks import WebhookSender
from .application.serializers import ObjectSerializer
from .application.services.account_service import AccountService, DemoAccount
from .application.services.balance_service import BalanceService
from .application.services.checkout_service import CheckoutConfig, CheckoutService
from .application.services.idempotency_service import IdempotencyService
from .application.services.payment_service import PaymentConfig, PaymentService
from .application.services.refund_service import RefundService
from .application.services.webhook_service import WebhookService
from .infrastructure.auth import JwtTokenService
from .infrastructure.config import AppConfig
from .infrastructure.memory.repositories import InMemoryUnitOfWork
from .infrastructure.postgres.database import build_conninfo, create_pool, migrate
from .infrastructure.postgres.repositories import PostgresUnitOfWork
from .infrastructure.system import (
    CryptoSecretGenerator,
    RandomIdGenerator,
    ScryptPasswordHasher,
    Sha256Hasher,
    SystemClock,
)
from .infrastructure.wallet.mock_wallet_adapter import MockWalletAdapter
from .infrastructure.webhooks import DefaultWebhookUrlPolicy, HmacSha256WebhookSigner, HttpxWebhookSender
from .mockwallet import MockWallet
from .paths import MIGRATIONS_DIR

logger = logging.getLogger("cryptopay")

Storage = Literal["postgres", "memory"]

DEMO_BUSINESS_NAME = "Демо-магазин «Пиксель»"


@dataclass(frozen=True, slots=True)
class Services:
    accounts: AccountService
    checkout: CheckoutService
    payments: PaymentService
    refunds: RefundService
    balance: BalanceService
    webhooks: WebhookService
    idempotency: IdempotencyService


@dataclass(frozen=True, slots=True)
class Container:
    config: AppConfig
    clock: Clock
    currencies: CurrencyRegistry
    serializer: ObjectSerializer
    mock_wallet: MockWallet
    uow: UnitOfWork
    storage: Storage
    services: Services
    jobs: list[Job]
    demo_merchant_id: str | None
    check_database: Callable[[], Awaitable[None]]
    close: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ContainerOverrides:
    """Подмены для тестов: фейковые часы, предсказуемые id, записывающий отправитель вебхуков…"""

    clock: Clock | None = None
    ids: IdGenerator | None = None
    uow: UnitOfWork | None = None
    webhook_sender: WebhookSender | None = None
    passwords: PasswordHasher | None = None
    wallet: CryptoWallet | None = None


async def _noop() -> None:
    return None


async def create_container(config: AppConfig, overrides: ContainerOverrides | None = None) -> Container:
    o = overrides or ContainerOverrides()
    clock = o.clock or SystemClock()
    ids = o.ids or RandomIdGenerator()
    secrets = CryptoSecretGenerator()
    closers: list[Callable[[], Awaitable[None]]] = []

    # ── Хранилище: PostgreSQL, если задан DATABASE_URL, иначе память процесса ──
    uow: UnitOfWork
    storage: Storage
    check_database = _noop
    if o.uow is not None:
        uow, storage = o.uow, "memory"
    elif config.database_url:
        await migrate(build_conninfo(config.database_url, config.database_ssl), MIGRATIONS_DIR)
        pool = await create_pool(config.database_url, config.database_ssl)
        uow, storage = PostgresUnitOfWork(pool), "postgres"

        async def check_database() -> None:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")

        closers.append(pool.close)
    else:
        uow, storage = InMemoryUnitOfWork(), "memory"
        logger.warning("DATABASE_URL не задан: данные хранятся в памяти и исчезнут при перезапуске (режим демо)")

    # ── Криптокошелёк: имитатор за портом CryptoWallet (паттерн Adapter) ──
    mock_wallet = MockWallet(now=clock.now, block_time_seconds=config.payments.block_time_seconds)
    wallet: CryptoWallet = o.wallet or MockWalletAdapter(mock_wallet, public_url=config.public_url)

    currencies = CurrencyRegistry(DEFAULT_CURRENCIES)
    serializer = ObjectSerializer(config.public_url)

    # ── Подписчики доменных событий (Observer) ──
    publisher = EventDispatcher(ids=ids, serializer=serializer, subscribers=[WebhookFanout(ids)])

    jwt_secret = config.jwt_secret
    if not jwt_secret:
        jwt_secret = secrets.token(32)
        logger.warning("JWT_SECRET не задан: сгенерирован временный, сессии сбросятся при перезапуске")

    webhook_sender = o.webhook_sender or HttpxWebhookSender()
    webhook_signer = HmacSha256WebhookSigner()

    payments = PaymentService(
        uow=uow,
        clock=clock,
        ids=ids,
        wallet=wallet,
        publisher=publisher,
        config=PaymentConfig(fee_bps=config.payments.fee_bps),
    )
    checkout = CheckoutService(
        uow=uow,
        clock=clock,
        ids=ids,
        currencies=currencies,
        wallet=wallet,
        payments=payments,
        publisher=publisher,
        serializer=serializer,
        config=CheckoutConfig(required_confirmations=config.payments.required_confirmations),
    )
    refunds = RefundService(
        uow=uow,
        clock=clock,
        ids=ids,
        wallet=wallet,
        publisher=publisher,
        required_confirmations=config.payments.required_confirmations,
    )
    idempotency = IdempotencyService(uow=uow, clock=clock)
    services = Services(
        accounts=AccountService(
            uow=uow,
            clock=clock,
            ids=ids,
            secrets=secrets,
            hasher=Sha256Hasher(),
            passwords=o.passwords or ScryptPasswordHasher(),
            tokens=JwtTokenService(jwt_secret, config.session_ttl_seconds),
        ),
        checkout=checkout,
        payments=payments,
        refunds=refunds,
        balance=BalanceService(uow=uow, clock=clock, currencies=currencies),
        webhooks=WebhookService(
            uow=uow,
            clock=clock,
            ids=ids,
            secrets=secrets,
            url_policy=DefaultWebhookUrlPolicy(config.allow_http_webhooks),
            serializer=serializer,
        ),
        idempotency=idempotency,
    )

    jobs: list[Job] = [
        PaymentSyncJob(uow=uow, payments=payments),
        RefundSyncJob(uow=uow, refunds=refunds),
        SessionExpiryJob(uow=uow, clock=clock, checkout=checkout),
        WebhookDeliveryJob(uow=uow, clock=clock, sender=webhook_sender, signer=webhook_signer, serializer=serializer),
        IdempotencyCleanupJob(idempotency),
    ]

    demo_merchant_id: str | None = None
    if config.demo.enabled:
        demo = await services.accounts.ensure_demo_account(
            DemoAccount(email=config.demo.email, password=config.demo.password, business_name=DEMO_BUSINESS_NAME)
        )
        demo_merchant_id = demo.id

    async def close() -> None:
        for closer in reversed(closers):
            try:
                await closer()
            except Exception:  # закрываем всё, что можем, даже если что-то одно упало
                logger.exception("close failed")

    return Container(
        config=config,
        clock=clock,
        currencies=currencies,
        serializer=serializer,
        mock_wallet=mock_wallet,
        uow=uow,
        storage=storage,
        services=services,
        jobs=jobs,
        demo_merchant_id=demo_merchant_id,
        check_database=check_database,
        close=close,
    )
