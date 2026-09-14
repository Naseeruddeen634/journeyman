import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.pages import page


def test_first_page():
    assert page(list(range(1, 11)), 1, 3) == [1, 2, 3]
