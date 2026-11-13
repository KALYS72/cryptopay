"""Мелкие системные порты. Каждый — один-два метода: SOLID (I).

Зачем абстрагировать даже часы? Чтобы тест мог «перемотать время» на час
и проверить, что страница оплаты истекла, не дожидаясь этого в реальности.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime: ...


IdPrefix = Literal["acct", "pi", "cs", "re", "txn", "evt", "we", "whd"]
"""Префиксы идентификаторов, как у Stripe: по id сразу видно, что это за объект."""


class IdGenerator(ABC):
    @abstractmethod
    def generate(self, prefix: IdPrefix) -> str: ...


class SecretGenerator(ABC):
    """Криптографически стойкие случайные строки: API-ключи, секреты вебхуков."""

    @abstractmethod
    def token(self, n_bytes: int = 24) -> str: ...


class Hasher(ABC):
    @abstractmethod
    def sha256(self, value: str) -> str: ...
