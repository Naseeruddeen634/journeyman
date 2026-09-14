import pytest
from lib.bills import split_evenly
def test_oracle():
    # The docstring promises the parts sum exactly to the total. It does not
    # promise equal parts, so neither does the oracle: an earlier version
    # required a spread under 0.02 and would have failed the standard correct
    # fix, where the last share absorbs the rounding remainder.
    for total, n in [(10.0, 4), (1.0, 3), (99.99, 7), (0.05, 2), (100.0, 1)]:
        parts = split_evenly(total, n)
        assert len(parts) == n
        assert round(sum(parts), 2) == round(total, 2)
        assert all(p >= 0 for p in parts)
    with pytest.raises(ValueError):
        split_evenly(10.0, 0)
