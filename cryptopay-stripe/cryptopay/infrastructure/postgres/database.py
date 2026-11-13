from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, LiteralString, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("cryptopay.postgres")

SslMode = Literal["auto", "true", "false", "no-verify"]
Row = dict[str, Any]


class Queryable(Protocol):
    """Минимальный общий интерфейс пула и одного соединения: репозиториям всё равно, в транзакции они или нет."""

    async def fetch_all(self, sql: LiteralString, params: Sequence[Any] = ()) -> list[Row]: ...

    async def fetch_one(self, sql: LiteralString, params: Sequence[Any] = ()) -> Row | None: ...

    async def execute(self, sql: LiteralString, params: Sequence[Any] = ()) -> int:
        """Выполнить запрос и вернуть число затронутых строк."""
        ...


class ConnectionQueryable:
    """Все запросы — через одно соединение (внутри транзакции)."""

    def __init__(self, conn: psycopg.AsyncConnection[Row]) -> None:
        self._conn = conn

    async def fetch_all(self, sql: LiteralString, params: Sequence[Any] = ()) -> list[Row]:
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()

    async def fetch_one(self, sql: LiteralString, params: Sequence[Any] = ()) -> Row | None:
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchone()

    async def execute(self, sql: LiteralString, params: Sequence[Any] = ()) -> int:
        cursor = await self._conn.execute(sql, params)
        return cursor.rowcount


class PoolQueryable:
    """Каждый запрос берёт свободное соединение из пула (вне транзакции)."""

    def __init__(self, pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]]) -> None:
        self._pool = pool

    async def fetch_all(self, sql: LiteralString, params: Sequence[Any] = ()) -> list[Row]:
        async with self._pool.connection() as conn:
            return await ConnectionQueryable(conn).fetch_all(sql, params)

    async def fetch_one(self, sql: LiteralString, params: Sequence[Any] = ()) -> Row | None:
        async with self._pool.connection() as conn:
            return await ConnectionQueryable(conn).fetch_one(sql, params)

    async def execute(self, sql: LiteralString, params: Sequence[Any] = ()) -> int:
        async with self._pool.connection() as conn:
            return await ConnectionQueryable(conn).execute(sql, params)


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1") or "." not in host


def build_conninfo(database_url: str, ssl_mode: SslMode) -> str:
    """Строка подключения для libpq с нужным sslmode.

    * ``auto``: sslmode из строки подключения (его добавляют Neon и Render), а если его нет —
      без SSL для локальной базы и с обязательным SSL для удалённой;
    * ``true``: SSL с проверкой сертификата по системным корневым сертификатам;
    * ``no-verify``: SSL без проверки сертификата; ``false``: без SSL.
    """
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    query.pop("uselibpqcompat", None)  # параметр драйвера node-postgres, libpq его не знает
    if ssl_mode == "false":
        query["sslmode"] = "disable"
    elif ssl_mode == "no-verify":
        query["sslmode"] = "require"
    elif ssl_mode == "true":
        query.update(sslmode="verify-full", sslrootcert="system")
    elif "sslmode" not in query:
        query["sslmode"] = "disable" if _is_local(parts.hostname or "") else "require"
    return urlunsplit(parts._replace(query=urlencode(query)))


async def create_pool(database_url: str, ssl_mode: SslMode) -> AsyncConnectionPool[psycopg.AsyncConnection[Row]]:
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]] = AsyncConnectionPool(
        build_conninfo(database_url, ssl_mode),
        min_size=1,
        max_size=10,
        max_idle=30,
        open=False,
        kwargs={
            "autocommit": True,  # вне явных транзакций каждый запрос фиксируется сразу
            "row_factory": dict_row,
            # PgBouncer в пулере Neon работает в режиме транзакций: серверные prepared
            # statements там ненадёжны, поэтому psycopg не должен их создавать.
            "prepare_threshold": None,
            "connect_timeout": 10,
        },
    )
    await pool.open(wait=True, timeout=30)
    return pool


MIGRATION_LOCK_ID = 727274


def _read_migrations(migrations_dir: Path) -> list[tuple[str, bytes]]:
    """SQL-файлы миграций по порядку имён: 001_…, 002_… (читаются один раз при старте)."""
    return [(file.name, file.read_bytes()) for file in sorted(migrations_dir.glob("*.sql"))]


async def migrate(conninfo: str, migrations_dir: Path) -> list[str]:
    """Простейший мигратор.

    Применяет по порядку SQL-файлы из папки migrations, которых ещё нет в таблице
    schema_migrations. Advisory lock не даёт двум экземплярам приложения мигрировать одновременно.
    """
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True, prepare_threshold=None) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(id TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in await (await conn.execute("SELECT id FROM schema_migrations")).fetchall()}
            for name, sql in _read_migrations(migrations_dir):
                if name in done:
                    continue
                try:
                    async with conn.transaction():
                        await conn.execute(sql)
                        await conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (name,))
                except psycopg.Error as error:
                    raise RuntimeError(f"Миграция {name} не применилась: {error}") from error
                applied.append(name)
                logger.info("migration applied", extra={"migration": name})
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
    return applied


def is_unique_violation(error: BaseException) -> bool:
    """Код ошибки PostgreSQL 23505 — «нарушение уникальности»."""
    return isinstance(error, psycopg.errors.UniqueViolation)
