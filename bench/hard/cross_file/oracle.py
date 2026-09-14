import inspect
from lib.embed import embed
from lib.index import build_index
def test_oracle():
    params = inspect.signature(embed).parameters
    assert "batch_size" in params and "batch" not in params, "the definition was right; fix the caller"
    idx = build_index({"a": "hello", "b": "world!"})
    assert idx["a"] == [5.0, 32.0], "the caller's batch size of 32 must still be passed through"
    assert idx["b"] == [6.0, 32.0]
