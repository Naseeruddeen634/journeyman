from lib.vectors import cosine
def test_oracle():
    assert abs(cosine([1, 0], [0, 1])) < 1e-9
    assert abs(cosine([1, 2], [-1, -2]) + 1.0) < 1e-9
    assert abs(cosine([3, 4], [6, 8]) - 1.0) < 1e-9, "scale invariant"
    assert cosine([0, 0], [1, 2]) == 0.0
    assert cosine([0.0], [0.0]) == 0.0
