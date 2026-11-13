"""Окружение сквозных тестов: настоящий сервер CryptoPay, пример магазина и браузер Chromium.

Запуск: ``RUN_E2E=1 pytest tests/e2e`` (один раз нужен браузер: ``playwright install chromium``).
С ``E2E_DATABASE_URL=postgresql://…`` сервер работает поверх PostgreSQL, как в production.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("E2E_PORT", "3100"))
SHOP_PORT = PORT + 1
BASE_URL = f"http://127.0.0.1:{PORT}"
SHOP_URL = f"http://127.0.0.1:{SHOP_PORT}"
SCREENSHOTS = ROOT / "docs" / "screenshots"
ENABLED = os.environ.get("RUN_E2E") == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e" in str(item.path).replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)
            if not ENABLED:
                item.add_marker(pytest.mark.skip(reason="сквозные тесты: RUN_E2E=1 pytest tests/e2e"))


def wait_for(url: str, process: subprocess.Popen[bytes], what: str) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status < 500:
                    return
        except OSError:
            pass
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"{what} не запустился")
        time.sleep(0.25)


def call(method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    """Запрос к API CryptoPay «как сервер магазина»."""
    request = urllib.request.Request(  # noqa: S310
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        result: dict[str, Any] = json.loads(response.read())
    return result


@pytest.fixture(scope="session")
def server() -> Iterator[str]:
    """Настоящий процесс `python -m cryptopay`, как в production; блоки идут раз в секунду."""
    env = {
        **os.environ,
        "APP_ENV": "test",
        "HOST": "127.0.0.1",
        "PORT": str(PORT),
        "PUBLIC_URL": BASE_URL,
        "LOG_LEVEL": "warning",
        "JWT_SECRET": "e2e-secret-e2e-secret-e2e-secret-e2e",
        "MOCK_BLOCK_TIME_SECONDS": "1",
        "REQUIRED_CONFIRMATIONS": "3",
        "WORKER_INTERVAL_MS": "500",
        "RATE_LIMIT_ENABLED": "false",
        "DEMO_ACCOUNT": "true",
        "DATABASE_URL": os.environ.get("E2E_DATABASE_URL", ""),
    }
    process = subprocess.Popen([sys.executable, "-m", "cryptopay"], cwd=ROOT, env=env)
    try:
        wait_for(f"{BASE_URL}/healthz", process, "сервер CryptoPay")
        yield BASE_URL
    finally:
        process.terminate()
        process.wait(timeout=15)


@dataclass(frozen=True)
class Shop:
    url: str
    token: str
    """Сессия кабинета продавца, которому принадлежит магазин."""


@pytest.fixture(scope="session")
def shop(server: str) -> Iterator[Shop]:
    """Пример магазина examples/shop.py: свой аккаунт, секретный ключ и адрес вебхука."""
    email = f"shop{int(time.time() * 1000)}@example.com"
    signed = call(
        "POST",
        "/api/auth/register",
        {"email": email, "password": "correct-horse", "business_name": "Магазин «Пиксель»"},
    )
    token = signed["token"]
    key = call("POST", "/api/account/api_key", token=token)["secret"]
    endpoint = call(
        "POST",
        "/v1/webhook_endpoints",
        {"url": f"{SHOP_URL}/webhook", "enabled_events": ["*"], "description": "examples/shop.py"},
        token=token,
    )
    env = {
        **os.environ,
        "CRYPTOPAY_URL": server,
        "CRYPTOPAY_API_KEY": key,
        "WEBHOOK_SECRET": endpoint["secret"],
        "PORT": str(SHOP_PORT),
        "SHOP_URL": SHOP_URL,
    }
    process = subprocess.Popen([sys.executable, "examples/shop.py"], cwd=ROOT, env=env)
    try:
        wait_for(SHOP_URL, process, "пример магазина")
        yield Shop(url=SHOP_URL, token=token)
    finally:
        process.terminate()
        process.wait(timeout=15)


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Iterator[Browser]:
    instance = playwright_instance.chromium.launch(channel=os.environ.get("E2E_BROWSER_CHANNEL") or None)
    yield instance
    instance.close()


class ConsoleWatch:
    """Собирает ошибки консоли страницы: нарушение CSP или исключение в JS — это провал теста."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def attach(self, page: Page) -> Page:
        page.on("pageerror", lambda error: self.errors.append(f"pageerror: {error}"))
        page.on(
            "console",
            lambda message: (
                self.errors.append(message.text)
                if message.type == "error" and "Failed to load resource" not in message.text
                else None
            ),
        )
        return page


@pytest.fixture
def console() -> Iterator[ConsoleWatch]:
    watch = ConsoleWatch()
    yield watch
    assert watch.errors == [], watch.errors


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    ctx = browser.new_context(viewport={"width": 1280, "height": 860}, device_scale_factor=2, locale="ru-RU")
    yield ctx
    ctx.close()


@pytest.fixture
def page(context: BrowserContext, console: ConsoleWatch, server: str) -> Page:
    return console.attach(context.new_page())


def shot(page: Page, name: str, *, full_page: bool = True) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / f"{name}.png"), full_page=full_page)
