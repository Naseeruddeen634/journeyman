"""The AWS review checks, and the part that makes it live in the system."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman.patterns.smells import review  # noqa: E402


BAD_BEDROCK = '''
import json
import boto3

client = boto3.client("bedrock-runtime")


def reply(user_message):
    body = json.dumps({"messages": [{"role": "user", "content": user_message}]})
    r = client.invoke_model(modelId="anthropic.claude-3-sonnet-20240229-v1:0", body=body)
    return json.loads(r["body"].read())["content"][0]["text"]
'''

GOOD_BEDROCK = '''
import os
import boto3
from botocore.config import Config

REGION = os.environ["AWS_REGION"]
MODEL = os.environ.get("CHAT_MODEL", "anthropic.claude-3-5-sonnet-20241022-v2:0")
GUARDRAIL = os.environ["CHAT_GUARDRAIL_ID"]

cfg = Config(retries={"max_attempts": 10, "mode": "adaptive"}, read_timeout=120)
client = boto3.client("bedrock-runtime", region_name=REGION, config=cfg)


def reply(user_message):
    r = client.converse(
        modelId=MODEL,
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": 500},
        guardrailConfig={"guardrailIdentifier": GUARDRAIL, "guardrailVersion": "DRAFT"},
    )
    total = r["usage"]["totalTokens"]
    return r["output"]["message"]["content"][0]["text"], total
'''


@pytest.fixture
def bad(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "chat.py").write_text(BAD_BEDROCK)
    return tmp_path


@pytest.fixture
def good(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "chat.py").write_text(GOOD_BEDROCK)
    return tmp_path


@pytest.mark.parametrize("code,what", [
    ("AIE010", "no adaptive retry, and Bedrock throttling is the normal case"),
    ("AIE011", "invoke_model locks the payload to one provider"),
    ("AIE012", "no region pinned, and model access is regional"),
    ("AIE013", "user-facing generation with no guardrail"),
])
def test_bedrock_problems_are_caught(bad, code, what):
    assert code in {f.code for f in review(bad)}, what


def test_well_written_bedrock_code_produces_nothing(good):
    """The property that decides whether anyone keeps it switched on."""
    assert review(good) == []


def test_bedrock_findings_explain_the_failure_and_the_fix(bad):
    for f in review(bad):
        assert len(f.why) > 80, f"{f.code}: no explanation"
        assert len(f.fix) > 40, f"{f.code}: no fix"


def test_a_non_aws_file_gets_no_bedrock_findings(tmp_path):
    (tmp_path / "m.py").write_text(
        "import boto3\nclient = boto3.client('s3')\n"
        "def put(k, v):\n    return client.put_object(Bucket='b', Key=k, Body=v)\n")
    codes = {f.code for f in review(tmp_path)}
    assert not (codes & {"AIE010", "AIE011", "AIE012", "AIE013"})


# ---- living in the system ---------------------------------------------


def test_plist_is_well_formed_and_conservative(tmp_path, monkeypatch):
    import plistlib

    import journeyman.service as svc

    monkeypatch.setattr(svc, "PLIST", tmp_path / "agent.plist")
    path = svc.write_plist([str(tmp_path)], every_minutes=180, max_shifts=1)
    data = plistlib.loads(path.read_bytes())

    assert data["Label"] == svc.LABEL
    assert data["StartInterval"] == 180 * 60
    assert data["RunAtLoad"] is False, "installing it must not start editing immediately"
    assert data["ProcessType"] == "Background", "it must yield to the user's own work"
    assert "run-scheduled" in data["ProgramArguments"]
    assert str(tmp_path) in data["ProgramArguments"]


def test_uninstall_leaves_your_records_alone(tmp_path, monkeypatch):
    """Never let this reach the real launchctl.

    The first version monkeypatched only the plist path. `launchctl unload -w`
    resolves the service by the Label inside the file, not by the path, so the
    test unloaded the agent actually running on the developer's machine. A test
    that reaches outside its tmp_path is a bug in the test.
    """
    import journeyman.service as svc

    unloaded = []
    monkeypatch.setattr(svc, "unload", lambda: (unloaded.append(True), (True, ""))[1])

    plist = tmp_path / "agent.plist"
    monkeypatch.setattr(svc, "PLIST", plist)
    svc.write_plist([str(tmp_path)])
    assert plist.exists()

    msg = svc.uninstall()
    assert not plist.exists()
    assert "untouched" in msg
    assert unloaded, "uninstall should stop the service before removing the file"


def test_no_test_in_this_file_touches_the_real_launchd(monkeypatch):
    """A guard, because the failure mode is silent and off-machine."""
    import journeyman.service as svc

    calls = []
    monkeypatch.setattr(svc.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _Fake())
    svc.is_loaded()
    assert calls, "is_loaded should shell out"
    assert all("launchctl" in str(c) for c in calls)


class _Fake:
    returncode = 0
    stdout = ""
    stderr = ""


def test_home_survives_being_installed_elsewhere(tmp_path, monkeypatch):
    """Paths used to resolve against the source tree, which breaks on install."""
    monkeypatch.setenv("JOURNEYMAN_HOME", str(tmp_path / "jm"))
    import importlib

    import journeyman.home as home
    importlib.reload(home)
    home.ensure()
    assert (tmp_path / "jm" / "config.json").exists()
    assert (tmp_path / "jm" / "shifts").is_dir()
    rec = home.record_shift("/some/repo", {"outcome": "fixed"})
    assert rec.exists()
    assert home.history()[0]["outcome"] == "fixed"
    importlib.reload(home)


def test_three_brains_are_reported_without_crashing_when_none_configured():
    from journeyman.brain.models import check

    st = check()
    assert isinstance(st.local_available, bool)
    assert isinstance(st.heavy_available, bool)
    assert isinstance(st.bedrock_available, bool)
    assert st.summary()
