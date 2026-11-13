"""Адрес на сервере продавца, куда CryptoPay присылает события (вебхуки)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .errors import ValidationError
from .events import EVENT_TYPES

WebhookEndpointStatus = Literal["enabled", "disabled"]
ALL_EVENTS = "*"
MAX_DESCRIPTION_LENGTH = 200


def normalize_enabled_events(events: Sequence[str]) -> tuple[str, ...]:
    """Список событий, на которые подписан адрес. ``["*"]`` — все события."""
    unique = tuple(dict.fromkeys(e.strip() for e in events if e.strip()))
    if not unique:
        raise ValidationError("Выберите хотя бы одно событие или «*» для всех", param="enabled_events")
    if ALL_EVENTS in unique:
        return (ALL_EVENTS,)
    unknown = [e for e in unique if e not in EVENT_TYPES]
    if unknown:
        raise ValidationError(f"Неизвестное событие: {unknown[0]}", param="enabled_events")
    return unique


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    id: str
    merchant_id: str
    url: str
    secret: str
    """whsec_… — им подписывается каждый вебхук; продавец проверяет подпись этим же секретом."""
    enabled_events: tuple[str, ...]
    description: str | None
    status: WebhookEndpointStatus
    created_at: datetime

    def accepts(self, event_type: str) -> bool:
        return self.status == "enabled" and (ALL_EVENTS in self.enabled_events or event_type in self.enabled_events)
