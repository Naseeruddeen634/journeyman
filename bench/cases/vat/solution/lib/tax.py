def gross(net: float, rate: float) -> float:
    """Add VAT at `rate` percent to a net amount, rounded to 2 decimals."""
    return round(net * (1 + rate / 100), 2)
