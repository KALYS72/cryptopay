from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class JobError:
    id: str
    message: str


@dataclass(slots=True)
class JobReport:
    processed: int = 0
    changed: int = 0
    errors: list[JobError] = field(default_factory=list)


class Job(ABC):
    """Фоновая задача. Планировщик (infrastructure/scheduler.py) просто вызывает run() по таймеру."""

    name: str

    @abstractmethod
    async def run(self) -> JobReport: ...
