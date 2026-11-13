from __future__ import annotations

import pytest

from cryptopay.application.ports.repositories import UnitOfWork
from cryptopay.infrastructure.memory.repositories import InMemoryUnitOfWork

from .repository_contract import RepositoryContract


class TestInMemoryRepositories(RepositoryContract):
    @pytest.fixture
    def uow(self) -> UnitOfWork:
        return InMemoryUnitOfWork()
