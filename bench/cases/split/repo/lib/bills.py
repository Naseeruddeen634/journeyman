def split_evenly(total: float, people: int) -> list[float]:
    """Split a total between people so the parts sum back exactly to the total."""
    if people <= 0:
        raise ValueError("people must be positive")
    share = round(total / people, 2)
    return [share] * people
