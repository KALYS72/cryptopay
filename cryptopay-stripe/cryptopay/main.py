"""Точка входа: конфигурация → приложение FastAPI → сервер uvicorn (+ фоновые задачи).

python -m cryptopay            # как в production
python -m cryptopay --reload   # для разработки: перезапуск при изменении кода
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .infrastructure.config import ConfigError, load_config
from .infrastructure.http.app import create_app
from .infrastructure.logger import configure_logging
from .paths import APP_ROOT


def load_env_file(path: Path) -> None:
    """Читает .env (KEY=VALUE). Уже заданные переменные окружения не перезаписывает."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def asgi_app() -> FastAPI:
    """Фабрика приложения для uvicorn (``uvicorn cryptopay.main:asgi_app --factory``)."""
    load_env_file(APP_ROOT / ".env")
    config = load_config()
    configure_logging(config.log_level, pretty=config.env == "development")
    return create_app(config=config)


def run() -> None:
    load_env_file(APP_ROOT / ".env")
    try:
        config = load_config()
    except ConfigError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
    uvicorn.run(
        "cryptopay.main:asgi_app",
        factory=True,
        host=config.host,
        port=config.port,
        reload="--reload" in sys.argv,
        reload_dirs=[str(APP_ROOT / "cryptopay")] if "--reload" in sys.argv else None,
        log_config=None,  # логирование настраивает configure_logging
        access_log=False,  # журнал запросов пишет AccessLogMiddleware
        server_header=False,
        proxy_headers=False,  # IP клиента разбираем сами с учётом TRUST_PROXY (middleware.client_ip)
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    run()
