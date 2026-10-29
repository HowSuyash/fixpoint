# Rate limiter lets one extra request through

`SlidingWindowLimiter(limit=5, window=1.0)` allows **six** requests in the same second. The sixth one should be
rejected. We noticed because the upstream API we call, which allows 5 requests per second, started answering
some of our requests with HTTP 429.

```python
from ratelimit import SlidingWindowLimiter

limiter = SlidingWindowLimiter(limit=5, window=1.0)
print([limiter.allow("user-1") for _ in range(6)])
# prints [True, True, True, True, True, True]
# expected [True, True, True, True, True, False]
```
