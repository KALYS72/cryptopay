from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ...domain.ledger import BalanceTransaction
from ...domain.payment_intent import PaymentIntentStatus
from ..currencies import CurrencyRegistry
from ..ports.repositories import DailyVolume, ListQuery, Page, UnitOfWork
from ..ports.system import Clock

OVERVIEW_DAYS = 7


@dataclass(frozen=True, slots=True)
class Overview:
    """Главная страница кабинета: баланс, оборот по дням и платежи по статусам."""

    balances: dict[str, int]
    volume: list[DailyVolume]
    days: list[date]
    payments_by_status: dict[PaymentIntentStatus, int]


class BalanceService:
    def __init__(self, *, uow: UnitOfWork, clock: Clock, currencies: CurrencyRegistry) -> None:
        self._uow = uow
        self._clock = clock
        self._currencies = currencies

    async def balances(self, merchant_id: str) -> dict[str, int]:
        return await self._uow.repos.ledger.balances(merchant_id)

    async def transactions(self, merchant_id: str, query: ListQuery) -> Page[BalanceTransaction]:
        return await self._uow.repos.ledger.page(merchant_id, query)

    async def overview(self, merchant_id: str) -> Overview:
        today = self._clock.now().date()
        first = today - timedelta(days=OVERVIEW_DAYS - 1)
        since = self._clock.now() - timedelta(days=OVERVIEW_DAYS)
        repos = self._uow.repos
        volume = [v for v in await repos.ledger.daily_volume(merchant_id, since) if v.day >= first]
        return Overview(
            balances=await repos.ledger.balances(merchant_id),
            volume=volume,
            days=[first + timedelta(days=i) for i in range(OVERVIEW_DAYS)],
            payments_by_status=await repos.payment_intents.count_by_status(merchant_id),
        )
