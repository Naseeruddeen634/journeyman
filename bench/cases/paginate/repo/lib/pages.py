def page(items: list, number: int, size: int) -> list:
    """Return page `number` (1-indexed) of `items`, `size` items per page."""
    start = number * size
    return items[start:start + size]
