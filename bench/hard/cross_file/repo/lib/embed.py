"""Embedding client."""


def embed(texts: list[str], *, model: str = "small", batch_size: int = 16) -> list[list[float]]:
    """Embed texts in batches. (batch_size was previously called `batch`.)"""
    return [[float(len(t)), float(batch_size)] for t in texts]
