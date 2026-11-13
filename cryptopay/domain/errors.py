"""Иерархия ошибок приложения — по образцу ошибок Stripe API.

У каждой ошибки есть ``type`` (класс ошибки), машинно-читаемый ``code`` и, если
ошибка в конкретном поле запроса, ``param``. Доменный и прикладной слои ничего
не знают про HTTP: они бросают AppError, а HTTP-слой
(cryptopay/infrastructure/http/errors.py) сам решает, какой статус вернуть.
"""

from __future__ import annotations

from typing import Any, Literal

ErrorType = Literal[
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "idempotency_error",
    "api_error",
]


class AppError(Exception):
    def __init__(
        self,
        type: ErrorType,
        code: str,
        message: str,
        *,
        param: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.type: ErrorType = type
        self.code = code
        self.message = message
        self.param = param
        self.details = details or {}


class ValidationError(AppError):
    """Некорректный параметр запроса."""

    def __init__(self, message: str, *, param: str | None = None, code: str = "parameter_invalid") -> None:
        super().__init__("invalid_request_error", code, message, param=param)


class NotFoundError(AppError):
    def __init__(self, what: str, id: str | None = None) -> None:
        suffix = f": {id}" if id else ""
        super().__init__("invalid_request_error", "resource_missing", f"{what} не найден{suffix}", param="id")


class AuthenticationError(AppError):
    def __init__(self, message: str = "Требуется авторизация", code: str = "authentication_required") -> None:
        super().__init__("authentication_error", code, message)


class PermissionDeniedError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("permission_error", "permission_denied", message)


class ConflictError(AppError):
    """Нарушение уникальности: e-mail уже занят, объект уже существует и т. п."""

    def __init__(self, message: str, *, code: str = "resource_already_exists", param: str | None = None) -> None:
        super().__init__("invalid_request_error", code, message, param=param)


class InvalidStateError(AppError):
    """Действие недопустимо в текущем статусе объекта (как payment_intent_unexpected_state у Stripe)."""

    def __init__(self, object_name: str, from_status: str, to_status: str) -> None:
        super().__init__(
            "invalid_request_error",
            f"{object_name}_unexpected_state",
            f"Нельзя перевести {object_name} из статуса «{from_status}» в «{to_status}»",
            details={"from": from_status, "to": to_status},
        )


class BusinessRuleError(AppError):
    """Запрос корректен по форме, но нарушает бизнес-правило (сумма возврата больше платежа и т. п.)."""

    def __init__(self, code: str, message: str, *, param: str | None = None) -> None:
        super().__init__("invalid_request_error", code, message, param=param)


class ConcurrencyError(AppError):
    """Оптимистическая блокировка: запись успели изменить параллельно."""

    def __init__(self, entity: str, id: str) -> None:
        super().__init__("api_error", "lock_timeout", f"{entity} {id} был изменён параллельно, повторите запрос")


class IdempotencyError(AppError):
    def __init__(self, code: Literal["idempotency_key_in_use", "idempotency_key_reused"], message: str) -> None:
        super().__init__("idempotency_error", code, message)


class WalletUnavailableError(AppError):
    """Внешний кошелёк (у нас — его имитация) не отвечает."""

    def __init__(self, message: str = "Криптокошелёк временно недоступен, повторите попытку") -> None:
        super().__init__("api_error", "wallet_unavailable", message)
