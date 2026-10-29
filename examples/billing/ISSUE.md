# Sales tax is rounded down on half-cent amounts

Our accountant flagged invoices where the tax is one cent short.

Example: one item at $2.50 with 5% tax.

```python
>>> from billing import Invoice
>>> inv = Invoice("acme", tax_rate=0.05).add("Sticker pack", 2.50)
>>> inv.tax
0.12
>>> inv.total
2.62
```

Expected: tax $0.13 and total $2.63. Our accounting system, and the tax authority, round half a cent up
("round half up"), so $0.125 becomes $0.13. The same happens with a $4.10 item at 5%: the tax shows $0.20
but should be $0.21.
