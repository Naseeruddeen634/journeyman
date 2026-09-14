"""Getting the review into a team's workflow.

A reviewer that only runs on one laptop reviews one person's code. The way a
team adopts it is CI, with findings showing up on the pull request diff next
to the line that caused them.

  text     for a terminal
  json     for scripts
  github   workflow commands, which GitHub renders as inline annotations
  sarif    SARIF 2.1.0, for GitHub code scanning and most other tools
"""

from __future__ import annotations

import json
from pathlib import Path

SEVERITY_LEVEL = [(80, "error"), (60, "warning"), (0, "note")]


def level(severity: int) -> str:
    return next(name for floor, name in SEVERITY_LEVEL if severity >= floor)


def _split_where(where: str) -> tuple[str, int]:
    path, _, line = where.rpartition(":")
    if path and line.isdigit():
        return path, int(line)
    return where, 1


def _escape_data(s: str) -> str:
    # GitHub workflow command escaping for the message part
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_prop(s: str) -> str:
    return _escape_data(s).replace(":", "%3A").replace(",", "%2C")


def to_json(findings) -> str:
    return json.dumps([f.to_dict() for f in findings], indent=2)


def to_github(findings) -> str:
    """::error file=...,line=...,title=...::message, one per finding."""
    lines = []
    for f in findings:
        path, line = _split_where(f.where)
        kind = {"error": "error", "warning": "warning", "note": "notice"}[level(f.severity)]
        title = _escape_prop(f"{f.code}: {f.title}")
        body = _escape_data(f"{f.why} Fix: {f.fix}")
        lines.append(f"::{kind} file={_escape_prop(path)},line={line},title={title}::{body}")
    return "\n".join(lines)


def to_sarif(findings, tool_version: str = "1.0.0") -> str:
    rules: dict[str, dict] = {}
    results = []
    for f in findings:
        rules.setdefault(f.code, {
            "id": f.code,
            "name": f.code,
            "shortDescription": {"text": f.title},
            "fullDescription": {"text": f.why},
            "help": {"text": f.fix},
            "defaultConfiguration": {"level": level(f.severity)},
        })
        path, line = _split_where(f.where)
        results.append({
            "ruleId": f.code,
            "level": level(f.severity),
            "message": {"text": f"{f.title}. {f.why} Fix: {f.fix}"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": path.replace("\\", "/")},
                "region": {"startLine": line},
            }}],
        })
    return json.dumps({
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "Journeyman", "version": tool_version,
                                "informationUri": "https://github.com/Naseeruddeen634/journeyman",
                                "rules": list(rules.values())}},
            "results": results,
        }],
    }, indent=2)


WORKFLOW = """name: journeyman

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read
  security-events: write   # for uploading SARIF to code scanning

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install Journeyman
        run: pip install {install}
      - name: AI engineering review (inline annotations)
        run: journeyman review --format github --fail-on {fail_on}
      - name: AI engineering review (code scanning)
        if: always()
        run: journeyman review --format sarif > journeyman.sarif
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: journeyman.sarif
          category: journeyman
{evals}"""

EVAL_STEP = """      - name: Prompt evals (replayed, no model calls)
        run: |
          pip install pytest
          pytest {tests}
"""


# Never a bare package name. `pip install journeyman` would install whatever
# happens to own that name on PyPI, which is not this project and could be
# anyone's. A wrong URL fails the build loudly; a squatted name does not.
DEFAULT_INSTALL = "git+https://github.com/Naseeruddeen634/journeyman"


def init_workflow(repo: Path, install: str = DEFAULT_INSTALL, fail_on: int = 80,
                  overwrite: bool = False) -> tuple[Path, bool]:
    """Write .github/workflows/journeyman.yml. Returns (path, written)."""
    if "/" not in install and "\\" not in install and not install.startswith(("git+", ".")):
        raise ValueError(
            f"refusing to write 'pip install {install}': a bare name installs whatever owns it "
            "on PyPI. Use a git URL or a path.")
    target = repo / ".github" / "workflows" / "journeyman.yml"
    if target.exists() and not overwrite:
        return target, False
    evals = sorted(str(p.relative_to(repo)) for p in (repo / "tests").glob("test_*_eval.py")) \
        if (repo / "tests").exists() else []
    text = WORKFLOW.format(
        install=install, fail_on=fail_on,
        evals=EVAL_STEP.format(tests=" ".join(evals)) if evals else "")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf8")
    return target, True
