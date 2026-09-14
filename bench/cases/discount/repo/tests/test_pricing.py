import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.pricing import apply_discount


def test_quarter_off_eighty():
    assert apply_discount(80.0, 25.0) == 60.0
