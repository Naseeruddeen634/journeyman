import pytest
from lib.bills import split_evenly
def test_oracle():
    for total, n in [(10.0, 4), (1.0, 3), (99.99, 7), (0.05, 2), (100.0, 1)]:
        parts = split_evenly(total, n)
        assert len(parts) == n
        assert round(sum(parts), 2) == round(total, 2)
        assert max(parts) - min(parts) <= 0.02 + 1e-9
    with pytest.raises(ValueError):
        split_evenly(10.0, 0)
