import pytest

from billing import Invoice, format_money


def test_empty_invoice_is_zero():
    invoice = Invoice("acme", tax_rate=0.2)
    assert invoice.subtotal == 0
    assert invoice.total == 0


def test_totals_with_tax():
    invoice = Invoice("acme", tax_rate=0.08).add("Widget", 4.00, quantity=3)
    assert invoice.subtotal == 12.00
    assert invoice.tax == 0.96
    assert invoice.total == 12.96


def test_multiple_items():
    invoice = Invoice("acme", tax_rate=0.10).add("Widget", 2.00, 2).add("Gadget", 6.00)
    assert invoice.subtotal == 10.00
    assert invoice.total == 11.00


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        Invoice("acme").add("Widget", 1.00, quantity=0)


def test_format_money():
    assert format_money(1234.5) == "$1,234.50"
    assert format_money(-3) == "-$3.00"


def test_summary_lists_items():
    text = Invoice("acme", tax_rate=0.08).add("Widget", 4.00, 3).summary()
    assert "Widget" in text and "$12.96" in text
