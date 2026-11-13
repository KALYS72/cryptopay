"""Заголовки и тело вебхука.

Подпись устроена как у Stripe (заголовок Stripe-Signature)::

    CryptoPay-Signature: t=<unix-время>,v1=<hex(HMAC-SHA256(whsec_…, "<t>.<тело>"))>

Время внутри подписи не даёт злоумышленнику переслать старый перехваченный
запрос (replay): магазин отбрасывает подписи старше 5 минут.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .ports.webhooks import WebhookSigner


def build_webhook_headers(
    *, signer: WebhookSigner, secret: str, body: str, event_type: str, delivery_id: str, now: datetime
) -> dict[str, str]:
    timestamp = int(now.timestamp())
    signature = signer.sign(secret, timestamp, body)
    return {
        "content-type": "application/json",
        "user-agent": "CryptoPay-Webhooks/2.0",
        "cryptopay-event": event_type,
        "cryptopay-delivery": delivery_id,
        "cryptopay-signature": f"t={timestamp},v1={signature}",
    }


def to_json(payload: dict[str, Any]) -> str:
    """Компактный JSON без экранирования кириллицы — ровно эти байты и подписываются."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
