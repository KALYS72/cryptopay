"""Перевод ошибок приложения в HTTP-ответы — в формате ошибок Stripe API.

Только здесь знают, что «не найдено» — это 404, а «конфликт» — 409. Формат ответа всегда один::

    {"error": {"type": "invalid_request_error", "code": "parameter_invalid",
               "message": "…", "param": "amount"}}
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from ...domain.errors import AppError
from ...paths import PUBLIC_DIR

logger = logging.getLogger("cryptopay.http")

CONFLICT_CODES = {
    "email_taken",
    "resource_already_exists",
    "transaction_reused",
    "duplicate_entry",
    "lock_timeout",
    "idempotency_key_in_use",
}


def status_for(error: AppError) -> int:
    if error.code == "resource_missing":
        return 404
    if error.code in CONFLICT_CODES:
        return 409
    if error.type == "authentication_error":
        return 401
    if error.type == "permission_error":
        return 403
    if error.type == "api_error":
        return 503 if error.code == "wallet_unavailable" else 500
    return 400


def error_response(
    status: int,
    type: str,
    code: str,
    message: str,
    *,
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"type": type, "code": code, "message": message}
    if param:
        body["param"] = param
    return JSONResponse(status_code=status, content={"error": body}, headers=headers)


class BodyTooLargeError(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Слишком большой запрос")


class RateLimitedError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


# Сообщения Pydantic по-русски — их видит пользователь в интерфейсе.
_MESSAGES: dict[str, str] = {
    "missing": "обязательный параметр",
    "string_type": "должно быть строкой",
    "int_type": "должно быть целым числом",
    "int_parsing": "должно быть целым числом",
    "int_from_float": "должно быть целым числом",
    "bool_type": "должно быть true или false",
    "bool_parsing": "должно быть true или false",
    "literal_error": "недопустимое значение",
    "enum": "недопустимое значение",
    "dict_type": "должно быть JSON-объектом",
    "list_type": "должно быть списком",
    "model_type": "должно быть JSON-объектом",
    "model_attributes_type": "должно быть JSON-объектом",
    "string_pattern_mismatch": "неверный формат",
    "extra_forbidden": "неизвестный параметр",
}


def _translate(issue: dict[str, Any]) -> str:
    kind = issue.get("type", "")
    ctx = issue.get("ctx") or {}
    if kind == "value_error":
        return str(ctx.get("error", issue.get("msg", "")))
    if kind == "string_too_short":
        return "пустое значение" if ctx.get("min_length") == 1 else f"минимум {ctx.get('min_length')} символов"
    if kind == "string_too_long":
        return f"не длиннее {ctx.get('max_length')} символов"
    if kind == "greater_than_equal":
        return f"должно быть не меньше {ctx.get('ge')}"
    if kind == "less_than_equal":
        return f"должно быть не больше {ctx.get('le')}"
    return _MESSAGES.get(kind, str(issue.get("msg", "некорректное значение")))


def _param(loc: Sequence[Any]) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] in ("body", "query", "path", "header"):
        parts = parts[1:]
    return ".".join(parts)


def _is_api(path: str) -> bool:
    return path.startswith(("/api/", "/v1/", "/wallet/api/")) or path in ("/api", "/v1")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(request: Request, error: AppError) -> JSONResponse:
        status = status_for(error)
        if status >= 500:
            logger.warning(error.message, extra={"path": request.url.path, "code": error.code})
        return error_response(status, error.type, error.code, error.message, param=error.param)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        issues = list(error.errors())
        if any(issue.get("type") == "json_invalid" for issue in issues):
            return error_response(400, "invalid_request_error", "parameter_invalid", "Тело запроса — некорректный JSON")
        first = issues[0] if issues else None
        if first is None:
            return error_response(400, "invalid_request_error", "parameter_invalid", "Некорректный запрос")
        param = _param(first["loc"])
        code = "parameter_missing" if first.get("type") == "missing" else "parameter_invalid"
        message = f"{param or 'запрос'}: {_translate(first)}"
        return error_response(400, "invalid_request_error", code, message, param=param or None)

    @app.exception_handler(RateLimitedError)
    async def rate_limited(request: Request, error: RateLimitedError) -> JSONResponse:
        return error_response(
            429,
            "rate_limit_error",
            "rate_limit",
            "Слишком много запросов. Подождите минуту",
            headers={"retry-after": str(error.retry_after)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> Response:
        path = request.url.path
        if _is_api(path) or error.status_code == 413:
            if error.status_code == 404:
                return error_response(
                    404, "invalid_request_error", "resource_missing", f"Маршрут {request.method} {path} не существует"
                )
            if error.status_code == 405:
                return error_response(
                    405,
                    "invalid_request_error",
                    "method_not_allowed",
                    f"Метод {request.method} для {path} не поддерживается",
                )
            if error.status_code == 413:
                return error_response(413, "invalid_request_error", "request_too_large", "Слишком большой запрос")
            return error_response(error.status_code, "api_error", f"http_{error.status_code}", str(error.detail))
        if error.status_code == 404:
            return FileResponse(PUBLIC_DIR / "404.html", status_code=404)
        return Response(str(error.detail), status_code=error.status_code, headers=getattr(error, "headers", None))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, error: Exception) -> JSONResponse:
        logger.error("unhandled error", exc_info=error, extra={"path": request.url.path})
        return error_response(500, "api_error", "internal_error", "Внутренняя ошибка сервера")
