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
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
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
        with:
          fetch-depth: 0     # the base branch is needed to tell new findings from old ones
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install Journeyman
        run: pip install {install}
      # On a pull request only findings the PR introduced can fail it, so installing
      # this on an existing codebase does not turn every PR red on day one.
      - name: AI engineering review (inline annotations)
        run: >-
          journeyman review --format github --fail-on {fail_on}
          ${{{{ github.event_name == 'pull_request' && format('--new-since origin/{{0}}', github.base_ref) || '' }}}}
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


# ------------------------------------------------------------ only what the change introduced


class RefError(ValueError):
    pass


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _added_lines(diff: str) -> dict[str, set[int]]:
    """path -> line numbers added or modified, from `git diff -U0`."""
    out: dict[str, set[int]] = defaultdict(set)
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and path:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                out[path].update(range(start, start + count))
    return out


def new_findings(repo: Path, ref: str, review=None):
    """(findings the change since `ref` introduced, how many were already there).

    Installing a reviewer on an existing codebase must not turn every pull request
    red for problems that predate it; that is how a check gets switched off in its
    first week. So the merge-base with `ref` is reviewed in a temporary worktree
    and a finding counts as new only if the base did not already have it.

    Findings are matched on (code, file, title), not line numbers, so code that
    merely moved is not new. File renames are followed. When a file gains a
    second copy of a finding it already had, the copy on a changed line is the
    one reported. Comparing whole reviews rather than filtering to changed lines
    also catches a finding that appears on an untouched line, such as a prompt
    that loses its eval because the PR deleted the test.
    """
    if review is None:
        from .patterns.smells import review
    repo = Path(repo).resolve()
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RefError(f"{repo} is not inside a git repository, so there is nothing to compare with")
    top_path = Path(top.stdout.strip()).resolve()
    prefix = str(repo.relative_to(top_path)) if repo != top_path else ""

    base = _git(repo, "merge-base", ref, "HEAD")
    if base.returncode != 0:
        raise RefError(f"cannot find a merge-base with {ref!r}. In CI, check out with "
                       "fetch-depth: 0 so the base branch is present.")
    sha = base.stdout.strip()

    def rel(path: str) -> str | None:
        if not prefix:
            return path
        return path[len(prefix) + 1:] if path.startswith(prefix + "/") else None

    renamed: dict[str, str] = {}
    names = _git(repo, "diff", "--name-status", "-M", sha, "--", ".")
    for line in names.stdout.splitlines():
        parts = line.split("\t")
        if parts[0].startswith("R") and len(parts) == 3:
            old, new = rel(parts[1]), rel(parts[2])
            if old and new:
                renamed[new] = old
    changed = {rel(p): lines for p, lines in _added_lines(_git(repo, "diff", "-U0", sha, "--", ".").stdout).items()
               if rel(p)}

    head = review(repo)
    work = Path(tempfile.mkdtemp(prefix="journeyman-base-"))
    tree = work / "base"
    try:
        added = _git(repo, "worktree", "add", "--detach", str(tree), sha)
        if added.returncode != 0:
            raise RefError(f"could not check out {sha[:12]} to compare with: {added.stderr.strip()[:200]}")
        base_findings = review(tree / prefix if prefix else tree)
    finally:
        _git(repo, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(work, ignore_errors=True)
        _git(repo, "worktree", "prune")

    def fingerprint(f, mapping: dict[str, str]):
        path, _ = _split_where(f.where)
        old = mapping.get(path, path)
        return f.code, old, f.title.replace(path, old) if old != path else f.title

    seen = Counter(fingerprint(f, {}) for f in base_findings)
    groups: dict[tuple, list] = defaultdict(list)
    for f in head:
        groups[fingerprint(f, renamed)].append(f)
    new = []
    for fp, fs in groups.items():
        extra = len(fs) - seen.get(fp, 0)
        if extra <= 0:
            continue
        on_changed = sorted(fs, key=lambda f: _split_where(f.where)[1] not in changed.get(_split_where(f.where)[0], ()))
        new.extend(on_changed[:extra])
    new.sort(key=lambda f: -f.severity)
    return new, len(head) - len(new)


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
