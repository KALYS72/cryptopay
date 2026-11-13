"""Имитация блокчейна: высота цепочки вычисляется из времени.

Блок «появляется» каждые ``block_time_seconds`` секунд, начиная с момента запуска.
Никакого фонового «майнера» не нужно: высота — чистая функция от текущего времени,
поэтому в тестах её можно двигать подменными часами.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MockChain:
    genesis: datetime
    block_time_seconds: float

    def height(self, now: datetime) -> int:
        """Номер последнего блока. Блок 0 — момент запуска."""
        elapsed = (now - self.genesis).total_seconds()
        return max(0, int(elapsed // self.block_time_seconds))

    def confirmations(self, included_height: int, now: datetime) -> int:
        """Сколько блоков подтверждают транзакцию: блок с ней самой + все следующие."""
        current = self.height(now)
        return 0 if current < included_height else current - included_height + 1
