"""CI output formats and the workflow generator."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman import ci  # noqa: E402
from journeyman.patterns.smells import Finding, review  # noqa: E402


def _finding(**kw):
    base = dict(code="AIE004", title="user input: goes, into a prompt", why="line one\nline two 100%",
                fix="wrap it", where="app/support.py:11", severity=80)
    base.update(kw)
    return Finding(**base)


def test_github_annotations_escape_what_would_break_the_command():
    out = ci.to_github([_finding()])
    assert out.startswith("::error file=app/support.py,line=11,title=AIE004%3A user input%3A goes%2C into a prompt::")
    assert "\n" not in out and "%0A" in out and "100%25" in out


@pytest.mark.parametrize("sev,kind", [(85, "::error "), (65, "::warning "), (40, "::notice ")])
def test_severity_maps_to_annotation_level(sev, kind):
    assert ci.to_github([_finding(severity=sev)]).startswith(kind)


def test_sarif_is_structurally_valid_and_deduplicates_rules():
    data = json.loads(ci.to_sarif([_finding(), _finding(where="app/other.py:3"),
                                   _finding(code="AIE002", severity=60, where="x.py")]))
    run = data["runs"][0]
    assert data["version"] == "2.1.0"
    assert sorted(r["id"] for r in run["tool"]["driver"]["rules"]) == ["AIE002", "AIE004"]
    assert len(run["results"]) == 3
    loc = run["results"][0]["locations"][0]["physicalLocation"]
    assert loc == {"artifactLocation": {"uri": "app/support.py"}, "region": {"startLine": 11}}
    assert run["results"][2]["locations"][0]["physicalLocation"]["region"]["startLine"] == 1


def test_real_findings_render_in_every_format():
    findings = review(ROOT / "bench/cases/aie004_injection/repo")
    assert findings
    assert json.loads(ci.to_json(findings))[0]["code"] == "AIE004"
    assert ci.to_github(findings).startswith("::error file=lib/support.py")
    assert json.loads(ci.to_sarif(findings))["runs"][0]["results"]


def test_workflow_generator_includes_replayed_evals_and_does_not_overwrite(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_triage_prompt_eval.py").write_text("")
    path, written = ci.init_workflow(tmp_path, fail_on=70)
    text = path.read_text()
    assert written and "--fail-on 70" in text
    assert "pip install git+https://" in text
    assert "pytest tests/test_triage_prompt_eval.py" in text
    assert "upload-sarif" in text
    assert ci.init_workflow(tmp_path)[1] is False


def test_workflow_generator_refuses_a_bare_package_name(tmp_path):
    """`pip install journeyman` installs whatever owns that name on PyPI."""
    with pytest.raises(ValueError, match="bare name"):
        ci.init_workflow(tmp_path, install="journeyman")
    assert not (tmp_path / ".github").exists()


def test_review_exit_code_follows_fail_on(tmp_path, capsys):
    from journeyman.cli import main

    repo = ROOT / "bench/cases/aie001_model_id/repo"      # a single severity-60 finding
    assert main(["review", "--repo", str(repo), "--format", "json"]) == 0
    assert main(["review", "--repo", str(repo), "--format", "json", "--fail-on", "60"]) == 1
