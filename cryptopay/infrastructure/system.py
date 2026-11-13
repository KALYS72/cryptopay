from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from ..application.ports.security import PasswordHasher
from ..application.ports.system import Clock, Hasher, IdGenerator, IdPrefix, SecretGenerator

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


class RandomIdGenerator(IdGenerator):
    """Идентификаторы в стиле Stripe: pi_4Fz9Qk2LmP8xR1tB7cVwQa3e.

    Префикс сразу говорит, что это за объект, а 24 случайных символа base62
    (~142 бита энтропии) делают ссылку на страницу оплаты неугадываемой.
    """

    LENGTH = 24

    def generate(self, prefix: IdPrefix) -> str:
        # secrets.choice выбирает символ равновероятно (без «перекоса» от взятия по модулю).
        return f"{prefix}_{''.join(secrets.choice(_ALPHABET) for _ in range(self.LENGTH))}"


class CryptoSecretGenerator(SecretGenerator):
    def token(self, n_bytes: int = 24) -> str:
        return secrets.token_hex(n_bytes)


class Sha256Hasher(Hasher):
    def sha256(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()


class ScryptPasswordHasher(PasswordHasher):
    """Пароли — через scrypt: функция специально медленная и требует много памяти,
    поэтому перебор украденных хэшей стоит дорого. Соль своя у каждого пароля.

    Формат хэша: ``scrypt$<n>$<r>$<p>$<соль hex>$<хэш hex>`` — параметры хранятся рядом,
    чтобы их можно было усилить позже, не ломая старые пароли.
    """

    def __init__(self, n: int = 2**14, r: int = 8, p: int = 1) -> None:
        self._n, self._r, self._p = n, r, p

    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=self._n, r=self._r, p=self._p, dklen=32)
        return f"scrypt${self._n}${self._r}${self._p}${salt.hex()}${digest.hex()}"

    def verify(self, password: str, password_hash: str) -> bool:
        try:
            scheme, n, r, p, salt_hex, digest_hex = password_hash.split("$")
            if scheme != "scrypt":
                return False
            digest = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=32
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(digest.hex(), digest_hex)
