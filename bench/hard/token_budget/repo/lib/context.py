"""Fit a chat history into a model's context window."""


def fit(messages: list[dict], budget: int, count=lambda m: len(m["content"].split())) -> list[dict]:
    """Drop messages until the total count is within budget.

    The system message (role "system", if present, always first) is always kept.
    The oldest non-system messages are dropped first, so the newest survive.
    Order is preserved.
    """
    kept = list(messages)
    while kept and sum(count(m) for m in kept) > budget:
        kept.pop()
    return kept
