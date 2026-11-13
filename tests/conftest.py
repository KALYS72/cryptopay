from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from .support.harness import Harness, create_harness


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    """Свежее приложение на каждый тест: хранилище в памяти, подменные часы, id и вебхуки, настоящий MockWallet."""
    harness = await create_harness()
    try:
        yield harness
    finally:
        await harness.client.aclose()
        await harness.container.close()
