"""Контракт репозиториев на настоящем PostgreSQL.

Запуск: ``TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/cryptopay_test pytest tests/contract``.
Без переменной тесты пропускаются (в CI база поднимается как service-контейнер).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from cryptopay.application.ports.repositories import UnitOfWork
from cryptopay.infrastructure.postgres.database import build_conninfo, create_pool, migrate
from cryptopay.infrastructure.postgres.repositories import PostgresUnitOfWork
from cryptopay.paths import MIGRATIONS_DIR

from .repository_contract import RepositoryContract

URL = os.environ.get("TEST_DATABASE_URL")
_schema_ready = False


@pytest.mark.skipif(not URL, reason="нет TEST_DATABASE_URL")
class TestPostgresRepositories(RepositoryContract):
    @pytest.fixture
    async def uow(self) -> AsyncIterator[UnitOfWork]:
        global _schema_ready
        assert URL is not None
        pool = await create_pool(URL, "auto")
        try:
            if not _schema_ready:  # схема с нуля — один раз за прогон
                async with pool.connection() as conn:
                    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
                await migrate(build_conninfo(URL, "auto"), MIGRATIONS_DIR)
                _schema_ready = True
            async with pool.connection() as conn:
                await conn.execute(
                    "TRUNCATE merchants, payment_intents, checkout_sessions, refunds, balance_transactions, events, "
                    "webhook_endpoints, webhook_deliveries, idempotency_keys CASCADE"
                )
            yield PostgresUnitOfWork(pool)
        finally:
            await pool.close()
