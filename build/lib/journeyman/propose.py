"""From a branch to a pull request a colleague can actually review.

A branch is not a deliverable. The work reaches your team as a pull request,
and a pull request from an agent is only worth reading if it says what was
checked, what was not, and what the agent itself was unsure about. This writes
that description from the shift's own record, so none of it is recollection.

It does not push and it does not open the PR. It prints the two commands and
you run them. The line between an agent that proposes and an agent that ships
is the whole reason anyone lets it run overnight.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .home import SHIFTS, ensure

DELIVERED = ("fixed", "fixed_with_concerns")


def find_record(repo: Path, branch: str | None = None) -> dict | None:
    """The shift record for a branch, or the newest delivered one for this repo."""
    ensure()
    repo = repo.resolve()
    for p in sorted(SHIFTS.glob("*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf8"))
        except (json.JSONDecodeError, OSError):
            continue
        if Path(d.get("repo", "")).resolve() != repo:
            continue
        if branch and d.get("branch") != branch:
            continue
        if not branch and d.get("outcome") not in DELIVERED:
            continue
        d["_record"] = str(p)
        return d
    return None


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return r.stdout.strip()


def base_branch(repo: Path) -> str:
    for name in ("main", "master", "develop"):
        if _git(repo, "rev-parse", "--verify", "--quiet", name):
            return name
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "main"


def body(record: dict, stat: str) -> str:
    task = record.get("task") or {}
    out = [f"## What", "", f"{task.get('title', 'Unattended fix')}", "",
           f"Found at `{task.get('where', '?')}` ({task.get('kind', '?').replace('_', ' ')}).", ""]

    out += ["## Why this is believed to be right", ""]
    before = record.get("failures_before") or []
    after = record.get("failures_after") or []
    fixed = record.get("fixed_failures") or []
    broke = record.get("new_failures") or []
    if before or after:
        out.append(f"- Test suite: {len(before)} failing before, {len(after)} failing after.")
        if fixed:
            out.append(f"- Now passing: {', '.join(f'`{t}`' for t in fixed[:5])}")
        out.append(f"- Newly failing: {', '.join(broke) if broke else 'none'}")
    else:
        out.append("- Test suite: green before and after.")
    if record.get("finding_resolved") is not None:
        code = (task.get("meta") or {}).get("code", "the finding")
        out.append(f"- Review check {code}: "
                   f"{'no longer fires on this file' if record['finding_resolved'] else 'STILL FIRES'}.")
    if record.get("findings_introduced"):
        out.append(f"- New review findings introduced: {', '.join(record['findings_introduced'])}")
    if record.get("feedback_rounds"):
        out.append(f"- Claimed done {record['feedback_rounds']} time(s) before it actually was; "
                   "each time it was sent back with what was still wrong.")
    budget = record.get("budget") or {}
    if budget:
        out.append(f"- Took {record.get('minutes', '?')} min, {budget.get('iterations', '?')} model "
                   f"turns, on {record.get('brain', 'an unrecorded model')}.")
    out.append("")

    concerns = record.get("concerns") or []
    if concerns:
        out += ["## Read the diff before merging", "",
                "The agent flagged these about its own change:", ""]
        out += [f"> {c}" for c in concerns] + [""]

    out += ["## What was not checked", "",
            "- Only the repository's own test suite and Journeyman's static review checks were run.",
            "- Behaviour those tests do not cover is unverified. A green suite is necessary, not sufficient.",
            "- No integration, load, or manual testing was done.", ""]

    if stat:
        out += ["## Changes", "", "```", stat, "```", ""]

    out += ["---",
            "*Proposed by Journeyman, working unattended in an isolated worktree. "
            "It did not push this branch or open this pull request.*"]
    return "\n".join(out)


def propose(repo: str | Path, branch: str | None = None) -> dict:
    repo = Path(repo).resolve()
    record = find_record(repo, branch)
    if record is None:
        what = f"branch {branch}" if branch else "a delivered shift"
        return {"ok": False, "reason": f"No shift record found for {what} in {repo}."}
    branch = record.get("branch", "")
    if not branch or not _git(repo, "rev-parse", "--verify", "--quiet", branch):
        return {"ok": False, "reason": f"Branch {branch!r} no longer exists in {repo}."}
    if record.get("outcome") not in DELIVERED:
        return {"ok": False, "reason": f"That shift ended {record.get('outcome')}; nothing to propose."}

    base = base_branch(repo)
    stat = _git(repo, "diff", "--stat", f"{base}...{branch}")
    title = ((record.get("task") or {}).get("title") or "Unattended fix")[:72]
    text = body(record, stat)

    outdir = repo / ".journeyman" / "proposals"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{branch.replace('/', '-')}.md"
    path.write_text(text, encoding="utf8")

    commands = [
        f"git push -u origin {shlex.quote(branch)}",
        f"gh pr create --base {shlex.quote(base)} --head {shlex.quote(branch)} "
        f"--title {shlex.quote(title)} --body-file {shlex.quote(str(path))}",
    ]
    return {"ok": True, "branch": branch, "base": base, "title": title,
            "body_file": str(path), "body": text, "commands": commands,
            "concerns": len(record.get("concerns") or [])}
