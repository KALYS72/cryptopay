"""Деньги — целые числа в минимальных единицах валюты.

Как у Stripe, где 10.00 USD передаются как ``amount=1000`` (центы), у нас
10 USDT = ``10_000_000`` (6 знаков после запятой), 1 BTC = 10^8 сатоши, 1 ETH = 10^18 wei.
Поэтому суммы везде хранятся как ``int``, а не ``float``: у float всего ~15 значащих
цифр и 0.1 + 0.2 != 0.3. В Python ``int`` не ограничен по длине.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class Currency:
    """Валюта, которую принимает система (Value Object)."""

    code: str
    """Код в API, строчными буквами, как у Stripe: usdt, btc, eth."""
    name: str
    decimals: int
    min_amount: int
    """Минимальная сумма платежа в минимальных единицах (у Stripe тоже есть минимум)."""

    @property
    def symbol(self) -> str:
        return self.code.upper()


MAX_AMOUNT_DIGITS = 30
"""Защита от абсурдных сумм: больше 10^30 минимальных единиц не принимаем."""

# [0-9], а не \d: в Python \d совпадает и с арабскими, и с деванагари цифрами.
_UNITS_RE = re.compile(r"[0-9]+")
_DECIMAL_RE = re.compile(r"[0-9]+(\.[0-9]+)?")


def parse_amount(value: int | str, *, param: str = "amount") -> int:
    """Сумма из запроса API: целое число минимальных единиц (число или строка из цифр)."""
    if isinstance(value, bool):  # bool — подкласс int в Python, True не должно стать 1
        raise ValidationError("Сумма должна быть целым числом минимальных единиц", param=param)
    text = str(value).strip()
    if _UNITS_RE.fullmatch(text) is None or len(text) > MAX_AMOUNT_DIGITS:
        raise ValidationError(
            "Сумма должна быть целым положительным числом минимальных единиц валюты, например 25000000",
            param=param,
        )
    amount = int(text)
    if amount <= 0:
        raise ValidationError("Сумма должна быть больше нуля", param=param)
    return amount


def parse_units(value: str, decimals: int) -> int:
    """'12.5' → 12500000 (для 6 знаков). Для форм кабинета, где сумму вводит человек."""
    normalized = str(value).strip().replace(",", ".", 1)
    if _DECIMAL_RE.fullmatch(normalized) is None:
        raise ValidationError("Сумма должна быть положительным числом, например 12.50", param="amount")
    whole, _, fraction = normalized.partition(".")
    if len(fraction) > decimals:
        raise ValidationError(f"Слишком много знаков после запятой: максимум {decimals}", param="amount")
    base: int = 10**decimals
    return int(whole) * base + int(fraction.ljust(decimals, "0") or "0")


def format_units(value: int, decimals: int) -> str:
    """12500000 → '12.5' (лишние нули справа отбрасываются)."""
    sign = "-" if value < 0 else ""
    base: int = 10**decimals
    whole, remainder = divmod(abs(value), base)
    fraction = str(remainder).rjust(decimals, "0").rstrip("0") if decimals else ""
    return f"{sign}{whole}{'.' + fraction if fraction else ''}"
