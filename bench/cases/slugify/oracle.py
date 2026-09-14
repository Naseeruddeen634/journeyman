from lib.slug import slugify
def test_oracle():
    assert slugify("  A -- B  ") == "a-b"
    assert slugify("Already-slug") == "already-slug"
    assert slugify("Q3 2026: Results") == "q3-2026-results"
