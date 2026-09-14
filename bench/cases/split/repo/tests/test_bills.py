import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.bills import split_evenly


def test_three_way_split_sums():
    assert round(sum(split_evenly(100.0, 3)), 2) == 100.0
