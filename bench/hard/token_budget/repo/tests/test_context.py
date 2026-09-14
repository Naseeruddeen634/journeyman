import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.context import fit


def test_newest_message_survives():
    msgs = [{"role": "user", "content": "one two three"}, {"role": "assistant", "content": "four five"},
            {"role": "user", "content": "six"}]
    assert fit(msgs, 3)[-1]["content"] == "six"
