"""Invoices: line items, tax and totals."""

from __future__ import annotations

from dataclasses import dataclass, field

from .money import format_money, round_money


@dataclass(frozen=True)
class LineItem:
    description: str
    unit_price: float
    quantity: int = 1

    @property
    def amount(self) -> float:
        return round_money(self.unit_price * self.quantity)


@dataclass
class Invoice:
    customer: str
    tax_rate: float = 0.0
    items: list[LineItem] = field(default_factory=list)

    def add(self, description: str, unit_price: float, quantity: int = 1) -> Invoice:
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        self.items.append(LineItem(description, unit_price, quantity))
        return self

    @property
    def subtotal(self) -> float:
        return round_money(sum(item.amount for item in self.items))

    @property
    def tax(self) -> float:
        return round_money(self.subtotal * self.tax_rate)

    @property
    def total(self) -> float:
        return round_money(self.subtotal + self.tax)

    def summary(self) -> str:
        lines = [f"Invoice for {self.customer}"]
        for item in self.items:
            price, amount = format_money(item.unit_price), format_money(item.amount)
            lines.append(f"  {item.description:<24}{item.quantity:>4} x {price:>10}{amount:>12}")
        tax_label = f"Tax ({self.tax_rate:.1%})"
        lines.append(f"  {'Subtotal':<40}{format_money(self.subtotal):>12}")
        lines.append(f"  {tax_label:<40}{format_money(self.tax):>12}")
        lines.append(f"  {'Total':<40}{format_money(self.total):>12}")
        return "\n".join(lines)
