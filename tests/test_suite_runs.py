"""Verification is only as good as the test run under it.

Two silent failures, both reproduced before being fixed:
  - a suite that cannot start (a conftest that does not import) produced no
    FAILED lines, so it read as green
  - a shift's worktree has no untracked .venv, so tests ran under Journeyman's
    own interpreter and a repo that was green in place was red in the sandbox
"""

import subprocess
import sys
from pathlib import Path

import pytest

from journeyman.autonomy.scout import _interpreter, failing_tests, main_worktree
from journeyman.autonomy.shift import SUITE_DID_NOT_RUN, failing_set


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout


def test_a_conftest_that_does_not_import_is_not_green(tmp_path):
    write(tmp_path, "tests/test_a.py", "def test_ok():\n    assert True\n")
    write(tmp_path, "conftest.py", "import does_not_exist\n")
    red, out = failing_set(tmp_path)
    assert red == {SUITE_DID_NOT_RUN} and "ImportError" in out


def test_an_ordinary_red_suite_lists_only_real_node_ids(tmp_path):
    write(tmp_path, "tests/test_a.py", "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n")
    red, _ = failing_set(tmp_path)
    assert red == {"tests/test_a.py::test_bad"}


def test_a_collection_error_is_named_not_hidden(tmp_path):
    write(tmp_path, "tests/test_a.py", "def test_ok():\n    assert True\n")
    write(tmp_path, "tests/test_b.py", "def test_(:\n")
    red, _ = failing_set(tmp_path)
    assert "tests/test_b.py" in red and SUITE_DID_NOT_RUN not in red


def test_no_tests_at_all_is_not_a_failure(tmp_path):
    write(tmp_path, "lib/x.py", "X = 1\n")
    assert failing_set(tmp_path)[0] == set()
    assert failing_tests(tmp_path) == []


def test_a_worktree_uses_the_venv_of_the_checkout_it_belongs_to(tmp_path):
    repo = tmp_path / "repo"
    write(repo, "lib/x.py", "X = 1\n")
    write(repo, ".gitignore", ".venv/\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    tree = repo / ".journeyman" / "worktrees" / "w"
    git(repo, "worktree", "add", "-q", "-b", "w", str(tree))
    assert not (tree / ".venv").exists()
    assert main_worktree(tree) == repo.resolve()
    assert main_worktree(repo) == repo.resolve()
    assert _interpreter(tree) == str(repo.resolve() / ".venv" / "bin" / "python")
    assert _interpreter(tmp_path / "not-a-repo") == sys.executable
