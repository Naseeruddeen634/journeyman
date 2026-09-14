import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib.slug import slugify


def test_punctuation_and_spaces():
    assert slugify("Hello  World!!") == "hello-world"
