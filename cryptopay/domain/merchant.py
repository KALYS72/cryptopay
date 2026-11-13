"""Продавец (аккаунт) — тот, кто принимает платежи. Аналог Account в Stripe."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime

from .errors import ValidationError

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_EMAIL_LENGTH = 254
MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 100


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or _EMAIL_RE.fullmatch(email) is None:
        raise ValidationError("Укажите корректный e-mail, например shop@example.com", param="email")
    return email


def normalize_business_name(value: str) -> str:
    name = " ".join(value.split())
    if not MIN_NAME_LENGTH <= len(name) <= MAX_NAME_LENGTH:
        raise ValidationError(
            f"Название магазина — от {MIN_NAME_LENGTH} до {MAX_NAME_LENGTH} символов", param="business_name"
        )
    return name


@dataclass(slots=True)
class MerchantProps:
    id: str
    email: str
    business_name: str
    password_hash: str
    api_key_hash: str | None
    """SHA-256 секретного ключа. Сам ключ sk_test_… показывается один раз и нигде не хранится."""
    api_key_last4: str | None
    api_key_created_at: datetime | None
    created_at: datetime


class Merchant:
    __slots__ = ("_props",)

    def __init__(self, props: MerchantProps) -> None:
        self._props = props

    @classmethod
    def register(cls, *, id: str, email: str, business_name: str, password_hash: str, created_at: datetime) -> Merchant:
        return cls(
            MerchantProps(
                id=id,
                email=normalize_email(email),
                business_name=normalize_business_name(business_name),
                password_hash=password_hash,
                api_key_hash=None,
                api_key_last4=None,
                api_key_created_at=None,
                created_at=created_at,
            )
        )

    @classmethod
    def restore(cls, props: MerchantProps) -> Merchant:
        return cls(replace(props))

    @property
    def id(self) -> str:
        return self._props.id

    @property
    def email(self) -> str:
        return self._props.email

    @property
    def business_name(self) -> str:
        return self._props.business_name

    @property
    def password_hash(self) -> str:
        return self._props.password_hash

    @property
    def api_key_hash(self) -> str | None:
        return self._props.api_key_hash

    @property
    def api_key_last4(self) -> str | None:
        return self._props.api_key_last4

    @property
    def api_key_created_at(self) -> datetime | None:
        return self._props.api_key_created_at

    @property
    def created_at(self) -> datetime:
        return self._props.created_at

    def rename(self, business_name: str) -> None:
        self._props.business_name = normalize_business_name(business_name)

    def set_api_key(self, *, key_hash: str, last4: str, now: datetime) -> None:
        """Новый ключ сразу заменяет старый: старый перестаёт работать (как «Roll key» в Stripe)."""
        self._props.api_key_hash = key_hash
        self._props.api_key_last4 = last4
        self._props.api_key_created_at = now

    def snapshot(self) -> MerchantProps:
        return replace(self._props)
