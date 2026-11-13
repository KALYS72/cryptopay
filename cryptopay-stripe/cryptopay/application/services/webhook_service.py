"""Адреса для вебхуков, журнал событий и история доставок (раздел «Разработчикам» в кабинете)."""

from __future__ import annotations

from collections.abc import Sequence

from ...domain.errors import BusinessRuleError, NotFoundError, ValidationError
from ...domain.webhook_endpoint import MAX_DESCRIPTION_LENGTH, WebhookEndpoint, normalize_enabled_events
from ..ports.repositories import EventRecord, ListQuery, Page, UnitOfWork, WebhookDelivery
from ..ports.system import Clock, IdGenerator, SecretGenerator
from ..ports.webhooks import WebhookUrlPolicy
from ..serializers import ObjectSerializer

MAX_ENDPOINTS = 5
TEST_EVENT_TYPE = "webhook_endpoint.test"


class WebhookService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        secrets: SecretGenerator,
        url_policy: WebhookUrlPolicy,
        serializer: ObjectSerializer,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._secrets = secrets
        self._url_policy = url_policy
        self._serializer = serializer

    # ── адреса ──
    async def create_endpoint(
        self, merchant_id: str, *, url: str, enabled_events: Sequence[str], description: str | None
    ) -> WebhookEndpoint:
        url = url.strip()
        self._url_policy.validate(url)  # защита от SSRF: только https и только внешние адреса
        text = (description or "").strip() or None
        if text is not None and len(text) > MAX_DESCRIPTION_LENGTH:
            raise ValidationError(f"Описание длиннее {MAX_DESCRIPTION_LENGTH} символов", param="description")
        endpoint = WebhookEndpoint(
            id=self._ids.generate("we"),
            merchant_id=merchant_id,
            url=url,
            secret="whsec_" + self._secrets.token(24),
            enabled_events=normalize_enabled_events(enabled_events),
            description=text,
            status="enabled",
            created_at=self._clock.now(),
        )
        async with self._uow.transaction() as repos:
            if len(await repos.webhook_endpoints.list_by_merchant(merchant_id)) >= MAX_ENDPOINTS:
                raise BusinessRuleError("too_many_endpoints", f"Можно добавить не больше {MAX_ENDPOINTS} адресов")
            await repos.webhook_endpoints.insert(endpoint)
        return endpoint

    async def list_endpoints(self, merchant_id: str) -> list[WebhookEndpoint]:
        return await self._uow.repos.webhook_endpoints.list_by_merchant(merchant_id)

    async def delete_endpoint(self, merchant_id: str, endpoint_id: str) -> WebhookEndpoint:
        endpoint = await self._own_endpoint(merchant_id, endpoint_id)
        await self._uow.repos.webhook_endpoints.delete(endpoint.id)
        return endpoint

    async def send_test(self, merchant_id: str, endpoint_id: str) -> WebhookDelivery:
        """Тестовое событие на один адрес — проверить, что сервер магазина принимает и проверяет подпись."""
        endpoint = await self._own_endpoint(merchant_id, endpoint_id)
        now = self._clock.now()
        event = EventRecord(
            id=self._ids.generate("evt"),
            merchant_id=merchant_id,
            type=TEST_EVENT_TYPE,
            subject_id=endpoint.id,
            data=self._serializer.webhook_endpoint(endpoint),
            created_at=now,
        )
        delivery = WebhookDelivery(
            id=self._ids.generate("whd"),
            merchant_id=merchant_id,
            event_id=event.id,
            endpoint_id=endpoint.id,
            event_type=event.type,
            url=endpoint.url,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            last_error=None,
            last_status_code=None,
            delivered_at=None,
            created_at=now,
        )
        async with self._uow.transaction() as repos:
            await repos.events.append(event)
            await repos.outbox.enqueue(delivery)
        return delivery

    # ── события ──
    async def list_events(
        self, merchant_id: str, query: ListQuery, *, type: str | None = None, subject_id: str | None = None
    ) -> Page[EventRecord]:
        return await self._uow.repos.events.page(merchant_id, query, type=type, subject_id=subject_id)

    async def get_event(self, merchant_id: str, event_id: str) -> EventRecord:
        event = await self._uow.repos.events.find_by_id(event_id)
        if event is None or event.merchant_id != merchant_id:
            raise NotFoundError("Событие", event_id)
        return event

    async def list_deliveries(self, merchant_id: str, event_id: str) -> list[WebhookDelivery]:
        event = await self.get_event(merchant_id, event_id)
        return await self._uow.repos.outbox.list_by_event(event.id)

    async def _own_endpoint(self, merchant_id: str, endpoint_id: str) -> WebhookEndpoint:
        endpoint = await self._uow.repos.webhook_endpoints.find_by_id(endpoint_id)
        if endpoint is None or endpoint.merchant_id != merchant_id:
            raise NotFoundError("Адрес вебхука", endpoint_id)
        return endpoint
