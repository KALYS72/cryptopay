"""Зависимости FastAPI (Depends): контейнер и аутентификация продавца."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ...application.services.account_service import API_KEY_PREFIX
from ...container import Container
from ...domain.errors import AuthenticationError, PermissionDeniedError


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="Секретный ключ или сессия",
    description="Секретный ключ sk_test_… (сервер магазина) или токен сессии кабинета: Authorization: Bearer …",
)


@dataclass(frozen=True, slots=True)
class Principal:
    merchant_id: str
    method: Literal["session", "api_key"]


def _basic_key(request: Request) -> str | None:
    """Как в Stripe, ключ можно передать и через Basic-авторизацию: ``curl -u sk_test_…:``."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded.partition(":")[0] or None


def merchant_auth(*, allow_api_key: bool) -> Callable[..., Awaitable[Principal]]:
    """Аутентификация продавца. Два способа:

    * секретный ключ ``sk_test_…`` — сервер магазина, все эндпоинты /v1;
    * сессия кабинета (JWT после входа по e-mail и паролю) — всё, включая настройки аккаунта.
    """

    async def dependency(
        request: Request,
        container: ContainerDep,
        bearer: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
    ) -> Principal:
        token = bearer.credentials if bearer else _basic_key(request)
        if not token:
            raise AuthenticationError(
                "Нужна авторизация: передайте секретный ключ в заголовке Authorization: Bearer sk_test_…"
            )
        if token.startswith(API_KEY_PREFIX):
            if not allow_api_key:
                raise PermissionDeniedError("Этот раздел доступен только из кабинета продавца, не по API-ключу")
            merchant_id = await container.services.accounts.authenticate_api_key(token)
            if merchant_id is None:
                raise AuthenticationError("Неверный API-ключ", code="invalid_api_key")
            return Principal(merchant_id=merchant_id, method="api_key")
        merchant_id = container.services.accounts.authenticate(token)
        if merchant_id is None:
            raise AuthenticationError("Сессия истекла — войдите снова", code="session_expired")
        return Principal(merchant_id=merchant_id, method="session")

    return dependency


SessionOnly = Annotated[Principal, Depends(merchant_auth(allow_api_key=False))]
ApiKeyOrSession = Annotated[Principal, Depends(merchant_auth(allow_api_key=True))]
