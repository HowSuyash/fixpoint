"""Calendar helpers used by the subscription scheduler."""

from __future__ import annotations

from datetime import date, timedelta


def add_months(d: date, months: int) -> date:
    """Return the same day of the month, `months` months later (earlier if negative)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return d.replace(year=year, month=month)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def next_business_day(d: date) -> date:
    """Roll a date forward to Monday if it falls on a weekend."""
    while is_weekend(d):
        d += timedelta(days=1)
    return d
