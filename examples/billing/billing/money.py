"""Money helpers. Amounts are floats in the account currency."""


def round_money(amount: float) -> float:
    """Round an amount to whole cents."""
    return round(amount, 2)


def format_money(amount: float, symbol: str = "$") -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(amount):,.2f}"
