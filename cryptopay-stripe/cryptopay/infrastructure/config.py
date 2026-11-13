"""Вся конфигурация — из переменных окружения (принцип 12-factor app).

Один и тот же Docker-образ работает и локально, и на Render, и на любом VPS;
отличаются только переменные. Список и описание — в .env.example.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

AppEnv = Literal["development", "production", "test"]
SslMode = Literal["auto", "true", "false", "no-verify"]


class ConfigError(Exception):
    pass


class _Env(BaseModel):
    """Сырые переменные окружения с проверкой типов и диапазонов (Pydantic)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    APP_ENV: AppEnv = "development"
    HOST: str = "0.0.0.0"
    PORT: int = Field(3000, ge=1, le=65535)
    PUBLIC_URL: str | None = None
    RENDER_EXTERNAL_URL: str | None = None
    TRUST_PROXY: int = Field(1, ge=0)
    LOG_LEVEL: Literal["critical", "error", "warning", "warn", "info", "debug", "silent"] = "info"

    DATABASE_URL: str | None = None
    DATABASE_SSL: SslMode = "auto"

    JWT_SECRET: str | None = Field(None, min_length=32)
    SESSION_TTL: str = "12h"

    REQUIRED_CONFIRMATIONS: int = Field(3, ge=1, le=100)
    PLATFORM_FEE_BPS: int = Field(100, ge=0, le=10000)
    MOCK_BLOCK_TIME_SECONDS: float = Field(3.0, ge=0.5, le=600)

    DEMO_ACCOUNT: bool = True
    DEMO_EMAIL: str = "demo@cryptopay.dev"
    DEMO_PASSWORD: str = Field("demo-password", min_length=8)

    WORKER_ENABLED: bool = True
    WORKER_INTERVAL_MS: int = Field(3000, ge=500)
    ALLOW_HTTP_WEBHOOKS: bool | None = None
    RATE_LIMIT_ENABLED: bool = True


@dataclass(frozen=True, slots=True)
class PaymentsConfig:
    required_confirmations: int
    fee_bps: int
    block_time_seconds: float


@dataclass(frozen=True, slots=True)
class DemoConfig:
    enabled: bool
    email: str
    password: str


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    enabled: bool
    interval_seconds: float


@dataclass(frozen=True, slots=True)
class AppConfig:
    env: AppEnv
    host: str
    port: int
    public_url: str
    trust_proxy: int
    log_level: str
    database_url: str | None
    database_ssl: SslMode
    jwt_secret: str | None
    session_ttl_seconds: int
    payments: PaymentsConfig
    demo: DemoConfig
    worker: WorkerConfig
    allow_http_webhooks: bool
    rate_limit_enabled: bool


_DURATION_RE = re.compile(r"([0-9]+)\s*([smhd]?)")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """'12h' → 43200, '30m' → 1800, '7d' → 604800, '90' → 90 (секунды)."""
    match = _DURATION_RE.fullmatch(text.strip().lower())
    if match is None or int(match.group(1)) <= 0:
        raise ConfigError(f"Ошибка конфигурации: SESSION_TTL «{text}» — укажите, например, 12h, 30m или 7d")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    source = os.environ if env is None else env
    raw = {key: value for key, value in source.items() if value.strip() != ""}  # пустая переменная = не задана
    try:
        e = _Env.model_validate(raw)
    except PydanticValidationError as error:
        problems = "\n".join(
            f"  • {'.'.join(str(p) for p in issue['loc'])}: {issue['msg']}" for issue in error.errors()
        )
        raise ConfigError(f"Ошибка конфигурации:\n{problems}") from None

    public_url = (e.PUBLIC_URL or e.RENDER_EXTERNAL_URL or f"http://localhost:{e.PORT}").rstrip("/")
    if not _is_http_url(public_url):
        raise ConfigError("Ошибка конфигурации: PUBLIC_URL должен быть адресом вида https://example.com")
    if e.APP_ENV == "production" and not e.JWT_SECRET:
        raise ConfigError("Ошибка конфигурации: в production обязательно задайте JWT_SECRET (не короче 32 символов)")

    level = {"warn": "warning"}.get(e.LOG_LEVEL, e.LOG_LEVEL)
    return AppConfig(
        env=e.APP_ENV,
        host=e.HOST,
        port=e.PORT,
        public_url=public_url,
        trust_proxy=e.TRUST_PROXY,
        log_level=level,
        database_url=(e.DATABASE_URL or "").strip() or None,
        database_ssl=e.DATABASE_SSL,
        jwt_secret=e.JWT_SECRET,
        session_ttl_seconds=parse_duration(e.SESSION_TTL),
        payments=PaymentsConfig(
            required_confirmations=e.REQUIRED_CONFIRMATIONS,
            fee_bps=e.PLATFORM_FEE_BPS,
            block_time_seconds=e.MOCK_BLOCK_TIME_SECONDS,
        ),
        demo=DemoConfig(enabled=e.DEMO_ACCOUNT, email=e.DEMO_EMAIL, password=e.DEMO_PASSWORD),
        worker=WorkerConfig(enabled=e.WORKER_ENABLED, interval_seconds=e.WORKER_INTERVAL_MS / 1000),
        allow_http_webhooks=e.ALLOW_HTTP_WEBHOOKS if e.ALLOW_HTTP_WEBHOOKS is not None else e.APP_ENV != "production",
        rate_limit_enabled=e.RATE_LIMIT_ENABLED,
    )
