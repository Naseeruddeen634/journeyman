def add_tag(tag: str, tags: list = []) -> list:
    """Append a tag to `tags` (a new list if none given) and return it."""
    tags.append(tag)
    return tags
