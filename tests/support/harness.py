from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI

from cryptopay.application.ports.wallet import CryptoWallet
from cryptopay.container import Container, ContainerOverrides, create_container
from cryptopay.infrastructure.config import AppConfig, load_config
from cryptopay.infrastructure.http.app import create_app
from cryptopay.infrastructure.memory.repositories import InMemoryUnitOfWork
from cryptopay.mockwallet import MockWallet

from .fakes import FakeClock, PlainPasswordHasher, RecordingWebhookSender, SequentialIds

BLOCK_TIME = 3.0

_TEST_ENV = {
    "APP_ENV": "test",
    "PORT": "3000",
    "PUBLIC_URL": "http://localhost:3000",
    "JWT_SECRET": "test-secret-test-secret-test-secret-123",
    "RATE_LIMIT_ENABLED": "false",
    "LOG_LEVEL": "silent",
    "REQUIRED_CONFIRMATIONS": "3",
    "PLATFORM_FEE_BPS": "100",
    "MOCK_BLOCK_TIME_SECONDS": str(BLOCK_TIME),
    "DEMO_ACCOUNT": "false",
}


def test_config(env: Mapping[str, str] | None = None) -> AppConfig:
    return load_config({**_TEST_ENV, **(env or {})})


test_config.__test__ = False  # type: ignore[attr-defined]  # pytest: это не тест


@dataclass
class Harness:
    container: Container
    app: FastAPI
    client: httpx.AsyncClient
    clock: FakeClock
    webhooks: RecordingWebhookSender

    @property
    def wallet(self) -> MockWallet:
        return self.container.mock_wallet

    async def run_jobs(self) -> None:
        for job in self.container.jobs:
            report = await job.run()
            if report.errors:
                raise AssertionError(f"{job.name}: {report.errors}")

    async def mine(self, blocks: int = 1) -> None:
        """Перемотать время на N блоков имитируемой сети и прогнать фоновые задачи."""
        self.clock.advance_seconds(BLOCK_TIME * blocks)
        await self.run_jobs()

    # ── сценарии, которые нужны многим тестам ──
    async def register(self, email: str = "shop@example.com", name: str = "Магазин «Пиксель»") -> str:
        response = await self.client.post(
            "/api/auth/register", json={"email": email, "password": "correct-horse", "business_name": name}
        )
        assert response.status_code == 201, response.text
        token: str = response.json()["token"]
        return token

    async def api_key(self, token: str) -> str:
        response = await self.client.post("/api/account/api_key", headers=auth(token))
        assert response.status_code == 200, response.text
        secret: str = response.json()["secret"]
        return secret

    async def create_session(self, key: str, **overrides: Any) -> dict[str, Any]:
        body = {"amount": "25000000", "currency": "usdt", "description": "Худи", **overrides}
        response = await self.client.post("/v1/checkout/sessions", json=body, headers=auth(key))
        assert response.status_code == 200, response.text
        session: dict[str, Any] = response.json()
        return session

    async def pay(self, session_id: str, account: str = "alice") -> dict[str, Any]:
        """Покупатель: «Оплатить» на странице → «Подтвердить» в кошельке → вернулся на страницу."""
        start = await self.client.post(f"/api/checkout/{session_id}/pay", json={"customer_email": "buyer@example.com"})
        assert start.status_code == 200, start.text
        request_id = start.json()["redirect_url"].rsplit("/", 1)[-1]
        approve = await self.client.post(f"/wallet/api/requests/{request_id}/approve", json={"account_id": account})
        assert approve.status_code == 200, approve.text
        page = await self.client.get(f"/api/checkout/{session_id}")
        assert page.status_code == 200, page.text
        result: dict[str, Any] = page.json()
        return result


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def create_harness(env: Mapping[str, str] | None = None, wallet: CryptoWallet | None = None) -> Harness:
    """Полностью собранное приложение, но с подменными часами, id, паролями и вебхуками."""
    clock = FakeClock()
    webhooks = RecordingWebhookSender()
    container = await create_container(
        test_config(env),
        ContainerOverrides(
            clock=clock,
            ids=SequentialIds(),
            uow=InMemoryUnitOfWork(),
            webhook_sender=webhooks,
            passwords=PlainPasswordHasher(),
            wallet=wallet,
        ),
    )
    app = create_app(container)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000")
    return Harness(container=container, app=app, client=client, clock=clock, webhooks=webhooks)
