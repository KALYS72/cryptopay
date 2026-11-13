"""Идемпотентные запросы (заголовок Idempotency-Key), как в Stripe API.

Сеть ненадёжна: сервер магазина отправил «создай платёж», а ответ потерялся.
Повторить запрос страшно — вдруг создастся второй платёж. С ключом идемпотентности
повтор безопасен: второй запрос с тем же ключом не выполняется заново, а получает
сохранённый ответ первого.

* тот же ключ и те же параметры → сохранённый ответ (заголовок ``Idempotent-Replayed: true``);
* тот же ключ, но другие параметры → ошибка ``idempotency_key_reused``;
* тот же ключ, пока первый запрос ещё выполняется → ``idempotency_key_in_use`` (409).

Ответы с ошибкой не сохраняются: исправив запрос, его можно повторить с тем же ключом.
Ключи хранятся 24 часа.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from ...domain.errors import IdempotencyError, ValidationError
from ..ports.repositories import IdempotencyRecord, UnitOfWork
from ..ports.system import Clock

MAX_KEY_LENGTH = 255
KEY_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: str
    replayed: bool


class IdempotencyService:
    def __init__(self, *, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(
        self,
        *,
        merchant_id: str,
        key: str | None,
        request_hash: str,
        operation: Callable[[], Awaitable[tuple[int, str]]],
    ) -> StoredResponse:
        if key is None:
            status, body = await operation()
            return StoredResponse(status=status, body=body, replayed=False)
        if not key.strip() or len(key) > MAX_KEY_LENGTH:
            raise ValidationError(f"Idempotency-Key — от 1 до {MAX_KEY_LENGTH} символов", param="Idempotency-Key")

        store = self._uow.repos.idempotency
        existing = await store.try_begin(
            IdempotencyRecord(
                merchant_id=merchant_id,
                key=key,
                request_hash=request_hash,
                status="processing",
                response_status=None,
                response_body=None,
                created_at=self._clock.now(),
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyError(
                    "idempotency_key_reused",
                    "Этот Idempotency-Key уже использован для запроса с другими параметрами",
                )
            if existing.status == "processing" or existing.response_status is None:
                raise IdempotencyError(
                    "idempotency_key_in_use", "Запрос с этим Idempotency-Key ещё выполняется, повторите позже"
                )
            return StoredResponse(status=existing.response_status, body=existing.response_body or "", replayed=True)

        try:
            status, body = await operation()
        except BaseException:
            await store.release(merchant_id, key)
            raise
        await store.complete(merchant_id, key, status, body)
        return StoredResponse(status=status, body=body, replayed=False)

    async def cleanup(self) -> int:
        return await self._uow.repos.idempotency.delete_older_than(self._clock.now() - KEY_TTL)
