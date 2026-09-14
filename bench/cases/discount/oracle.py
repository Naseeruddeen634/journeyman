from lib.pricing import apply_discount
def test_oracle():
    assert apply_discount(250.0, 10.0) == 225.0
    assert apply_discount(49.99, 0.0) == 49.99
    assert apply_discount(120.0, 100.0) == 0.0
    assert apply_discount(19.99, 15.0) == 16.99
