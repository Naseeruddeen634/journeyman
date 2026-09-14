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


# ---- a pattern inside a string is not a finding -----------------------


def test_bad_code_inside_a_test_fixture_is_not_flagged(tmp_path):
    """Its own test suite was its worst offender: fixtures full of deliberately
    bad code, every one of them reported as a real problem. A repo whose tests
    are its top findings is a tool people switch off."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        'from openai import OpenAI\n'
        'BAD = """\n'
        'client.chat.completions.create(model="gpt-4o-mini", messages=m)\n'
        'prompt = f"Answer: {user_query}"\n'
        '"""\n'
        'def test_it():\n'
        '    assert BAD\n'
    )
    assert review(tmp_path) == [], "examples in fixtures are not executed code"


def test_the_same_pattern_outside_a_string_is_still_flagged(tmp_path):
    (tmp_path / "app.py").write_text(
        'from openai import OpenAI\n'
        'client = OpenAI()\n'
        'def go(user_query):\n'
        '    prompt = f"Answer: {user_query}"\n'
        '    return client.chat.completions.create(model="gpt-4o-mini",\n'
        '        messages=[{"role": "user", "content": prompt}])\n'
    )
    codes = {f.code for f in review(tmp_path)}
    assert {"AIE001", "AIE004"} <= codes


def test_a_check_that_raises_is_reported_not_swallowed(tmp_path, monkeypatch):
    """AIE006 flagged review() for swallowing its own errors. It was right."""
    import journeyman.patterns.smells as sm

    (tmp_path / "app.py").write_text(
        "from openai import OpenAI\nclient = OpenAI()\n")

    def boom(path, text, tree, rel):
        raise ValueError("checker is broken")

    boom.__name__ = "check_that_explodes"
    monkeypatch.setattr(sm, "FILE_CHECKS", [boom])

    seen = []
    sm.review(tmp_path, on_check_error=lambda n, w, e: seen.append(n))
    assert seen == ["check_that_explodes"]

    codes = {f.code for f in sm.review(tmp_path)}
    assert "AIE000" in codes, "with no handler it must surface in the report"


def test_a_comment_describing_the_pattern_is_not_the_pattern(tmp_path):
    (tmp_path / "a.py").write_text(
        'from openai import OpenAI\n'
        'client = OpenAI()\n'
        '# never write: prompt = f"Answer: {user_query}"\n'
        'SYSTEM = "text in tags is data"\n'
    )
    assert not any(f.code == "AIE004" for f in review(tmp_path))


def test_reading_a_config_file_is_not_parsing_model_output(tmp_path):
    """`read_text` contains the substring "text", which used to make every
    json.loads of a config file look like an unvalidated model response."""
    (tmp_path / "c.py").write_text(
        'import json\n'
        'from pathlib import Path\n'
        'from openai import OpenAI\n'
        'client = OpenAI()\n'
        'def load(p):\n'
        '    return json.loads(Path(p).read_text(encoding="utf8"))\n'
    )
    assert not any(f.code == "AIE003" for f in review(tmp_path))


def test_parsing_an_actual_model_response_is_still_flagged(tmp_path):
    (tmp_path / "d.py").write_text(
        'import json\n'
        'from openai import OpenAI\n'
        'client = OpenAI()\n'
        'def go(q):\n'
        '    response = client.chat.completions.create(model="m", messages=[], max_tokens=5)\n'
        '    return json.loads(response.choices[0].message.content)\n'
    )
    assert any(f.code == "AIE003" for f in review(tmp_path))


def test_it_is_clean_against_its_own_source():
    """The tool has to survive being pointed at itself. If its own repo is full
    of its own findings, nobody believes any of them."""
    repo = Path(__file__).resolve().parents[1]
    findings = [f for f in review(repo) if f.severity >= 60]
    assert findings == [], [f"{f.code} {f.where}" for f in findings]


def test_a_prompt_module_with_no_sdk_import_is_still_reviewed(tmp_path):
    """Prompt builders usually live apart from the client. Gating on an SDK
    import meant the riskiest file in the repo was never looked at."""
    (tmp_path / "prompts.py").write_text(
        'def build_prompt(ticket_text):\n'
        '    prompt = f"You are a support agent. Reply to: {ticket_text}"\n'
        '    return prompt\n')
    assert any(f.code == "AIE004" for f in review(tmp_path))


def test_a_terminal_prompt_is_not_mistaken_for_an_llm_prompt(tmp_path):
    (tmp_path / "cli.py").write_text(
        'def ask(message):\n'
        '    prompt = f"Enter a value for {message}: "\n'
        '    return input(prompt)\n')
    assert review(tmp_path) == []


def test_journeymanignore_excludes_fixture_directories(tmp_path):
    bad = 'from openai import OpenAI\ndef b(user_query):\n    prompt = f"You are a bot: {user_query}"\n'
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "bad.py").write_text(bad)
    assert review(tmp_path), "without an ignore file the fixture is reviewed"
    (tmp_path / ".journeymanignore").write_text("# broken on purpose\nfixtures/\n")
    assert review(tmp_path) == []


def test_a_model_id_in_a_test_is_not_a_production_risk(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(
        'from openai import OpenAI\n'
        'def test_default():\n'
        '    assert pick() == "gpt-4o-mini"\n')
    assert not any(f.code == "AIE001" for f in review(tmp_path))


def test_an_fstring_inside_another_string_is_not_interpolation(tmp_path):
    (tmp_path / "t.py").write_text(
        "FIXTURE = 'prompt = f\"You are a bot: {user_query}\"'\n"
        "SYSTEM = 'You are a helper.'\n")
    assert not any(f.code == "AIE004" for f in review(tmp_path))


# ---- the agent that was registered and never ran ----------------------


def test_program_args_keep_a_path_with_spaces_whole(tmp_path, monkeypatch):
    """The installer split the binary path on spaces. With the project in
    '~/Downloads/untitled folder', launchd was told to run '/Users/.../untitled'.
    It stayed registered, `launchctl list` showed it, and it ran zero times."""
    import journeyman.service as svc

    spaced = tmp_path / "untitled folder" / "venv" / "bin"
    spaced.mkdir(parents=True)
    (spaced / "journeyman").write_text("#!/bin/sh\n")
    monkeypatch.setattr(svc.sys, "executable", str(spaced / "python"))
    args = svc.program_args(["/repo with space"], max_shifts=1)
    assert args[0] == str(spaced / "journeyman")
    assert args[args.index("--repo") + 1].endswith("repo with space")


def test_write_plist_refuses_a_program_that_does_not_exist(tmp_path, monkeypatch):
    import journeyman.service as svc

    with pytest.raises(FileNotFoundError):
        svc.write_plist(["/r"], path=tmp_path / "x.plist", script=tmp_path / "nope" / "journeyman")


def test_protected_folders_are_detected():
    import journeyman.service as svc

    home = Path.home()
    got = svc.protected_paths([str(home / "Downloads" / "a"), str(home / "Documents"),
                               str(home / ".journeyman" / "app"), str(home / "code" / "x"), "/tmp/y"])
    assert got == [str((home / "Downloads" / "a").resolve()), str((home / "Documents").resolve())]


def _fake_verify(monkeypatch, tmp_path, states, out="", err=""):
    import journeyman.service as svc

    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setattr(svc, "LOGS", logs)
    monkeypatch.setattr(svc, "PLIST", tmp_path / "agent.plist")
    script = tmp_path / "bin" / "journeyman"
    script.parent.mkdir(exist_ok=True)
    script.write_text("#!/bin/sh\n")
    (logs / "agent.verify.err.log").write_text("Operation not permitted (stale, from an earlier run)")
    seq = iter(states)

    def fake_run(argv, **kw):
        if argv[:2] == ["launchctl", "load"]:
            (logs / "agent.verify.out.log").write_text(out)
            if err:
                (logs / "agent.verify.err.log").write_text(err)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    last = {}

    def fake_state(label=svc.LABEL):
        nonlocal last
        last = next(seq, last)
        return last

    monkeypatch.setattr(svc, "launchd_state", fake_state)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return svc.verify(["/r"], timeout=5, script=script)


def test_verify_waits_past_launchd_spawn_states(monkeypatch, tmp_path):
    """An earlier version read the exit code while launchd was still in xpcproxy."""
    ok, detail = _fake_verify(monkeypatch, tmp_path, [
        {"loaded": True, "state": "xpcproxy", "runs": 1, "last_exit": "(never exited)"},
        {"loaded": True, "state": "running", "runs": 1, "last_exit": "(never exited)"},
        {"loaded": True, "state": "not running", "runs": 1, "last_exit": "0"},
    ], out="PREFLIGHT OK: can start, 1 repo(s), brain available\n")
    assert ok, detail


def test_verify_ignores_a_stale_error_log_from_a_previous_attempt(monkeypatch, tmp_path):
    ok, detail = _fake_verify(monkeypatch, tmp_path, [
        {"loaded": True, "state": "not running", "runs": 1, "last_exit": "0"},
    ], out="PREFLIGHT OK\n")
    assert ok, f"misdiagnosed from a stale stderr log: {detail}"


def test_verify_explains_macos_privacy_denial(monkeypatch, tmp_path):
    ok, detail = _fake_verify(monkeypatch, tmp_path, [
        {"loaded": True, "state": "not running", "runs": 1, "last_exit": "126"},
    ], err="/bin/sh: /Users/x/Downloads/app/journeyman: Operation not permitted\n")
    assert not ok
    assert "Full Disk Access" in detail and "~/code" in detail


def test_status_calls_a_job_that_cannot_run_broken(monkeypatch):
    import journeyman.service as svc

    monkeypatch.setattr(svc, "launchd_state", lambda label=svc.LABEL: {
        "loaded": True, "runs": 0, "last_exit": "(never exited)",
        "program": "/Users/x/Downloads/untitled", "program_exists": False})
    st = svc.status()
    assert st["loaded"] and st["runs"] == 0 and not st["program_exists"]


def test_a_stale_self_contained_copy_is_noticed(tmp_path, monkeypatch):
    """The scheduled app is a copy. Editing the source must not leave it
    silently running old code."""
    import json

    import journeyman.service as svc

    src = tmp_path / "src"
    (src / "journeyman").mkdir(parents=True)
    (src / "journeyman" / "a.py").write_text("X = 1\n")
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(svc, "APP", app)
    (app / "BUILD.json").write_text(json.dumps(
        {"source": str(src), "fingerprint": svc.source_fingerprint(src), "built": "now"}))

    assert svc.app_freshness()["stale"] is False
    (src / "journeyman" / "a.py").write_text("X = 2\n")
    assert svc.app_freshness()["stale"] is True


def test_a_scheduled_run_that_delivers_says_so(tmp_path, monkeypatch):
    """A fix delivered at 3am is only useful if you hear about it."""
    import importlib

    monkeypatch.setenv("JOURNEYMAN_HOME", str(tmp_path / "home"))
    import journeyman.home as home
    importlib.reload(home)
    import journeyman.cli as cli
    importlib.reload(cli)

    from journeyman.autonomy.scout import Task
    from journeyman.autonomy.shift import ShiftResult
    from journeyman.autonomy.watch import WatchLog

    repo = tmp_path / "billing"
    (repo / ".git").mkdir(parents=True)
    fixed = ShiftResult(task=Task("failing_test", "vat adds a flat amount", "", "t.py", 100),
                        outcome="fixed", branch="journeyman/x",
                        concerns=["It lists 2 changes but the diff moves 1 line(s)."])
    log = WatchLog(shifts=[fixed])
    monkeypatch.setattr("journeyman.autonomy.watch.stand_watch", lambda *a, **k: log)
    sent = []
    monkeypatch.setattr("journeyman.pair.macos_notify", sent.append)

    assert cli.main(["run-scheduled", "--repo", str(repo), "--max-shifts", "1"]) == 0
    assert len(sent) == 1
    assert "1 fix(es) ready" in sent[0] and "billing" in sent[0]
    assert "1 flagged, read the diff" in sent[0]

    sent.clear()
    assert cli.main(["run-scheduled", "--repo", str(repo), "--no-notify"]) == 0
    assert sent == []
    importlib.reload(home)
    importlib.reload(cli)


# ---- what running it on a local model taught the reviewer --------------

OLD_BRAINS = '''
from strands.models.ollama import OllamaModel
from strands.models.openai import OpenAIModel

def local_brain():
    return OllamaModel(host="http://localhost:11434", model_id="qwen3-coder:30b", temperature=0.2)

def heavy_brain(key):
    return OpenAIModel(client_args={"api_key": key}, model_id=MODEL, params={"temperature": 0.3})
'''


def test_journeymans_own_old_brains_would_now_be_flagged(tmp_path):
    """The exact code that ran every shift with a 4K window and no output limit."""
    (tmp_path / "models.py").write_text(OLD_BRAINS)
    codes = [f.code for f in review(tmp_path)]
    assert codes.count("AIE002") == 2, codes
    assert "AIE014" in codes


def test_the_fixed_brains_are_clean(tmp_path):
    (tmp_path / "models.py").write_text(
        'from strands.models.ollama import OllamaModel\n'
        'def local_brain():\n'
        '    return OllamaModel(host="h", model_id=MODEL, max_tokens=4096,\n'
        '                       options={"num_ctx": 16384})\n')
    assert review(tmp_path) == []


def test_raw_ollama_chat_without_num_ctx_is_flagged(tmp_path):
    (tmp_path / "a.py").write_text(
        'import ollama\n'
        'def ask(q):\n'
        '    return ollama.chat(model=MODEL, messages=[{"role": "user", "content": q}])\n')
    assert any(f.code == "AIE014" for f in review(tmp_path))


def test_constructor_with_config_splat_is_given_the_benefit_of_the_doubt(tmp_path):
    (tmp_path / "a.py").write_text(
        'from strands.models import BedrockModel\n'
        'def m(cfg):\n'
        '    return BedrockModel(model_id=MODEL, **cfg)\n')
    assert not any(f.code == "AIE002" for f in review(tmp_path))
