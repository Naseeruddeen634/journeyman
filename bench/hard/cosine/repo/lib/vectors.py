"""Embedding similarity."""

import math


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. A zero vector has similarity 0.0 with anything."""
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) + math.sqrt(sum(y * y for y in b)))
