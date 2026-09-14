"""Embedding similarity."""

import math


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. A zero vector has similarity 0.0 with anything."""
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)
