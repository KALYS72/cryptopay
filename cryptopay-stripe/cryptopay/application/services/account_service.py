"""Аккаунт продавца: регистрация, вход в кабинет, секретный API-ключ."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from ...domain.merchant import Merchant, normalize_email
from ..ports.repositories import UnitOfWork
from ..ports.security import PasswordHasher, TokenService
from ..ports.system import Clock, Hasher, IdGenerator, SecretGenerator

API_KEY_PREFIX = "sk_test_"
"""Как у Stripe: по префиксу видно, что это секретный ключ тестового режима."""
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


@dataclass(frozen=True, slots=True)
class SignInResult:
    token: str
    merchant: Merchant


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    secret: str
    """Показывается продавцу один раз. В базе хранится только SHA-256."""
    merchant: Merchant


@dataclass(frozen=True, slots=True)
class DemoAccount:
    email: str
    password: str
    business_name: str


class AccountService:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        secrets: SecretGenerator,
        hasher: Hasher,
        passwords: PasswordHasher,
        tokens: TokenService,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._secrets = secrets
        self._hasher = hasher
        self._passwords = passwords
        self._tokens = tokens
        # Хэш-«пустышка»: проверяем пароль даже для несуществующего e-mail, чтобы по времени ответа
        # нельзя было понять, зарегистрирован ли такой адрес.
        self._dummy_hash = passwords.hash(secrets.token(16))

    async def register(self, *, email: str, password: str, business_name: str) -> SignInResult:
        _check_password(password)
        merchant = Merchant.register(
            id=self._ids.generate("acct"),
            email=email,
            business_name=business_name,
            password_hash=self._passwords.hash(password),
            created_at=self._clock.now(),
        )
        async with self._uow.transaction() as repos:
            if await repos.merchants.find_by_email(merchant.email) is not None:
                raise ConflictError("Аккаунт с таким e-mail уже есть", code="email_taken", param="email")
            await repos.merchants.insert(merchant)
        return SignInResult(token=self._tokens.issue(merchant.id), merchant=merchant)

    async def sign_in(self, *, email: str, password: str) -> SignInResult:
        try:
            normalized = normalize_email(email)
        except ValidationError:
            normalized = ""
        merchant = await self._uow.repos.merchants.find_by_email(normalized) if normalized else None
        valid = self._passwords.verify(password, merchant.password_hash if merchant else self._dummy_hash)
        if merchant is None or not valid:
            raise AuthenticationError("Неверный e-mail или пароль", code="invalid_credentials")
        return SignInResult(token=self._tokens.issue(merchant.id), merchant=merchant)

    async def ensure_demo_account(self, demo: DemoAccount) -> Merchant:
        """Демо-аккаунт для защиты проекта: создаётся при старте, если его ещё нет."""
        existing = await self._uow.repos.merchants.find_by_email(normalize_email(demo.email))
        if existing is not None:
            return existing
        try:
            return (
                await self.register(email=demo.email, password=demo.password, business_name=demo.business_name)
            ).merchant
        except ConflictError:  # два экземпляра стартовали одновременно
            found = await self._uow.repos.merchants.find_by_email(normalize_email(demo.email))
            if found is None:
                raise
            return found

    def authenticate(self, token: str) -> str | None:
        return self._tokens.verify(token)

    async def authenticate_api_key(self, api_key: str) -> str | None:
        if not api_key.startswith(API_KEY_PREFIX):
            return None
        merchant = await self._uow.repos.merchants.find_by_api_key_hash(self._hasher.sha256(api_key))
        return merchant.id if merchant else None

    async def get(self, merchant_id: str) -> Merchant:
        merchant = await self._uow.repos.merchants.find_by_id(merchant_id)
        if merchant is None:
            raise NotFoundError("Аккаунт", merchant_id)
        return merchant

    async def rename(self, merchant_id: str, business_name: str) -> Merchant:
        async with self._uow.transaction() as repos:
            merchant = await repos.merchants.find_by_id(merchant_id)
            if merchant is None:
                raise NotFoundError("Аккаунт", merchant_id)
            merchant.rename(business_name)
            await repos.merchants.update(merchant)
        return merchant

    async def roll_api_key(self, merchant_id: str) -> IssuedApiKey:
        """Выпустить новый секретный ключ. Старый сразу перестаёт работать."""
        secret = API_KEY_PREFIX + self._secrets.token(24)
        async with self._uow.transaction() as repos:
            merchant = await repos.merchants.find_by_id(merchant_id)
            if merchant is None:
                raise NotFoundError("Аккаунт", merchant_id)
            merchant.set_api_key(key_hash=self._hasher.sha256(secret), last4=secret[-4:], now=self._clock.now())
            await repos.merchants.update(merchant)
        return IssuedApiKey(secret=secret, merchant=merchant)


def _check_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValidationError(f"Пароль — от {MIN_PASSWORD_LENGTH} до {MAX_PASSWORD_LENGTH} символов", param="password")
