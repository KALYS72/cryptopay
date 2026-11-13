from __future__ import annotations

from abc import ABC, abstractmethod


class TokenService(ABC):
    """Выпуск и проверка сессий кабинета (реализация — JWT, см. JwtTokenService)."""

    @abstractmethod
    def issue(self, merchant_id: str) -> str: ...

    @abstractmethod
    def verify(self, token: str) -> str | None:
        """Возвращает merchant_id или None, если токен поддельный или просроченный."""


class PasswordHasher(ABC):
    """Хэширование паролей медленной функцией (реализация — scrypt из стандартной библиотеки)."""

    @abstractmethod
    def hash(self, password: str) -> str: ...

    @abstractmethod
    def verify(self, password: str, password_hash: str) -> bool: ...
