"""Публикация доменных событий (паттерн Observer).

1. Каждое доменное событие превращается в запись журнала событий — объект Event
   в формате Stripe (``evt_…``) со снимком объекта в ``data.object``.
2. Запись получают все подписчики. Сейчас подписчик один — WebhookFanout, который
   ставит событие в очередь доставки на адреса продавца. Новая реакция на события
   (письмо покупателю, уведомление в Telegram) — новый подписчик; ни сущности,
   ни сервисы для этого менять не нужно. SOLID (O).

Всё происходит с репозиториями ТЕКУЩЕЙ транзакции: событие, его доставки и само
изменение объекта сохраняются атомарно.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence

from ...domain.events import DomainEvent
from ..ports.repositories import EventRecord, Repositories
from ..ports.system import IdGenerator
from ..serializers import ObjectSerializer


class EventSubscriber(ABC):
    name: str

    @abstractmethod
    async def on_event(self, event: EventRecord, repos: Repositories) -> None: ...


class EventPublisher(ABC):
    """SOLID (I): сервисам нужен только метод publish — они не знают, кто подписан на события."""

    @abstractmethod
    async def publish(self, events: Sequence[DomainEvent], repos: Repositories) -> list[EventRecord]: ...


class EventDispatcher(EventPublisher):
    def __init__(
        self, *, ids: IdGenerator, serializer: ObjectSerializer, subscribers: Iterable[EventSubscriber]
    ) -> None:
        self._ids = ids
        self._serializer = serializer
        self._subscribers = tuple(subscribers)

    async def publish(self, events: Sequence[DomainEvent], repos: Repositories) -> list[EventRecord]:
        records: list[EventRecord] = []
        for event in events:
            record = EventRecord(
                id=self._ids.generate("evt"),
                merchant_id=event.merchant_id,
                type=event.type,
                subject_id=event.subject_id,
                data=self._serializer.subject(event.subject),
                created_at=event.occurred_at,
            )
            await repos.events.append(record)
            for subscriber in self._subscribers:
                await subscriber.on_event(record, repos)
            records.append(record)
        return records
