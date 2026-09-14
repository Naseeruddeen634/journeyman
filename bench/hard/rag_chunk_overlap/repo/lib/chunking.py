"""Split documents into overlapping chunks for retrieval."""


def chunk(text: str, size: int, overlap: int = 0) -> list[str]:
    """Chunks of at most `size` characters; consecutive chunks share `overlap` characters.

    Every character of `text` appears in at least one chunk. overlap must be < size.
    """
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    step = size + overlap
    return [text[i:i + size] for i in range(0, len(text), step)]
