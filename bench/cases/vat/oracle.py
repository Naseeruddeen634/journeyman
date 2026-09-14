from lib.tax import gross
def test_oracle():
    assert gross(100.0, 23.0) == 123.0
    assert gross(80.0, 0.0) == 80.0
    assert gross(19.99, 9.0) == 21.79
