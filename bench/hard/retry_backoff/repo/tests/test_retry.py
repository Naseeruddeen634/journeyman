import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pytest
from lib.retry import with_retry


def test_raises_when_every_attempt_times_out():
    def boom():
        raise TimeoutError("slow")
    with pytest.raises(TimeoutError):
        with_retry(boom, attempts=3)
