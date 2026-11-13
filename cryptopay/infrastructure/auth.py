from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from ..application.ports.security import TokenService


class JwtTokenService(TokenService):
    """Сессии продавца — JWT (HS256), подписанные секретом сервера. Хранить сессии в БД не нужно."""

    ISSUER = "cryptopay"

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret
        self._ttl = timedelta(seconds=ttl_seconds)

    def issue(self, merchant_id: str) -> str:
        now = datetime.now(UTC)
        claims = {"sub": merchant_id, "iss": self.ISSUER, "iat": now, "exp": now + self._ttl}
        return jwt.encode(claims, self._secret, algorithm="HS256", headers={"typ": "JWT"})

    def verify(self, token: str) -> str | None:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],  # явный список: защита от подмены алгоритма (alg=none)
                issuer=self.ISSUER,
                options={"require": ["sub", "exp", "iss"]},
            )
        except jwt.PyJWTError:
            return None
        subject = payload.get("sub")
        return subject if isinstance(subject, str) else None
