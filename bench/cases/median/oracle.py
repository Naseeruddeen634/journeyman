from lib.stats import median
def test_oracle():
    assert median([5, 1, 3]) == 3
    assert median([4, 1, 3, 2]) == 2.5
    assert median([7]) == 7
    assert median([10, 20]) == 15
