import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.chunking import chunk


def test_overlap_keeps_every_character():
    text = "abcdefghijklmnopqrstuvwxyz"
    joined = "".join(c[2:] if i else c for i, c in enumerate(chunk(text, 6, 2)))
    assert joined == text
