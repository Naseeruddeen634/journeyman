"""The independent check: tests written from the documentation, judged before and after."""

import subprocess
import textwrap
from pathlib import Path

import pytest

from journeyman.autonomy.spec_check import (SPEC_FILE, Verdict, approx_floats, changed_functions,
                                            checker_prompt, counts, extract_code, judge, judge_all,
                                            write_checks)

BUGGY = textwrap.dedent('''
    """Embedding similarity."""
    import math

    EPS = 0.0

    def cosine(a, b):
        """Cosine similarity in [-1, 1]. A zero vector has similarity 0.0 with anything."""
        dot = sum(x * y for x, y in zip(a, b))
        return dot / (math.sqrt(sum(x * x for x in a)) + math.sqrt(sum(y * y for y in b)))

    def _helper():
        return 1

    def untouched():
        """Stays the same."""
        return 2
''')

HALF_FIX = BUGGY.replace("+ math.sqrt(sum(y * y", "* math.sqrt(sum(y * y")

RIGHT = BUGGY.replace(
    "    dot = sum(x * y for x, y in zip(a, b))\n"
    "    return dot / (math.sqrt(sum(x * x for x in a)) + math.sqrt(sum(y * y for y in b)))",
    "    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))\n"
    "    if na == 0 or nb == 0:\n        return 0.0\n"
    "    return sum(x * y for x, y in zip(a, b)) / (na * nb)")
assert RIGHT != BUGGY

CHECK = '''
from lib.vectors import cosine

def test_parallel():
    # (1*2 + 2*4) / (sqrt(5) * sqrt(20)) = 10 / 10 = 1
    assert cosine([1, 2], [2, 4]) == 1.0

def test_zero_vector():
    assert cosine([0, 0], [1, 2]) == 0.0
'''


def git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                   capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "__init__.py").write_text("")
    (root / "lib" / "vectors.py").write_text(BUGGY)
    (root / "tests").mkdir()
    (root / "tests" / "test_vectors.py").write_text("def test_x():\n    assert True\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def test_changed_functions_are_described_as_they_were_before_the_change(repo):
    (repo / "lib" / "vectors.py").write_text(HALF_FIX.replace("return 1", "return 3"))
    (repo / "tests" / "test_vectors.py").write_text("def test_x():\n    assert 1\n")
    specs = changed_functions(repo, ["lib/vectors.py", "tests/test_vectors.py"])
    assert [s.qualname for s in specs] == ["cosine"]          # _helper changed but is undocumented
    spec = specs[0]
    assert spec.module == "lib.vectors" and spec.signature == "def cosine(a, b):"
    assert "zero vector" in spec.docstring and spec.context == "EPS = 0.0"
    prompt = checker_prompt("cosine crashes on zero vectors", "", specs)
    assert "from lib.vectors import cosine" in prompt and "EPS = 0.0" in prompt
    assert "HALF_FIX" not in prompt and "* math.sqrt" not in prompt   # never the new code


def test_float_equality_becomes_approx_and_pytest_gets_imported():
    code = approx_floats("from m import f\ndef test_a():\n    assert f() == -1.0\n    assert f() == 1\n")
    assert "f() == pytest.approx(-1.0)" in code and "import pytest" in code
    assert code.rstrip().endswith("assert f() == 1")
    fixed = approx_floats("def test_a():\n    assert x == pytest.approx(2)\n")
    assert fixed.startswith("import pytest")


def test_extract_code_takes_a_parseable_block_with_tests():
    reply = "Here you go:\n```python\ndef helper(:\n```\n```python\ndef test_ok():\n    assert 1\n```"
    assert "def test_ok" in extract_code(reply)
    assert extract_code("no code at all") is None


def test_only_failures_about_the_code_count(tmp_path):
    spec = str(tmp_path / SPEC_FILE)
    assert counts(spec, "assert 2 == 3", tmp_path)
    assert counts(spec, "Failed: DID NOT RAISE <class 'ValueError'>", tmp_path)
    assert not counts(spec, "NameError: name 'pytest' is not defined", tmp_path)
    assert counts(str(tmp_path / "lib" / "vectors.py"), "ZeroDivisionError: float division by zero", tmp_path)
    assert not counts(str(tmp_path / ".venv/lib/python3.13/site-packages/openai/_client.py"),
                      "openai.OpenAIError: Missing credentials", tmp_path)
    assert not counts("/usr/lib/python3.13/json/decoder.py", "JSONDecodeError", tmp_path)


def test_a_half_fix_that_breaks_the_zero_vector_case_is_broke_not_unfixed(repo):
    (repo / "lib" / "vectors.py").write_text(HALF_FIX)
    v = judge(repo, extract_code(f"```python{CHECK}```"))
    assert v.usable and not v.ok
    assert list(v.broke) == ["test_zero_vector"]           # passed on the buggy original: 0 / sqrt(5)
    assert "ZeroDivisionError" in v.broke["test_zero_vector"]
    assert list(v.unfixed) == []
    assert not (repo / SPEC_FILE).exists()


def test_the_right_fix_passes_and_the_original_is_unfixed(repo):
    code = extract_code(f"```python{CHECK}```")
    (repo / "lib" / "vectors.py").write_text(RIGHT)
    assert judge(repo, code).ok
    (repo / "lib" / "vectors.py").write_text(BUGGY.replace('"""Embedding', '"""Still buggy. Embedding'))
    v = judge(repo, code)
    assert list(v.unfixed) == ["test_parallel"] and not v.broke


def test_failures_that_say_nothing_about_the_code_do_not_count(repo):
    code = "def test_env():\n    raise RuntimeError('no credentials')\n\ndef test_name():\n    undefined_name\n"
    (repo / "lib" / "vectors.py").write_text(RIGHT)
    v = judge(repo, code)
    assert v.ok


def test_agreement_is_required(repo, monkeypatch):
    from journeyman.autonomy import spec_check as S

    verdicts = {"a": Verdict(True, unfixed={"t": "assert 1 == 2"}), "b": Verdict(True, passed=3),
                "c": Verdict(True, broke={"u": "assert 0"}), "x": Verdict(False, note="broken check")}
    monkeypatch.setattr(S, "judge", lambda root, code: verdicts[code])
    assert judge_all(repo, ["a", "b"]).ok                      # one checker disagrees: no finding
    both = judge_all(repo, ["a", "c"])
    assert not both.ok and both.broke == {"u": "assert 0"} and both.unfixed == {"t": "assert 1 == 2"}
    assert not judge_all(repo, ["a", "x"]).usable              # one usable check is not enough
    assert "broke documented behaviour" in both.feedback() and "may be wrong" in both.feedback()
    assert both.concern().startswith("An independent test written from the docstring and the task passed before")


def test_write_checks_asks_independently_and_drops_unusable_replies(repo):
    replies = iter(["```python\ndef test_a():\n    assert 1 == 1.0\n```", "sorry, I can't", "```python\ndef test_b():\n    pass\n```"])
    prompts = []
    codes = write_checks(lambda p: prompts.append(p) or next(replies), "t", "", [], samples=3)
    assert len(codes) == 2 and len(set(prompts)) == 1
