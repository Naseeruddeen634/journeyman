def split_evenly(total: float, people: int) -> list[float]:
    """Split a total between people so the parts sum back exactly to the total."""
    if people <= 0:
        raise ValueError("people must be positive")
    parts = [round(total / people, 2)] * (people - 1)
    parts.append(round(total - sum(parts), 2))
    return parts
