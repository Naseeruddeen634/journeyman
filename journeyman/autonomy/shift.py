"""One shift of unattended work.

A shift is: survey the repo, take the most important broken thing, work it in an
isolated worktree until the tests pass or the budget runs out, commit to a
branch, write a report, stop.

It never touches your checkout, never pushes, never merges. You come back to a
branch and a note. That is the whole contract, and it is enforced in
`guardrails.py` rather than requested in a prompt.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from strands import Agent, tool

from ..brain.models import check as brain_check
from ..brain.models import LOCAL_MODEL
from ..brain.models import pick as pick_brain
from .guardrails import Budget, BudgetGuard, Refused, Sandbox, close_sandbox, open_sandbox
from .scout import Task, describe, survey

SYSTEM = """You are an engineer working a night shift on a colleague's repository.

You have one job, stated below. Work it and stop.

How to work:
- Read before you write. Use read_file on the files involved.
- Make the smallest change that fixes the actual cause. Do not refactor
  surrounding code, do not rename things, do not "improve" what you were not
  asked about.
- Never weaken a test to make it pass. If a test is failing because the code is
  wrong, fix the code. If you genuinely believe the test is wrong, stop and say
  so in your final message rather than editing it.
- Run run_tests after each change. The task is done when the suite is green.
- If you cannot fix it, say what you tried and what you think is going on. A
  clear account of a failure is worth more than a guess that breaks something.

You cannot push, merge, install packages, or touch anything outside this
worktree. Do not try; the attempt will be refused and it wastes your budget.

When the tests pass, reply with a one-line summary starting with DONE:.
If you are giving up, reply with a one-line summary starting with STUCK:.
"""


# Hedges an engineer uses when they know the change is not quite right. A green
# test suite is not proof of correctness: a model can satisfy an assertion by
# deleting the behaviour the assertion was guarding. When it says so, that has
# to reach the top of the report rather than the bottom of a log.
DOUBT = (
    "mismatch", "however", "but this", "not ideal", "contradic", "inconsistent",
    "may not be", "might not be", "unclear", "assumption", "i assumed",
    "does not match the docstring", "conflicts with", "arguably", "trade-off",
    "tradeoff", "not sure", "unsure", "questionable",
)


def claims_vs_diff(summary: str, diff: str) -> list[str]:
    """Check what it said it did against what it actually did.

    Observed in a real shift: the agent reported "1. Added delimiters around
    the input. 2. Updated the system prompt to separate instructions from
    data." The diff changed one line and never touched a system prompt.

    Testing that the suite still passes does not catch this, because the claim
    is about the change, not about the outcome. So compare the enumerated
    claims against the size of the diff, and compare any file the summary names
    against the files that actually moved.
    """
    notes: list[str] = []
    if not summary:
        return notes

    added = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    touched = max(len(added), len(removed))

    claims = re.findall(r"^\s*(?:\d+[.)]|[-*])\s+(\S.{10,})$", summary, re.M)
    if len(claims) >= 2 and touched <= 1:
        notes.append(
            f"It lists {len(claims)} changes but the diff moves {touched} line(s). "
            "Check whether everything it claims is actually there."
        )

    named = set(re.findall(r"[\w/]+\.(?:py|txt|md|json|ya?ml)", summary))
    changed_in_diff = set(re.findall(r"^\+\+\+ b/(\S+)", diff, re.M))
    if changed_in_diff:
        missing = {f for f in named if not any(f in c for c in changed_in_diff)}
        if missing:
            notes.append(
                "It mentions " + ", ".join(sorted(missing)[:3])
                + " but the diff does not touch that file."
            )
    return notes


def find_concerns(summary: str) -> list[str]:
    """Sentences where the agent hedged about its own change."""
    out = []
    for raw in re.split(r"(?<=[.!?])\s+|\n", summary or ""):
        line = raw.strip()
        if len(line) < 12:
            continue
        low = line.lower()
        if any(d in low for d in DOUBT):
            out.append(line[:220])
    return out[:4]


@dataclass
class ShiftResult:
    task: Task | None
    branch: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    tests_before: str = ""
    tests_after: str = ""
    green: bool = False
    files_changed: list[str] = field(default_factory=list)
    diff: str = ""
    commit: str = ""
    summary: str = ""
    outcome: str = "no_work"     # fixed | fixed_with_concerns | stuck | regressed | refused | no_work | error
    concerns: list[str] = field(default_factory=list)
    stopped_by: str = ""
    failures_before: list[str] = field(default_factory=list)
    failures_after: list[str] = field(default_factory=list)
    new_failures: list[str] = field(default_factory=list)
    fixed_failures: list[str] = field(default_factory=list)
    brain: str = ""
    log: list[str] = field(default_factory=list)
    budget: dict = field(default_factory=dict)

    @property
    def minutes(self) -> float:
        return round(((self.finished or time.time()) - self.started) / 60, 1)

    def to_dict(self) -> dict:
        return {
            "task": self.task.to_dict() if self.task else None,
            "branch": self.branch, "outcome": self.outcome, "green": self.green,
            "files_changed": self.files_changed, "commit": self.commit,
            "summary": self.summary, "brain": self.brain, "concerns": self.concerns,
            "stopped_by": self.stopped_by,
            "failures_before": self.failures_before, "failures_after": self.failures_after,
            "new_failures": self.new_failures, "fixed_failures": self.fixed_failures,
            "minutes": self.minutes, "budget": self.budget,
            "diff": self.diff[:8000], "log": self.log[-80:],
        }


FAIL_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def failing_set(repo: Path) -> tuple[set[str], str]:
    """Which tests are red right now, as a set of node ids.

    The gate cannot be "the suite is green". Plenty of real repositories are
    not green when you arrive: a test needs a credential, an integration suite
    is skipped in CI, someone left a known failure. Demanding perfection means
    every honest piece of work gets reported as STUCK.

    What matters is whether the agent broke anything that was working.
    """
    from .scout import _interpreter

    try:
        r = subprocess.run(
            [_interpreter(repo), "-m", "pytest", "-q", "--no-header", "--tb=no"],
            cwd=repo, capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return set(), f"could not run the suite: {exc}"
    out = r.stdout + r.stderr
    return set(FAIL_LINE.findall(out)), out[-2500:]


def _tools_for(sandbox: Sandbox, result: ShiftResult):
    """Tools bound to one sandbox. Every path goes through the guardrail."""

    @tool
    def read_file(path: str) -> str:
        """Read a file from the repository.

        Args:
            path: Path relative to the repository root.

        Returns:
            The file contents with line numbers, or an error message.
        """
        try:
            p = sandbox.resolve(path)
            if not p.exists():
                return f"{path} does not exist."
            lines = p.read_text(encoding="utf8", errors="ignore").splitlines()
            return "\n".join(f"{i:5d}  {l}" for i, l in enumerate(lines[:900], 1))
        except Refused as exc:
            return f"Refused: {exc}"
        except OSError as exc:
            return f"Could not read {path}: {exc}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write a file, replacing it entirely.

        Args:
            path: Path relative to the repository root.
            content: The complete new contents of the file.

        Returns:
            Confirmation, or the reason it was refused.
        """
        try:
            sandbox.write(path, content)
            return f"Wrote {path} ({len(content.splitlines())} lines)."
        except Refused as exc:
            result.log.append(f"REFUSED write {path}: {exc}")
            return f"Refused: {exc}"
        except OSError as exc:
            return f"Could not write {path}: {exc}"

    @tool
    def list_files(subdir: str = ".") -> str:
        """List Python files in the repository.

        Args:
            subdir: Directory to list, relative to the repository root.

        Returns:
            A newline-separated list of paths.
        """
        try:
            base = sandbox.resolve(subdir)
        except Refused as exc:
            return f"Refused: {exc}"
        out = []
        for p in sorted(base.rglob("*.py")):
            if any(x in p.parts for x in (".git", ".venv", "__pycache__", ".journeyman")):
                continue
            out.append(str(p.relative_to(sandbox.root)))
        return "\n".join(out[:200]) or "(nothing)"

    @tool
    def run_tests(target: str = "") -> str:
        """Run the test suite and return the result.

        Args:
            target: Optional test file or node id to run instead of everything.

        Returns:
            The tail of pytest's output, including failures.
        """
        from .scout import _interpreter

        cmd = f"{_interpreter(sandbox.root)} -m pytest -q --no-header --tb=short"
        if target:
            cmd += f" {target}"
        try:
            r = sandbox.run(cmd, timeout=300)
        except Refused as exc:
            return f"Refused: {exc}"
        except subprocess.TimeoutExpired:
            return "The test run timed out after 300s."
        out = (r.stdout + r.stderr)[-3500:]
        result.tests_after = out
        return out

    @tool
    def show_diff() -> str:
        """Show what you have changed so far, as a unified diff.

        Returns:
            The current diff against the branch point.
        """
        try:
            r = sandbox.run("git diff", timeout=60)
        except Refused as exc:
            return f"Refused: {exc}"
        return (r.stdout or "(no changes yet)")[:6000]

    return [read_file, write_file, list_files, run_tests, show_diff]


def work_one(repo: str | Path, task: Task | None = None, budget: Budget | None = None,
             difficulty: str = "routine", keep_worktree: bool = True) -> ShiftResult:
    """Do one task, end to end, unattended."""
    repo = Path(repo).resolve()
    budget = budget or Budget()

    if task is None:
        queue = survey(repo)
        task = queue[0] if queue else None
    if task is None:
        return ShiftResult(task=None, outcome="no_work",
                           summary="Nothing to do. Tests pass and the backlog is empty.")

    result = ShiftResult(task=task)
    stamp = time.strftime("%Y%m%d-%H%M")
    slug = "".join(c if c.isalnum() else "-" for c in task.title.lower())[:40].strip("-")
    branch = f"journeyman/{stamp}-{slug or task.kind}"

    try:
        sandbox = open_sandbox(repo, branch, budget)
    except Refused as exc:
        result.outcome, result.summary = "refused", str(exc)
        result.finished = time.time()
        return result
    result.branch = branch

    try:
        model, name, why = pick_brain("hard" if task.priority >= 100 else difficulty)
        result.brain = f"{name} ({why})"
    except RuntimeError as exc:
        result.outcome, result.summary = "refused", str(exc)
        result.finished = time.time()
        return result

    # Baseline before the agent touches anything, inside the sandbox so it is
    # the same tree the agent will work in.
    before, before_out = failing_set(sandbox.root)
    result.failures_before = sorted(before)
    result.tests_before = before_out

    brief = (
        f"TASK ({task.kind}, priority {task.priority})\n"
        f"{task.title}\n\n"
        f"{task.detail}\n\n"
        f"Where: {task.where}\n"
    )
    if task.evidence:
        brief += f"\nEvidence:\n{task.evidence[:2500]}\n"

    # A local model is free; anything else is metered against max_heavy_calls.
    guard = BudgetGuard(budget, paid=(name != LOCAL_MODEL))
    agent = Agent(
        name="journeyman-night-shift",
        system_prompt=SYSTEM,
        tools=_tools_for(sandbox, result),
        model=model,
        callback_handler=None,
        hooks=[guard],
    )

    try:
        response = agent(brief)
        result.summary = str(response)[-600:].strip()
    except Refused as exc:
        result.outcome, result.summary = "refused", str(exc)
    except Exception as exc:
        result.outcome = "error"
        result.summary = f"{type(exc).__name__}: {exc}"

    result.stopped_by = guard.stopped_by

    # ---- verify for ourselves. The agent's own claim is not evidence. ----
    after, after_out = failing_set(sandbox.root)
    result.failures_after = sorted(after)
    result.tests_after = after_out
    result.new_failures = sorted(after - before)
    result.fixed_failures = sorted(before - after)
    result.green = not after

    result.files_changed = sandbox.changed_files()
    result.diff = sandbox.run("git diff").stdout if result.files_changed else ""
    result.log = sandbox.log
    result.budget = budget.to_dict()

    result.concerns = find_concerns(result.summary) + claims_vs_diff(
        result.summary, result.diff
    )

    if result.outcome not in ("refused", "error"):
        if result.new_failures:
            # Broke something that was working. Nothing else matters.
            result.outcome = "regressed"
            result.summary = (
                f"Broke {len(result.new_failures)} test(s) that were passing: "
                + ", ".join(result.new_failures[:3]) + ". Not committed."
            )
        elif not result.files_changed:
            result.outcome = "stuck"
            result.summary = result.summary or "Made no changes."
        elif task.kind == "failing_test" and not result.fixed_failures:
            # It was sent to fix a red test and the test is still red.
            result.outcome = "stuck"
        else:
            # Changed something, broke nothing. If it hedged, say so loudly.
            result.outcome = "fixed_with_concerns" if result.concerns else "fixed"

    # commit only what survived verification
    if result.outcome in ("fixed", "fixed_with_concerns"):
        if len(result.files_changed) > budget.max_files_changed:
            result.outcome = "refused"
            result.summary = (f"Changed {len(result.files_changed)} files, over the limit of "
                              f"{budget.max_files_changed}. Left uncommitted for review.")
        else:
            sandbox.run("git add -A")
            msg = f"{task.title[:68]}\n\nWorked unattended by Journeyman on {stamp}.\nTask: {task.kind} at {task.where}\n"
            (sandbox.root / ".git_commit_msg").write_text(msg, encoding="utf8")
            sandbox.run("git commit -F .git_commit_msg")
            (sandbox.root / ".git_commit_msg").unlink(missing_ok=True)
            result.commit = sandbox.run("git rev-parse --short HEAD").stdout.strip()

    result.finished = time.time()
    close_sandbox(repo, sandbox, keep=keep_worktree)
    return result


def report(result: ShiftResult) -> str:
    """What you read in the morning."""
    icon = {"fixed": "FIXED", "fixed_with_concerns": "FIXED, BUT READ THIS",
            "regressed": "BROKE SOMETHING, NOT COMMITTED", "stuck": "STUCK",
            "refused": "REFUSED", "no_work": "NOTHING TO DO",
            "error": "ERROR"}[result.outcome]
    lines = ["", "=" * 72]
    if result.task:
        lines.append(f"  {icon}   {result.task.title[:58]}")
        lines.append(f"          {result.task.where}")
    else:
        lines.append(f"  {icon}")
    lines.append("=" * 72)
    lines.append("")
    if result.brain:
        lines.append(f"  brain     {result.brain}")
    if result.branch:
        lines.append(f"  branch    {result.branch}")
    if result.commit:
        lines.append(f"  commit    {result.commit}")
    if result.failures_before or result.failures_after:
        lines.append(f"  tests     {len(result.failures_before)} red before, "
                     f"{len(result.failures_after)} red after")
        if result.fixed_failures:
            lines.append(f"            fixed:  {', '.join(result.fixed_failures[:3])}")
        if result.new_failures:
            lines.append(f"            BROKE:  {', '.join(result.new_failures[:3])}")
    else:
        lines.append("  tests     green before and after")
    lines.append(f"  took      {result.minutes} min")
    if result.stopped_by:
        lines.append(f"  STOPPED   {result.stopped_by}")
    if result.budget:
        b = result.budget
        lines.append(f"  budget    {b['iterations']}/{b['max_iterations']} turns, "
                     f"{b['commands_run']}/{b['max_commands']} commands, "
                     f"{b['heavy_calls']}/{b['max_heavy_calls']} paid calls")
    lines.append("")
    if result.files_changed:
        lines.append("  changed:")
        for f in result.files_changed[:12]:
            lines.append(f"    {f}")
        lines.append("")
    if result.concerns:
        lines.append("  READ THE DIFF BEFORE YOU MERGE:")
        for c in result.concerns:
            lines.append(f"    {c[:66]}")
        lines.append("")
    if result.summary:
        lines.append("  what it says:")
        for l in result.summary.splitlines()[-8:]:
            lines.append(f"    {l[:68]}")
        lines.append("")
    refused = [l for l in result.log if l.startswith("REFUSED")]
    if refused:
        lines.append("  refused during the shift:")
        for l in refused[:5]:
            lines.append(f"    {l[:68]}")
        lines.append("")
    if result.outcome in ("fixed", "fixed_with_concerns"):
        lines.append(f"  Review it:  git diff main..{result.branch}")
    lines.append("")
    return "\n".join(lines)
