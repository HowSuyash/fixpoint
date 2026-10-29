from datetime import date

from scheduler import Subscription, add_months, next_business_day


def test_add_months_same_year():
    assert add_months(date(2024, 3, 15), 2) == date(2024, 5, 15)


def test_add_months_crosses_year():
    assert add_months(date(2024, 11, 10), 3) == date(2025, 2, 10)


def test_add_months_negative():
    assert add_months(date(2024, 1, 20), -1) == date(2023, 12, 20)


def test_next_business_day_rolls_weekend_forward():
    assert next_business_day(date(2024, 6, 15)) == date(2024, 6, 17)  # Saturday -> Monday
    assert next_business_day(date(2024, 6, 18)) == date(2024, 6, 18)


def test_charge_dates_monthly():
    sub = Subscription("acme", date(2024, 1, 10), months=3)
    assert sub.charge_dates() == [date(2024, 1, 10), date(2024, 2, 10), date(2024, 3, 10)]


def test_charge_dates_business_days_only():
    sub = Subscription("acme", date(2024, 6, 15), months=1, business_days_only=True)
    assert sub.charge_dates() == [date(2024, 6, 17)]
