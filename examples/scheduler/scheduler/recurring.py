"""Charge dates for monthly subscriptions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .dates import add_months, next_business_day


@dataclass(frozen=True)
class Subscription:
    customer: str
    start: date
    months: int
    business_days_only: bool = False

    def charge_dates(self) -> list[date]:
        dates = []
        for i in range(self.months):
            when = add_months(self.start, i)
            if self.business_days_only:
                when = next_business_day(when)
            dates.append(when)
        return dates
