import re


def slugify(title: str) -> str:
    """Lowercase, words joined by single dashes, no leading or trailing dashes."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
