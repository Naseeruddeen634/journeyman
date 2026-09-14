import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.text import truncate


def test_respects_limit():
    assert len(truncate("abcdefghij", 5)) == 5
