"""Retry model calls on transient failures."""

TRANSIENT = (TimeoutError, ConnectionError)


def with_retry(fn, attempts: int = 3, sleep=lambda s: None):
    """Call fn(); retry on transient errors with exponential backoff.

    Non-transient errors (bugs, bad requests) are raised immediately, never retried.
    If every attempt fails, the last transient error is raised.
    """
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except TRANSIENT as exc:
            last = exc
            if attempt < attempts - 1:
                sleep(2 ** attempt)
    raise last
