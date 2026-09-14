from lib.pages import page
def test_oracle():
    items = list(range(1, 11))
    assert page(items, 2, 3) == [4, 5, 6]
    assert page(items, 4, 3) == [10]
    assert page(items, 5, 3) == []
    assert page([], 1, 5) == []
