def apply_discount(amount: float, percent: float) -> float:
    """Apply a percentage discount (0-100) and round to 2 decimals."""
    return round(amount - percent, 2)
