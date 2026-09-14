def truncate(s: str, limit: int) -> str:
    """Shorten s to at most `limit` characters, ending with '...' when shortened."""
    if len(s) <= limit:
        return s
    return s[:limit] + "..."
