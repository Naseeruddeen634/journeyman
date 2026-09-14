def median(values: list[float]) -> float:
    """Median of a non-empty list. Input need not be sorted."""
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
