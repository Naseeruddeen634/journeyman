import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.stats import median


def test_even_length():
    assert median([1, 2, 3, 4]) == 2.5
