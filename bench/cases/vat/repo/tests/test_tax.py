import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.tax import gross


def test_reduced_rate():
    assert gross(250.0, 13.5) == 283.75
