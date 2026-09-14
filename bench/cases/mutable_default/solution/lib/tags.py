def add_tag(tag: str, tags: list | None = None) -> list:
    """Append a tag to `tags` (a new list if none given) and return it."""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags
