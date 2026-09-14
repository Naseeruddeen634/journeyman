"""Fit a support conversation into the model's context window."""


def words(message: dict) -> int:
    return len(message["content"].split())


def fit(messages: list[dict], budget: int) -> list[dict]:
    """Drop messages until the conversation fits within `budget` words.

    The system message (role "system", always first when present) is always kept.
    The oldest other messages are dropped first, so the newest survive.
    Order is preserved.
    """
    kept = list(messages)
    while kept and sum(words(m) for m in kept) > budget:
        kept.pop()
    return kept
