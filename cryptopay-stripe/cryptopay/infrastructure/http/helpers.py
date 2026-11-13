"""Общие кусочки HTTP-слоя: списки в формате Stripe, курсорная пагинация, идемпотентность."""

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

from fastapi import Request
from fastapi.responses import Response

from ...application.ports.repositories import ListQuery, Page, clamp_limit
from ...container import Container
from .dependencies import Principal

T = TypeVar("T")


def list_query(limit: int | None, starting_after: str | None) -> ListQuery:
    return ListQuery(limit=clamp_limit(limit), starting_after=starting_after or None)


def stripe_list(page: Page[T], url: str, to_json: Callable[[T], dict[str, Any]]) -> dict[str, Any]:
    return {"object": "list", "url": url, "has_more": page.has_more, "data": [to_json(item) for item in page.items]}


def plain_list(items: Iterable[T], url: str, to_json: Callable[[T], dict[str, Any]]) -> dict[str, Any]:
    return {"object": "list", "url": url, "has_more": False, "data": [to_json(item) for item in items]}


def json_response(payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return Response(content=body, status_code=status, media_type="application/json", headers=headers)


async def idempotent(
    request: Request,
    container: Container,
    principal: Principal,
    produce: Callable[[], Awaitable[dict[str, Any]]],
    status: int = 200,
) -> Response:
    """Выполнить POST с учётом заголовка Idempotency-Key (см. IdempotencyService)."""
    key = request.headers.get("idempotency-key")
    raw_body = await request.body()
    request_hash = hashlib.sha256(f"{request.method} {request.url.path}\n".encode() + raw_body).hexdigest()

    async def operation() -> tuple[int, str]:
        payload = await produce()
        return status, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    result = await container.services.idempotency.execute(
        merchant_id=principal.merchant_id, key=key, request_hash=request_hash, operation=operation
    )
    headers: dict[str, str] = {}
    if key is not None:
        headers["idempotency-key"] = key
        if result.replayed:
            headers["idempotent-replayed"] = "true"
    return Response(content=result.body, status_code=result.status, media_type="application/json", headers=headers)
