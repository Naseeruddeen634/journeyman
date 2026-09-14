"""The OS boundary around code a shift causes to run, and the tool bug it fixed.

Two findings from probing with harmless canaries:
  - the agent's run_tests tool was refused on every call on the development
    machine, because the interpreter path contains a space and the command was a
    shell string; every shift before this fixed code without running tests
  - once tests did run, test code the agent wrote could write anywhere the user
    can, read credentials from the environment, and reach the network
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman.autonomy import jail  # noqa: E402
from journeyman.autonomy.guardrails import Budget, Refused, check_command, open_sandbox  # noqa: E402
from journeyman.autonomy.scout import Task  # noqa: E402
from journeyman.autonomy.shift import ShiftResult, _tools_for  # noqa: E402

needs_sandbox_exec = pytest.mark.skipif(not jail.available(), reason="sandbox-exec not available")


def test_secret_looking_environment_is_removed():
    env = {"PATH": "/bin", "HOME": "/h", "OPENROUTER_API_KEY": "x", "AWS_ACCESS_KEY_ID": "x",
           "GITHUB_TOKEN": "x", "DB_PASSWORD": "x", "MY_SESSION_COOKIE": "x",
           "PYTHONPYCACHEPREFIX": "/tmp/p", "LANG": "C"}
    assert set(jail.scrub_env(env)) == {"PATH", "HOME", "PYTHONPYCACHEPREFIX", "LANG"}


def test_profile_denies_network_and_writes_and_names_credential_stores(tmp_path):
    prof = jail.profile(tmp_path)
    assert "(deny network*)" in prof and "(deny file-write*)" in prof
    assert f'(allow file-write* (subpath "{tmp_path.resolve()}"))' in prof
    assert ".ssh" in prof and "Library/Keychains" in prof and ".journeyman/lessons.json" in prof


def test_wrap_reports_unconfined_where_no_sandbox_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(jail, "available", lambda: False)
    argv, env, confined = jail.wrap(["python", "-m", "pytest"], tmp_path, {"OPENAI_API_KEY": "x", "PATH": "/b"})
    assert argv == ["python", "-m", "pytest"] and confined is False
    assert "OPENAI_API_KEY" not in env, "credentials are removed even when unconfined"


@pytest.mark.parametrize("cmd", ["python -m pytest $(id)", "python -m pytest `id`",
                                 "echo x > ~/.bashrc", "cat < /etc/passwd", "sleep 9 & echo",
                                 "python -m pytest ${HOME}"])
def test_shell_escapes_are_refused(cmd):
    with pytest.raises(Refused):
        check_command(cmd)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    repo = tmp_path / "space in path" / "repo"       # the condition that broke run_tests
    shutil.copytree(ROOT / "bench/cases/vat/repo", repo)
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    g("add", "-A"); g("commit", "-qm", "c")
    box = open_sandbox(repo, "journeyman/jail-test", Budget())
    tools = {t.tool_name: t for t in _tools_for(box, ShiftResult(task=None),
                                                   Task("failing_test", "t", "", "x", 100))}
    return box, tools["run_tests"]._tool_func


def test_the_agent_can_actually_run_tests(sandbox):
    """On the development machine this returned 'Refused' on every call."""
    box, run_tests = sandbox
    out = run_tests("")
    assert not out.startswith("Refused"), out
    assert "1 failed" in out


@pytest.mark.parametrize("target", ["tests; python -c 'x'", "tests $(id)", "tests`id`",
                                    "../../etc", "/etc/passwd", "tests && rm -rf x"])
def test_test_targets_are_paths_not_commands(sandbox, target):
    _, run_tests = sandbox
    assert run_tests(target).startswith("Refused")


@needs_sandbox_exec
def test_code_the_agent_writes_cannot_escape_when_tests_run(sandbox, monkeypatch):
    box, run_tests = sandbox
    outside = Path.home() / ".journeyman" / "jail-test-canary"
    shutil.rmtree(outside, ignore_errors=True)
    outside.mkdir(parents=True)
    monkeypatch.setenv("FAKE_JAIL_API_KEY", "sk-canary")
    box.write("tests/test_escape.py", f'''import os, socket, pathlib
def test_escape():
    r = {{}}
    try:
        open(r"{outside}/w.txt", "w").write("x"); r["write"] = "ESCAPED"
    except OSError:
        r["write"] = "blocked"
    r["env"] = os.environ.get("FAKE_JAIL_API_KEY", "hidden")
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3); r["net"] = "ESCAPED"
    except OSError:
        r["net"] = "blocked"
    try:
        (pathlib.Path.home() / ".ssh").mkdir(exist_ok=True)
    except OSError:
        pass
    pathlib.Path("r.txt").write_text(repr(r))
''')
    try:
        run_tests("tests/test_escape.py")
        assert (box.root / "r.txt").exists(), "the test must still be able to write in its worktree"
        results = eval((box.root / "r.txt").read_text())
        assert results == {"write": "blocked", "env": "hidden", "net": "blocked"}
        assert not (outside / "w.txt").exists()
        assert box.confined is True
    finally:
        shutil.rmtree(outside, ignore_errors=True)
