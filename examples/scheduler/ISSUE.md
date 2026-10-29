# Subscriptions that start on the 31st crash the billing run

Customers who sign up on the 31st of a month crash the nightly billing job:

```
Traceback (most recent call last):
  File "jobs/nightly.py", line 42, in run
    for when in sub.charge_dates():
  File "scheduler/recurring.py", line 21, in charge_dates
    when = add_months(self.start, i)
  File "scheduler/dates.py", line 13, in add_months
    return d.replace(year=year, month=month)
ValueError: day is out of range for month
```

Repro: `Subscription("acme", date(2024, 1, 31), months=3).charge_dates()`

Expected: `[2024-01-31, 2024-02-29, 2024-03-31]`. When the target month is shorter, bill on its last day,
and go back to the original day in the months that have it.
