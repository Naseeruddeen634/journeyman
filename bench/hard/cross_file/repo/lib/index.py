"""Build a search index."""

from lib.embed import embed


def build_index(docs: dict[str, str]) -> dict[str, list[float]]:
    """Map document id to its embedding."""
    ids = list(docs)
    vectors = embed([docs[i] for i in ids], model="small", batch=32)
    return dict(zip(ids, vectors))
