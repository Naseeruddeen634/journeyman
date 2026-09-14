from lib.text import truncate
def test_oracle():
    out = truncate("abcdefghij", 5)
    assert len(out) <= 5 and out.endswith("..."), f"lost the ellipsis: {out!r}"
    assert truncate("hello world, this is long", 10).endswith("...")
    assert len(truncate("hello world, this is long", 10)) <= 10
    assert truncate("short", 10) == "short"
