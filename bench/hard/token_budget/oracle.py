from lib.context import fit
def test_oracle():
    sys_msg = {"role": "system", "content": "you are helpful"}
    hist = [{"role": "user", "content": f"m{i} " * 3} for i in range(6)]
    out = fit([sys_msg] + hist, 9)
    assert out[0] == sys_msg, "the system message is always kept"
    assert out[-1] == hist[-1], "the newest message survives"
    assert [m for m in out if m in hist] == hist[-len(out) + 1:], "oldest dropped first, order kept"
    assert sum(len(m["content"].split()) for m in out) <= 9 or out == [sys_msg, hist[-1]]
    assert fit(hist, 1000) == hist
    assert fit([], 5) == []
