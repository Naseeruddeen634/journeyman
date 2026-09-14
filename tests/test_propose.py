"""propose: a pull request description written from the shift record.

It must describe what was checked and what was not, lead with the agent's own
doubts, and never push or open anything itself.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNEYMAN_HOME", str(tmp_path / "home"))
    import journeyman.home as home
    import journeyman.propose as propose
    importlib.reload(home)
    importlib.reload(propose)

    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "a.py").write_text("def f():\n    return 1\n")
    g("add", "-A"); g("commit", "-q", "-m", "init")
    g("checkout", "-q", "-b", "journeyman/20260914-fix")
    (repo / "a.py").write_text("def f():\n    return 2\n")
    g("commit", "-qam", "fix"); g("checkout", "-q", "main")

    def record(**over):
        d = {"task": {"kind": "failing_test", "title": "f returns the wrong value",
                      "where": "tests/test_a.py", "meta": {}},
             "branch": "journeyman/20260914-fix", "outcome": "fixed",
             "failures_before": ["tests/test_a.py::test_f"], "failures_after": [],
             "fixed_failures": ["tests/test_a.py::test_f"], "new_failures": [],
             "concerns": [], "feedback_rounds": 0, "minutes": 0.4,
             "budget": {"iterations": 4}, "brain": "qwen3-coder:30b"}
        d.update(over)
        return home.record_shift(str(repo), d)

    yield repo, record, propose
    importlib.reload(home)
    importlib.reload(propose)


def test_it_writes_a_description_and_prints_commands_without_running_them(env, monkeypatch):
    repo, record, propose = env
    record()
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/x.git"], cwd=repo)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/gh" if name == "gh" else None)
    r = propose.propose(repo)
    assert r["ok"], r
    assert Path(r["body_file"]).exists()
    assert r["commands"][0].startswith("git push -u origin")
    assert r["commands"][1].startswith("gh pr create --base main --head")
    log = subprocess.run(["git", "log", "--all", "--oneline"], cwd=repo, capture_output=True, text=True)
    assert "origin/" not in subprocess.run(["git", "branch", "-a"], cwd=repo,
                                           capture_output=True, text=True).stdout, \
        "propose must not push"


def test_no_gh_means_no_gh_command(env, monkeypatch):
    """Printing a command that will fail is worse than printing none."""
    repo, record, propose = env
    record()
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/x.git"], cwd=repo)
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = propose.propose(repo)
    assert [c for c in r["commands"] if c.startswith("gh ")] == []
    assert any("gh is not installed" in n for n in r["notes"])


def test_no_remote_means_no_push_command(env):
    repo, record, propose = env
    record()
    r = propose.propose(repo)
    assert r["commands"] == []
    assert any("no remote" in n for n in r["notes"])


def test_the_description_says_what_was_checked_and_what_was_not(env):
    repo, record, propose = env
    record()
    text = propose.propose(repo)["body"]
    assert "1 failing before, 0 failing after" in text
    assert "`tests/test_a.py::test_f`" in text
    assert "Newly failing: none" in text
    assert "## What was not checked" in text
    assert "It did not push this branch" in text
    assert "a.py" in text, "the diff stat should be included"


def test_the_agents_own_doubts_come_before_the_diff(env):
    repo, record, propose = env
    record(outcome="fixed_with_concerns",
           concerns=["It lists 2 changes but the diff moves 1 line(s)."])
    text = propose.propose(repo)["body"]
    assert text.index("Read the diff before merging") < text.index("## Changes")
    assert "diff moves 1 line" in text


def test_a_stuck_shift_is_not_proposed(env):
    repo, record, propose = env
    record(outcome="stuck")
    assert not propose.propose(repo)["ok"]
    assert not propose.propose(repo, "journeyman/20260914-fix")["ok"]


def test_a_branch_that_was_deleted_is_reported_not_proposed(env):
    repo, record, propose = env
    record()
    subprocess.run(["git", "branch", "-D", "journeyman/20260914-fix"], cwd=repo, capture_output=True)
    r = propose.propose(repo)
    assert not r["ok"] and "no longer exists" in r["reason"]


def test_a_review_fix_reports_the_finding_verdict(env):
    repo, record, propose = env
    record(task={"kind": "ai_review", "title": "AIE002: no max_tokens", "where": "a.py:2",
                 "meta": {"code": "AIE002"}}, finding_resolved=True,
           failures_before=[], failures_after=[], fixed_failures=[])
    text = propose.propose(repo)["body"]
    assert "Review check AIE002: no longer fires on this file" in text
