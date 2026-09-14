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

import pytest
def test_oracle(monkeypatch):
    fenced = '```json\n{"name": "Ada", "phone": "555"}\n```'
    mod, fake = load("lib.extract", fenced, monkeypatch)
    assert mod.extract_contact("hi")["name"] == "Ada", "fenced JSON is the most common model output"
    fake.content = '{"name": "Bo", "phone": "1"}'
    assert mod.extract_contact("hi")["name"] == "Bo"
    fake.content = "Sorry, I cannot help with that."
    try:
        out = mod.extract_contact("hi")
    except ValueError:
        out = None
    assert not (isinstance(out, dict) and out.get("name")), "garbage must not become a contact"
    assert finding_gone("lib/extract.py", "AIE003")
