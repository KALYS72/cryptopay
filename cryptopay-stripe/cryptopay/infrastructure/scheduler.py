from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from ..application.jobs.base import Job

logger = logging.getLogger("cryptopay.scheduler")


class Scheduler:
    """Простой планировщик фоновых задач внутри процесса (asyncio).

    Блокировка не даёт запускам наслаиваться, если RPC-узел отвечает медленно.
    Ограничение: рассчитан на один экземпляр приложения (см. docs/architecture.md).
    """

    def __init__(self, jobs: Sequence[Job], interval_seconds: float) -> None:
        self._jobs = tuple(jobs)
        self._interval = interval_seconds
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="cryptopay-scheduler")
        logger.info(
            "scheduler started",
            extra={"interval_seconds": self._interval, "jobs": [job.name for job in self._jobs]},
        )

    async def tick(self) -> None:
        """Один проход по всем задачам. Если предыдущий ещё идёт — ждём его, а не запускаем второй."""
        if self._lock.locked():
            async with self._lock:
                return
        async with self._lock:
            for job in self._jobs:
                try:
                    report = await job.run()
                except Exception:
                    logger.exception("job crashed", extra={"job": job.name})
                    continue
                if report.changed or report.errors:
                    logger.info(
                        "job finished",
                        extra={
                            "job": job.name,
                            "processed": report.processed,
                            "changed": report.changed,
                            "errors": [{"id": e.id, "message": e.message} for e in report.errors],
                        },
                    )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        async with self._lock:  # дождаться задачи, которая выполняется прямо сейчас
            pass

    async def _loop(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self._interval)
