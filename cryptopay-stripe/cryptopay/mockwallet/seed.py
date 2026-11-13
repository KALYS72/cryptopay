"""Тестовые кошельки — аналог тестовых карт Stripe.

У Stripe карта 4242 4242 4242 4242 всегда проходит, 4000 0000 0000 9995 — «недостаточно
средств» и т. д. Здесь то же самое, только кошельки: выбрав кошелёк, покупатель заранее
знает, чем закончится оплата. Это позволяет показать на защите каждый сценарий.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .models import DECIMALS, Profile, WalletAccount

TREASURY_ID = "treasury"


def units(amount: str, currency: str) -> int:
    whole, _, fraction = amount.partition(".")
    decimals = DECIMALS[currency]
    base: int = 10**decimals
    return int(whole) * base + int(fraction.ljust(decimals, "0") or "0")


def mock_address(account_id: str) -> str:
    """Детерминированный «адрес» кошелька: одинаковый после каждого перезапуска."""
    return "0x" + hashlib.sha256(f"mockwallet:{account_id}".encode()).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class SeedAccount:
    id: str
    name: str
    profile: Profile
    outcome: str
    balances: dict[str, str]


RICH = {"usdt": "10000", "usdc": "10000", "eth": "5", "btc": "0.5"}

SEED_ACCOUNTS: tuple[SeedAccount, ...] = (
    SeedAccount("alice", "Алиса", "normal", "Оплата проходит", RICH),
    SeedAccount(
        "bob",
        "Боб",
        "insufficient_funds",
        "Недостаточно средств",
        {"usdt": "2", "usdc": "2", "eth": "0.0001", "btc": "0.00001"},
    ),
    SeedAccount("carol", "Кэрол", "declines", "Кошелёк отклоняет платёж", RICH),
    SeedAccount("dave", "Дэйв", "slow", "Медленная сеть: транзакция ждёт блока дольше", RICH),
    SeedAccount("eve", "Ева", "fails_onchain", "Транзакция отправлена, но проваливается в сети", RICH),
    SeedAccount(
        TREASURY_ID,
        "CryptoPay — кошелёк платформы",
        "treasury",
        "Сюда приходят оплаты, отсюда уходят возвраты",
        {"usdt": "1000000", "usdc": "1000000", "eth": "1000", "btc": "50"},
    ),
)


def build_accounts() -> dict[str, WalletAccount]:
    return {
        seed.id: WalletAccount(
            id=seed.id,
            name=seed.name,
            address=mock_address(seed.id),
            profile=seed.profile,
            outcome=seed.outcome,
            balances={currency: units(amount, currency) for currency, amount in seed.balances.items()},
        )
        for seed in SEED_ACCOUNTS
    }
