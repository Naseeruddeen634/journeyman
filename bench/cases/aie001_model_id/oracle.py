import importlib, sys, types
from pathlib import Path


class FakeClient:
    def __init__(self, content="ok"):
        self.content = content
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls.append(kw)
        msg = types.SimpleNamespace(content=self.content, parsed=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)],
            usage=types.SimpleNamespace(total_tokens=3))

    parse = create


def load(module, content="ok", monkeypatch=None):
    """Import the module under test with openai.OpenAI replaced by a fake."""
    import openai
    fake = FakeClient(content)
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: fake)
    sys.modules.pop(module, None)
    return importlib.import_module(module), fake


def finding_gone(rel, code):
    from journeyman.patterns.smells import review
    here = Path(__file__).parent
    return not [f for f in review(here) if f.code == code and f.where.split(":")[0] == rel]

def test_oracle(monkeypatch):
    for var in [k for k in __import__("os").environ if "MODEL" in k]:
        monkeypatch.delenv(var, raising=False)
    mod, fake = load("lib.classify", "billing", monkeypatch)
    assert mod.classify("refund please") == "billing"
    assert fake.calls[-1]["model"] == "gpt-4o-mini", "the default model must not change"
    src = (Path(__file__).parent / "lib" / "classify.py").read_text()
    assert "environ" in src or "getenv" in src, "model id should be configurable from the environment"
    assert finding_gone("lib/classify.py", "AIE001")
