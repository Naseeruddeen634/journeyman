import pytest
from lib.chunking import chunk
def covers(text, chunks, size, overlap):
    seen = set()
    pos = 0
    for c in chunks:
        assert 0 < len(c) <= size
        idx = text.find(c, max(0, pos - overlap))
        assert idx >= 0
        seen.update(range(idx, idx + len(c)))
        pos = idx + len(c)
    return seen == set(range(len(text)))
def test_oracle():
    t = "the quick brown fox jumps over the lazy dog" * 3
    for size, ov in [(10, 3), (7, 0), (20, 19), (100, 5)]:
        cs = chunk(t, size, ov)
        assert covers(t, cs, size, ov)
        for a, b in zip(cs, cs[1:]):
            assert a[-ov:] == b[:ov] if ov else True, "consecutive chunks must share exactly `overlap` chars"
    assert chunk("", 5, 1) == []
    with pytest.raises(ValueError):
        chunk("abc", 3, 3)
