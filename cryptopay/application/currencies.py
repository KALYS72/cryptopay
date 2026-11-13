from __future__ import annotations

from collections.abc import Iterable

from ..domain.errors import ValidationError
from ..domain.money import Currency

DEFAULT_CURRENCIES: tuple[Currency, ...] = (
    Currency(code="usdt", name="Tether USD", decimals=6, min_amount=500_000),  # 0.50 USDT
    Currency(code="usdc", name="USD Coin", decimals=6, min_amount=500_000),
    Currency(code="btc", name="Bitcoin", decimals=8, min_amount=1_000),  # 0.00001 BTC
    Currency(code="eth", name="Ether", decimals=18, min_amount=10**14),  # 0.0001 ETH
)


class CurrencyRegistry:
    """Какие валюты принимает система. SOLID (O): новая валюта — одна строка в списке."""

    def __init__(self, currencies: Iterable[Currency]) -> None:
        self._by_code = {c.code: c for c in currencies}

    def get(self, code: str) -> Currency:
        currency = self._by_code.get(code.strip().lower())
        if currency is None:
            supported = ", ".join(self._by_code)
            raise ValidationError(f"Валюта «{code}» не поддерживается. Доступны: {supported}", param="currency")
        return currency

    def all(self) -> list[Currency]:
        return list(self._by_code.values())
