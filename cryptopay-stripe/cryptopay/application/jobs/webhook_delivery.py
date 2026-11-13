from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from ..ports.repositories import UnitOfWork, WebhookDelivery
from ..ports.system import Clock
from ..ports.webhooks import WebhookRequest, WebhookSender, WebhookSigner
from ..serializers import ObjectSerializer
from ..webhooks import build_webhook_headers, to_json
from .base import Job, JobError, JobReport

WEBHOOK_RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 300, 900, 3600, 21600)
"""Паузы между повторными попытками: 1 мин, 5 мин, 15 мин, 1 ч, 6 ч."""
WEBHOOK_MAX_ATTEMPTS = len(WEBHOOK_RETRY_DELAYS_SECONDS) + 1


class WebhookDeliveryJob(Job):
    """Отправляет события из outbox на серверы продавцов.

    Ответ 2xx — доставлено. Ошибка или таймаут — повтор с растущей паузой
    (exponential backoff), после 6 неудачных попыток — failed.
    """

    name = "webhook-delivery"

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        sender: WebhookSender,
        signer: WebhookSigner,
        serializer: ObjectSerializer,
        batch_size: int = 25,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._sender = sender
        self._signer = signer
        self._serializer = serializer
        self._batch_size = batch_size

    async def run(self) -> JobReport:
        report = JobReport()
        for delivery in await self._uow.repos.outbox.find_due(self._clock.now(), self._batch_size):
            report.processed += 1
            try:
                await self._deliver(delivery)
                report.changed += 1
            except Exception as error:
                report.errors.append(JobError(id=delivery.id, message=str(error)))
        return report

    async def _deliver(self, delivery: WebhookDelivery) -> None:
        repos = self._uow.repos
        endpoint = await repos.webhook_endpoints.find_by_id(delivery.endpoint_id)
        event = await repos.events.find_by_id(delivery.event_id)
        if endpoint is None or event is None or endpoint.status != "enabled":
            await repos.outbox.save(replace(delivery, status="failed", last_error="Адрес вебхука удалён или выключен"))
            return

        now = self._clock.now()
        body = to_json(self._serializer.event(event))
        response = await self._sender.send(
            WebhookRequest(
                url=endpoint.url,
                body=body,
                headers=build_webhook_headers(
                    signer=self._signer,
                    secret=endpoint.secret,
                    body=body,
                    event_type=event.type,
                    delivery_id=delivery.id,
                    now=now,
                ),
            )
        )

        attempts = delivery.attempts + 1
        if response.ok:
            await repos.outbox.save(
                replace(
                    delivery,
                    attempts=attempts,
                    status="delivered",
                    delivered_at=now,
                    last_error=None,
                    last_status_code=response.status,
                )
            )
            return

        exhausted = attempts >= WEBHOOK_MAX_ATTEMPTS
        delay = WEBHOOK_RETRY_DELAYS_SECONDS[min(attempts - 1, len(WEBHOOK_RETRY_DELAYS_SECONDS) - 1)]
        await repos.outbox.save(
            replace(
                delivery,
                attempts=attempts,
                status="failed" if exhausted else "pending",
                next_attempt_at=delivery.next_attempt_at if exhausted else now + timedelta(seconds=delay),
                last_error=response.error or f"HTTP {response.status}",
                last_status_code=response.status,
            )
        )
