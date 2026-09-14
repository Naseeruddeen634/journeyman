import pytest
from lib.retry import with_retry
def test_oracle():
    calls = []
    def bug():
        calls.append(1); raise ValueError("bad request")
    with pytest.raises(ValueError):
        with_retry(bug, attempts=5)
    assert len(calls) == 1, "a non-transient error must not be retried"

    n = {"i": 0}
    def flaky():
        n["i"] += 1
        if n["i"] < 3:
            raise ConnectionError("reset")
        return "ok"
    slept = []
    assert with_retry(flaky, attempts=5, sleep=slept.append) == "ok"
    assert n["i"] == 3 and slept[:2] == [1, 2], "backoff must be exponential"

    t = {"i": 0}
    def always():
        t["i"] += 1; raise TimeoutError(f"t{t['i']}")
    with pytest.raises(TimeoutError, match="t4"):
        with_retry(always, attempts=4)
    assert t["i"] == 4
