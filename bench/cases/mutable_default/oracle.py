from lib.tags import add_tag
def test_oracle():
    mine = ["x"]
    out = add_tag("y", mine)
    assert out == ["x", "y"] and out is mine, "must still append to a list that was passed in"
    assert add_tag("z") == ["z"]
    assert add_tag("w") == ["w"]
