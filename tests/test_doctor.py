"""journeyman doctor: each check simulates a failure that really happened."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman import doctor  # noqa: E402


def _by_name(checks, name):
    return next(c for c in checks if c.name == name)


@pytest.fixture
def mac(monkeypatch, tmp_path):
    import journeyman.service as svc

    plist = tmp_path / "agent.plist"
    plist.write_text("x")
    monkeypatch.setattr(svc, "PLIST", plist)
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(svc, "app_freshness", lambda: {"installed": False})
    return svc


def test_the_agent_that_was_registered_and_never_ran(mac, monkeypatch):
    monkeypatch.setattr(mac, "launchd_state", lambda label=mac.LABEL: {
        "loaded": True, "runs": 0, "last_exit": "(never exited)",
        "program": "/Users/x/Downloads/untitled", "program_exists": False})
    c = doctor._scheduler()[0]
    assert c.status == "fail" and "does not exist" in c.detail


def test_the_agent_macos_would_not_let_run(mac, monkeypatch):
    monkeypatch.setattr(mac, "launchd_state", lambda label=mac.LABEL: {
        "loaded": True, "runs": 3, "last_exit": "126", "program": "/bin/sh", "program_exists": True})
    c = doctor._scheduler()[0]
    assert c.status == "fail" and "denied by macOS" in c.detail and "Full Disk Access" in c.fix


def test_a_program_inside_a_protected_folder_is_called_out(mac, monkeypatch):
    prog = str(Path.home() / "Downloads" / "app" / "journeyman")
    monkeypatch.setattr(mac, "launchd_state", lambda label=mac.LABEL: {
        "loaded": True, "runs": 1, "last_exit": "0", "program": prog, "program_exists": True})
    names = {c.name: c for c in doctor._scheduler()}
    assert names["scheduler: program"].status == "fail"


def test_a_stale_app_copy_warns(mac, monkeypatch):
    monkeypatch.setattr(mac, "launchd_state", lambda label=mac.LABEL: {
        "loaded": True, "runs": 5, "last_exit": "0", "program": "/opt/j", "program_exists": True})
    monkeypatch.setattr(mac, "app_freshness",
                        lambda: {"installed": True, "stale": True, "built": "yesterday"})
    names = {c.name: c for c in doctor._scheduler()}
    assert names["scheduler"].status == "ok"
    assert names["scheduler: app copy"].status == "warn"


def test_a_tiny_context_window_warns(monkeypatch):
    import journeyman.brain.models as models

    class FakeClient:
        def __init__(self, host):
            pass

        def list(self):
            return {"models": [{"model": models.LOCAL_MODEL}]}

    monkeypatch.setitem(sys.modules, "ollama", type(sys)("ollama"))
    sys.modules["ollama"].Client = FakeClient
    monkeypatch.setattr(models, "LOCAL_CTX", 4096)
    c = doctor._ollama_context()
    assert c.status == "warn" and "4096" in c.detail


def test_a_red_repo_warns_but_reassures(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("journeyman.autonomy.shift.failing_set",
                        lambda root: ({"tests/test_a.py::test_x"}, ""))
    c = doctor._repo(tmp_path)[0]
    assert c.status == "warn" and "gate on new failures" in c.fix


def test_not_a_git_repo_fails(tmp_path):
    assert doctor._repo(tmp_path)[0].status == "fail"


def test_a_crashing_check_is_reported_not_raised(monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor, "_tools", boom)
    monkeypatch.setattr(doctor, "_ollama_context", lambda: doctor.Check("local model", "ok", "x"))
    monkeypatch.setattr(doctor, "_other_brains", lambda: [])
    monkeypatch.setattr(doctor, "_scheduler", lambda: [])
    checks = doctor.run()
    assert any(c.status == "fail" and "kaboom" in c.detail for c in checks)
    assert "1 failing" in doctor.render(checks)


def test_confinement_is_probed_not_assumed():
    from journeyman.autonomy import jail

    c = doctor._confinement()
    if jail.available():
        assert c.status == "ok", c.detail
    else:
        assert c.status == "warn"


def test_a_sandbox_that_stopped_enforcing_is_a_failure(monkeypatch, tmp_path):
    """Apple deprecated sandbox-exec. If it ever silently stops enforcing, say so."""
    from journeyman.autonomy import jail

    monkeypatch.setattr(jail, "available", lambda: True)
    monkeypatch.setattr(jail, "wrap", lambda argv, work, env: (argv, {"PATH": "/bin:/usr/bin"}, True))
    c = doctor._confinement()
    assert c.status == "fail" and "did NOT block" in c.detail
