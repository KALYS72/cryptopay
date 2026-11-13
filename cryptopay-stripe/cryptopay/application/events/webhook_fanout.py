from __future__ import annotations

from ..ports.repositories import EventRecord, Repositories, WebhookDelivery
from ..ports.system import IdGenerator
from .dispatcher import EventSubscriber


class WebhookFanout(EventSubscriber):
    """Подписчик: ставит событие в очередь доставки на каждый адрес продавца, который на него подписан.

    Паттерн Transactional Outbox: сами HTTP-запросы отправляет позже фоновая задача
    WebhookDeliveryJob. Медленный или упавший сервер магазина никак не задерживает
    и не ломает приём платежа.
    """

    name = "webhook-fanout"

    def __init__(self, ids: IdGenerator) -> None:
        self._ids = ids

    async def on_event(self, event: EventRecord, repos: Repositories) -> None:
        for endpoint in await repos.webhook_endpoints.list_by_merchant(event.merchant_id):
            if endpoint.accepts(event.type):
                await repos.outbox.enqueue(
                    WebhookDelivery(
                        id=self._ids.generate("whd"),
                        merchant_id=event.merchant_id,
                        event_id=event.id,
                        endpoint_id=endpoint.id,
                        event_type=event.type,
                        url=endpoint.url,
                        status="pending",
                        attempts=0,
                        next_attempt_at=event.created_at,
                        last_error=None,
                        last_status_code=None,
                        delivered_at=None,
                        created_at=event.created_at,
                    )
                )
