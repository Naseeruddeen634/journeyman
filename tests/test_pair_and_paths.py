"""Pair mode, and the path bug the benchmark found.

Every scanner used to judge skip directories by the absolute path. A repo with
an ancestor called build, dist or venv looked empty, and so did every sandbox,
because sandboxes live under .journeyman/worktrees. Inside a sandbox the
reviewer found nothing, so check_my_fix told the agent every finding was gone.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman import pair  # noqa: E402
from journeyman.autonomy import scout  # noqa: E402
from journeyman.patterns.smells import review  # noqa: E402

BAD = ('def _client():\n    from openai import OpenAI\n    return OpenAI(timeout=3)\n'
       'def go(t):\n    return _client().chat.completions.create(model="gpt-4o-mini",'
       ' messages=[], max_tokens=5)\n')


@pytest.mark.parametrize("ancestor", [".journeyman/worktrees/x", "build", "dist", "venv", "node_modules"])
def test_a_repo_under_a_skip_named_directory_is_still_seen(tmp_path, ancestor):
    repo = tmp_path / ancestor / "repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "a.py").write_text(BAD)
    assert [f.code for f in review(repo)] == ["AIE001"]
    assert len(scout._py_files(repo)) == 1
    assert len(pair._py_files(repo)) == 1


def test_skip_directories_inside_the_repo_are_still_skipped(tmp_path):
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "a.py").write_text(BAD)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "b.py").write_text(BAD)
    assert review(tmp_path) == []
    assert scout._py_files(tmp_path) == []


# ---- pair: which tests does a save affect ------------------------------


@pytest.fixture
def project(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("")
    (tmp_path / "lib" / "money.py").write_text("def vat(n):\n    return round(n * 1.23, 2)\n")
    (tmp_path / "lib" / "invoice.py").write_text(
        "from lib.money import vat\n\ndef total(n):\n    return vat(n)\n")
    (tmp_path / "lib" / "unrelated.py").write_text("def hello():\n    return 'hi'\n")
    (tmp_path / "tests").mkdir()
    hdr = "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
    (tmp_path / "tests" / "test_invoice.py").write_text(
        hdr + "from lib.invoice import total\n\ndef test_total():\n    assert total(100) == 123.0\n")
    (tmp_path / "tests" / "test_unrelated.py").write_text(
        hdr + "from lib.unrelated import hello\n\ndef test_hello():\n    assert hello() == 'hi'\n")
    return tmp_path


def test_a_save_reaches_tests_through_other_modules(project):
    """money.py is imported by invoice.py, which test_invoice imports."""
    names = [t.name for t in pair.affected_tests(project, "lib/money.py")]
    assert names == ["test_invoice.py"]


def test_a_save_does_not_run_unrelated_tests(project):
    names = [t.name for t in pair.affected_tests(project, "lib/unrelated.py")]
    assert names == ["test_unrelated.py"]


def test_saving_a_test_runs_that_test(project):
    names = [t.name for t in pair.affected_tests(project, "tests/test_invoice.py")]
    assert names == ["test_invoice.py"]


def test_pair_is_silent_when_nothing_changed_for_the_worse(project):
    state = pair.PairState(project)
    state.prime()
    (project / "lib" / "money.py").write_text("def vat(n):\n    return round(n * 1.23, 2)  # tidy\n")
    assert state.on_save([project / "lib" / "money.py"]) == []


def test_pair_speaks_when_a_green_test_goes_red(project):
    state = pair.PairState(project)
    state.prime()
    (project / "lib" / "money.py").write_text("def vat(n):\n    return round(n * 1.32, 2)\n")
    msgs = state.on_save([project / "lib" / "money.py"])
    assert msgs and msgs[0].startswith("RED after saving money.py")
    assert "test_total" in msgs[0]


def test_pair_says_when_it_recovers(project):
    state = pair.PairState(project)
    state.prime()
    (project / "lib" / "money.py").write_text("def vat(n):\n    return round(n * 1.32, 2)\n")
    state.on_save([project / "lib" / "money.py"])
    (project / "lib" / "money.py").write_text("def vat(n):\n    return round(n * 1.23, 2)\n")
    msgs = state.on_save([project / "lib" / "money.py"])
    assert any(m.startswith("green again") for m in msgs)


def test_a_save_that_stops_the_tests_running_is_red_not_silent(project):
    state = pair.PairState(project)
    state.prime()
    assert state.on_save([project / "lib" / "money.py"]) == []
    (project / "conftest.py").write_text("import does_not_exist\n")
    msgs = state.on_save([project / "conftest.py"])
    assert len(msgs) == 1 and msgs[0].startswith("RED after saving conftest.py: the tests no longer run")
    assert "ModuleNotFoundError" in msgs[0] or "ImportError" in msgs[0]
    (project / "conftest.py").write_text("")
    assert state.on_save([project / "conftest.py"]) == ["the tests run again"]


def test_saving_a_conftest_runs_the_tests_beneath_it(project):
    (project / "tests" / "conftest.py").write_text("")
    names = sorted(t.name for t in pair.affected_tests(project, "tests/conftest.py"))
    assert names == ["test_invoice.py", "test_unrelated.py"]


def test_pair_mentions_a_new_review_finding_in_the_file_you_saved(project):
    state = pair.PairState(project)
    state.prime()
    (project / "lib" / "unrelated.py").write_text(BAD + "\ndef hello():\n    return 'hi'\n")
    msgs = state.on_save([project / "lib" / "unrelated.py"])
    assert any("new: AIE001" in m for m in msgs)


def test_a_same_size_edit_in_the_same_second_is_not_read_from_stale_bytecode(tmp_path):
    """Python validates .pyc by size and whole-second mtime. A same-length edit
    within the second runs the old code. Found because a pair test passed
    standalone and failed under pytest, where the edit landed faster."""
    import subprocess

    from journeyman.autonomy.scout import fresh_env

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("")
    (tmp_path / "lib" / "m.py").write_text("def f():\n    return 123\n")
    run = lambda env: subprocess.run(
        [sys.executable, "-c", "import lib.m; print(lib.m.f())"],
        cwd=tmp_path, capture_output=True, text=True, env=env).stdout.strip()
    assert run(fresh_env()) == "123"
    (tmp_path / "lib" / "m.py").write_text("def f():\n    return 132\n")
    assert run(fresh_env()) == "132"
