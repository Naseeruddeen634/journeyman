import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.tags import add_tag


def test_calls_are_independent():
    assert add_tag("a") == ["a"]
    assert add_tag("b") == ["b"]
