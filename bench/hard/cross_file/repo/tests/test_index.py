import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.index import build_index


def test_index_has_every_doc():
    idx = build_index({"a": "hello", "b": "world!"})
    assert set(idx) == {"a", "b"}
