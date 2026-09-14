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

When the work is verified, reply with a one-line summary starting with DONE:.
If you are giving up, reply with a one-line summary starting with STUCK:.
"""

# Only review tasks have check_my_fix. When this paragraph was in SYSTEM for every
# task, the agent called check_my_fix on failing-test tasks, got "tool not found",
# and spent a turn on it: three times in one 12-case benchmark run.
REVIEW_RULES = """
If the task is a review finding, it counts as fixed only when that finding no
longer fires on the file. Use check_my_fix after each edit, and keep going
until it says the finding is gone. Removing the code that triggers the check
without fixing the underlying problem is not a fix.
"""


def system_prompt_for(task: Task) -> str:
    return SYSTEM + (REVIEW_RULES if task.kind == "ai_review" else "")


def checker_brain(worker_model_name: str):
    """The same brain the worker has, sampled hotter, so two checks are independent draws."""
    from ..brain import models

    if worker_model_name == models.HEAVY_MODEL:
        return models.heavy_brain(temperature=0.7)
    if worker_model_name == models.BEDROCK_MODEL:
        return models.bedrock_brain(temperature=0.7)
    return models.local_brain(temperature=0.7)


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

    bullets = re.findall(r"^\s*(?:\d+[.)]|[-*])\s+(\S.{10,})$", summary, re.M)
    # Only bullets that describe a change are claims. The first version counted
    # every bullet, so a correct one-line fix whose summary showed its arithmetic
    # ("- Discount amount = 80 * 0.25 = 20") was flagged as over-claiming. In the
    # benchmark that was 2 of 9 correct fixes, which is how a warning stops
    # being read.
    change = re.compile(r"\b(add|chang|updat|modif|wrap|replac|remov|delet|fix|mov|renam|"
                        r"introduc|creat|wr[io]t|insert|refactor|extract|implement|adjust|"
                        r"convert|swap|edit)\w*", re.I)
    claims = [b for b in bullets if change.search(b)]
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


CONTRACT_WORDS = re.compile(r"(docstring|documented|documentation|the spec\b|specification|"
                            r"contract)", re.I)
CONFLICT_WORDS = re.compile(r"(mismatch|contradict|conflict|inconsisten|doesn't match|does not "
                            r"match|violat|differs? from|not what the)", re.I)


def admits_contract_conflict(summary: str) -> str:
    """The sentence where the agent says its change contradicts the documentation.

    Twice a shift deleted documented behaviour to satisfy a test and said so in
    its own summary. A flagged fix is still a delivered fix, so an admission is
    now a reason to withhold, not a footnote.
    """
    for raw in re.split(r"(?<=[.!?])\s+|\n", summary or ""):
        if CONTRACT_WORDS.search(raw) and CONFLICT_WORDS.search(raw):
            return raw.strip()[:220]
    return ""


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
    confined: bool | None = None
    feedback_rounds: int = 0
    feedback: list[str] = field(default_factory=list)
    spec_check: str = ""         # what the independent check concluded, "" when it did not run
    finding_resolved: bool | None = None
    findings_introduced: list[str] = field(default_factory=list)
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
            "stopped_by": self.stopped_by, "confined": self.confined,
            "feedback_rounds": self.feedback_rounds, "feedback": self.feedback,
            "spec_check": self.spec_check,
            "finding_resolved": self.finding_resolved,
            "findings_introduced": self.findings_introduced,
            "failures_before": self.failures_before, "failures_after": self.failures_after,
            "new_failures": self.new_failures, "fixed_failures": self.fixed_failures,
            "minutes": self.minutes, "budget": self.budget,
            "diff": self.diff[:8000], "log": self.log[-80:],
        }


FAIL_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def failing_set(repo: Path, jail: bool = False) -> tuple[set[str], str]:
    """Which tests are red right now, as a set of node ids.

    The gate cannot be "the suite is green". Plenty of real repositories are
    not green when you arrive: a test needs a credential, an integration suite
    is skipped in CI, someone left a known failure. Demanding perfection means
    every honest piece of work gets reported as STUCK.

    What matters is whether the agent broke anything that was working.
    """
    from . import jail as _jail
    from .scout import _interpreter, fresh_env

    argv = [_interpreter(repo), "-m", "pytest", "-q", "--no-header", "--tb=no",
            "-p", "no:cacheprovider"]
    env = fresh_env()
    if jail:
        # The agent has edited this code. Running it runs the agent's code.
        argv, env, _ = _jail.wrap(argv, Path(repo), env)
    try:
        r = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=300, env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return set(), f"could not run the suite: {exc}"
    out = r.stdout + r.stderr
    return set(FAIL_LINE.findall(out)), out[-2500:]


def finding_counts(root: Path, file: str) -> dict[str, int]:
    """How many of each finding code sit in one file, right now."""
    from ..patterns.smells import review

    counts: dict[str, int] = {}
    for f in review(root):
        if f.where.split(":")[0] == file:
            counts[f.code] = counts.get(f.code, 0) + 1
    return counts


def resolution(code: str, before: dict[str, int], after: dict[str, int]) -> tuple[bool, list[str]]:
    """Did the change remove the finding it was sent for, and add any new ones?

    Matched on code and file rather than line, because a fix moves lines.
    An earlier version never asked this at all: a review task was marked FIXED
    whenever something changed and no test broke, including a run where the
    agent wrapped the input in backticks and AIE004 still fired on the line.
    """
    resolved = after.get(code, 0) < before.get(code, 0)
    introduced = sorted(c for c, n in after.items() if n > before.get(c, 0))
    return resolved, introduced


def gather_context(root: Path, task: Task, limit_chars: int = 14000) -> str:
    """Hand the agent the files it would otherwise spend turns opening.

    For a failing test: the files named in the traceback, the test file, and the
    repository modules that test imports. For a review finding: the file it was
    found in. Numbered lines, capped, most specific first.

    Turns are the budget, and on a local model they are also the wall clock, so
    every read_file the harness can answer for free is time back.
    """
    import re as _re

    from ..pair import import_graph

    root = root.resolve()
    ordered: list[Path] = []

    def add(p: Path) -> None:
        p = p.resolve()
        try:
            p.relative_to(root)
        except ValueError:
            return
        if p.suffix == ".py" and p.exists() and p not in ordered:
            ordered.append(p)

    where = root / task.where.split(":")[0]
    if task.kind == "failing_test":
        for m in _re.finditer(r"([\w./-]+\.py):\d+", task.evidence or ""):
            add(root / m.group(1))
        add(where)
        graph = import_graph(root)
        for test in list(ordered):
            for dep in sorted(graph.get(test.resolve(), ())):
                add(dep)
    else:
        add(where)

    chunks, used = [], 0
    for p in ordered:
        rel = p.relative_to(root)
        lines = p.read_text(encoding="utf8", errors="ignore").splitlines()
        body = "\n".join(f"{i:5d}  {l}" for i, l in enumerate(lines[:400], 1))
        block = f"--- {rel} ---\n{body}\n"
        if used + len(block) > limit_chars:
            if not chunks:
                chunks.append(block[:limit_chars])
            break
        chunks.append(block)
        used += len(block)
    return "\n".join(chunks)


FEEDBACK = """Not done yet. I checked your work independently, and this is what I found:

{feedback}

Fix the underlying cause. Do not edit or delete tests to make them pass, and do
not delete the code the check is looking at. When it is genuinely fixed, reply
with a one-line summary starting with DONE:. If you cannot fix it, reply with
STUCK: and what you think is going on."""


def progress_check(root: Path, task: Task, fail_before: set[str],
                   findings_before: dict[str, int], changed: list[str],
                   summary: str = "") -> tuple[bool, str]:
    """The same test the shift applies at the end, run while there is still time to act.

    An agent that says DONE has made a claim. In real runs it said DONE with the
    finding still firing and turns left in the budget. The cheapest fix for that
    is the one a senior engineer uses on a junior: look, and say what is still
    wrong.
    """
    problems: list[str] = []
    after, out = failing_set(root, jail=True)

    if not changed:
        problems.append("You have not changed any file.")
    new = sorted(after - fail_before)
    if new:
        problems.append("These tests passed before your change and fail now:\n  "
                        + "\n  ".join(new) + "\n\n" + out[-1500:])
    if task.kind == "failing_test" and not (fail_before - after):
        problems.append("The test you were sent to fix is still failing:\n\n" + out[-1500:])
    if task.kind == "ai_review" and task.meta.get("code"):
        from ..patterns.smells import review

        code, target = task.meta["code"], task.where.split(":")[0]
        still = [f for f in review(root) if f.code == code and f.where.split(":")[0] == target]
        if still and len(still) >= findings_before.get(code, 0):
            lines = [f"{code} still fires on {target}:"]
            for f in still[:2]:
                lines.append(f"  line {f.where.rsplit(':', 1)[-1]}: {f.title}")
                if f.evidence:
                    lines.append(f"    {f.evidence}")
            lines.append(f"\nWhat resolves it: {still[0].fix}")
            problems.append("\n".join(lines))

    from .contract import check_worktree

    for v in check_worktree(root, changed):
        problems.append("Your change breaks a documented promise. " + v.describe() + " Make the "
                        "code satisfy both the test and the docstring. If they genuinely "
                        "conflict, reply STUCK and explain the conflict instead of choosing one.")
    admitted = admits_contract_conflict(summary)
    if admitted:
        problems.append(f"You said: \"{admitted}\" A change that contradicts the documented "
                        "behaviour will not be accepted. Satisfy both, or reply STUCK and explain.")
    return (not problems, "\n\n".join(problems))


def _tools_for(sandbox: Sandbox, result: ShiftResult, task: Task | None = None):
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
        from .scout import skipped

        out = []
        for p in sorted(base.rglob("*.py")):
            if skipped(p, sandbox.root, {".git", ".venv", "__pycache__", ".journeyman"}):
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

        argv = [_interpreter(sandbox.root), "-m", "pytest", "-q", "--no-header", "--tb=short",
                "-p", "no:cacheprovider"]
        if target:
            # A path inside the sandbox, optionally with a ::node id. Nothing else.
            path, _, node = target.strip().partition("::")
            if not re.fullmatch(r"[\w./-]+", path) or (node and not re.fullmatch(r"[\w:\[\]./-]+", node)):
                return f"Refused: {target!r} is not a test path inside the repository."
            try:
                sandbox.resolve(path)
            except Refused as exc:
                return f"Refused: {exc}"
            argv.append(target.strip())
        try:
            r = sandbox.run_argv(argv, timeout=300, jailed=True)
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
            r = sandbox.run_argv(["git", "diff"], timeout=60)
        except Refused as exc:
            return f"Refused: {exc}"
        return (r.stdout or "(no changes yet)")[:6000]

    tools = [read_file, write_file, list_files, run_tests, show_diff]

    if task is not None and task.kind == "ai_review" and task.meta.get("code"):
        code = task.meta["code"]
        target = task.where.split(":")[0]

        @tool
        def check_my_fix() -> str:
            """Re-run the review on the file you were asked to fix.

            Use this after editing. The shift is only counted as fixed if the
            finding you were sent for no longer fires, so check before you stop.

            Returns:
                Whether the finding still fires, and anything new your change introduced.
            """
            from ..patterns.smells import review

            here = [f for f in review(sandbox.root) if f.where.split(":")[0] == target]
            still = [f for f in here if f.code == code]
            others = sorted({f.code for f in here if f.code != code})
            if still:
                lines = [f"{code} still fires on {target}:"]
                lines += [f"  line {f.where.rsplit(':', 1)[-1]}: {f.evidence or f.title}"
                          for f in still[:3]]
                lines.append(f"What resolves it: {still[0].fix}")
                return "\n".join(lines)
            msg = f"{code} no longer fires on {target}."
            if others:
                msg += f" Other findings in the file: {', '.join(others)}."
            return msg

        tools.append(check_my_fix)
    return tools


def work_one(repo: str | Path, task: Task | None = None, budget: Budget | None = None,
             difficulty: str = "routine", keep_worktree: bool = True,
             max_feedback_rounds: int = 2, pregather: bool = False,
             spec_check: bool = False) -> ShiftResult:
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
    before, before_out = failing_set(sandbox.root, jail=True)
    result.failures_before = sorted(before)
    result.tests_before = before_out

    review_file = task.where.split(":")[0]
    review_code = task.meta.get("code", "")
    findings_before = finding_counts(sandbox.root, review_file) if task.kind == "ai_review" else {}

    brief = (
        f"TASK ({task.kind}, priority {task.priority})\n"
        f"{task.title}\n\n"
        f"{task.detail}\n\n"
        f"Where: {task.where}\n"
    )
    if task.evidence:
        brief += f"\nEvidence:\n{task.evidence[:2500]}\n"
    if pregather:
        context = gather_context(sandbox.root, task)
        if context:
            brief += ("\nRelevant files, already read for you. Use read_file only for "
                      "files not shown here:\n\n" + context)

    # A local model is free; anything else is metered against max_heavy_calls.
    guard = BudgetGuard(budget, paid=(name != LOCAL_MODEL))
    agent = Agent(
        name="journeyman-night-shift",
        system_prompt=system_prompt_for(task),
        tools=_tools_for(sandbox, result, task),
        model=model,
        callback_handler=None,
        hooks=[guard],
    )

    # The budget hook runs between model calls, so it cannot stop a single
    # generation that runs away. Agent.cancel() is thread-safe and takes effect
    # during streaming, so a timer enforces the wall clock from outside.
    import threading

    def _overtime() -> None:
        if not guard.stopped_by:
            guard.stopped_by = f"{budget.max_minutes:g} minute limit reached during a model call"
        agent.cancel()

    watchdog = threading.Timer(budget.max_minutes * 60, _overtime)
    watchdog.daemon = True
    watchdog.start()
    from . import spec_check as _spec

    spec_codes: list[str] | None = None

    def independent_checks() -> list[str]:
        """Written once, from the task and the pre-change docstrings, by fresh contexts."""
        specs = _spec.changed_functions(sandbox.root, sandbox.changed_files())
        if not specs:
            result.spec_check = "not run: no documented function changed"
            return []

        def ask(text: str) -> str:
            checker = Agent(name="journeyman-checker", system_prompt=_spec.CHECKER_SYSTEM,
                            model=checker_brain(name), callback_handler=None, hooks=[guard])
            return str(checker(text))

        codes = _spec.write_checks(ask, task.title, task.detail, specs)
        if len(codes) < 2:
            result.spec_check = "not run: the checker did not produce two usable test files"
        return codes

    try:
        prompt = brief
        for round_no in range(max_feedback_rounds + 1):
            response = agent(prompt)
            result.summary = str(response)[-600:].strip()
            if guard.stopped_by or round_no == max_feedback_rounds:
                break
            ok, feedback = progress_check(sandbox.root, task, before, findings_before,
                                          sandbox.changed_files(), result.summary)
            if ok and spec_check:
                if spec_codes is None:
                    spec_codes = independent_checks()
                if len(spec_codes) >= 2:
                    verdict = _spec.judge_all(sandbox.root, spec_codes)
                    if not verdict.ok:
                        ok, feedback = False, verdict.feedback()
            if ok:
                break
            result.feedback_rounds += 1
            result.feedback.append(feedback[:1200])
            prompt = FEEDBACK.format(feedback=feedback)
    except Refused as exc:
        result.outcome, result.summary = "refused", str(exc)
    except Exception as exc:
        result.outcome = "error"
        result.summary = f"{type(exc).__name__}: {exc}"
    finally:
        watchdog.cancel()

    result.stopped_by = guard.stopped_by
    from . import jail as _jail
    result.confined = _jail.available()

    # ---- verify for ourselves. The agent's own claim is not evidence. ----
    after, after_out = failing_set(sandbox.root, jail=True)
    result.failures_after = sorted(after)
    result.tests_after = after_out
    result.new_failures = sorted(after - before)
    result.fixed_failures = sorted(before - after)
    result.green = not after

    result.files_changed = sandbox.changed_files()
    result.diff = sandbox.run_argv(["git", "diff"]).stdout if result.files_changed else ""
    result.log = sandbox.log
    result.budget = budget.to_dict()

    result.concerns = find_concerns(result.summary) + claims_vs_diff(
        result.summary, result.diff
    )

    if task.kind == "ai_review" and review_code:
        findings_after = finding_counts(sandbox.root, review_file)
        result.finding_resolved, result.findings_introduced = resolution(
            review_code, findings_before, findings_after)
        if result.findings_introduced:
            result.concerns.append(
                "The change introduced new review findings in the same file: "
                + ", ".join(result.findings_introduced))

    from .contract import check_worktree

    violations = check_worktree(sandbox.root, result.files_changed)
    admitted = admits_contract_conflict(result.summary)

    spec_broke = ""
    if spec_check and result.files_changed and not guard.stopped_by:
        if spec_codes is None:          # no feedback round ran, so the check has not been written
            spec_codes = independent_checks()
        if len(spec_codes) >= 2:
            final = _spec.judge_all(sandbox.root, spec_codes)
            if not final.usable:
                result.spec_check = f"not run: {final.note}"
            elif final.ok:
                result.spec_check = f"passed: {final.passed} independent test(s)"
            else:
                result.spec_check = final.concern()
                if final.broke:
                    spec_broke = final.concern()
                else:
                    result.concerns.append(final.concern())

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
        elif spec_broke:
            # Two independent checks, written without seeing the code, agree that behaviour
            # which worked before the change no longer does.
            result.outcome = "stuck"
            result.concerns.append(spec_broke)
            result.summary = ("Made the tests pass, but broke behaviour the documentation "
                              f"describes. Not committed. {spec_broke}\n" + (result.summary or ""))
        elif violations or admitted:
            # The tests went green by deleting what the code promises. Twice a
            # shift did exactly this; once it was delivered. Not any more.
            result.outcome = "stuck"
            reason = violations[0].describe() if violations else f'The agent said: "{admitted}"'
            result.concerns.append(reason)
            result.summary = ("Made the tests pass, but the change contradicts the documented "
                              f"behaviour. Not committed. {reason}\n" + (result.summary or ""))
        elif task.kind == "ai_review" and result.finding_resolved is False:
            # Changed something, but the thing it was sent for is still there.
            result.outcome = "stuck"
            result.summary = (
                f"Changed {', '.join(result.files_changed[:2])} but {review_code} still "
                f"fires on {review_file}. Not committed.\n" + (result.summary or ""))
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
            (sandbox.root / _spec.SPEC_FILE).unlink(missing_ok=True)   # never part of the work
            sandbox.run_argv(["git", "add", "-A"])
            msg = f"{task.title[:68]}\n\nWorked unattended by Journeyman on {stamp}.\nTask: {task.kind} at {task.where}\n"
            (sandbox.root / ".git_commit_msg").write_text(msg, encoding="utf8")
            sandbox.run_argv(["git", "commit", "-q", "-F", ".git_commit_msg"])
            (sandbox.root / ".git_commit_msg").unlink(missing_ok=True)
            result.commit = sandbox.run_argv(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()

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
    if result.feedback_rounds:
        lines.append(f"  feedback  sent back {result.feedback_rounds} time(s) after claiming done")
    if result.confined is False:
        lines.append("  UNCONFINED  the repository's tests ran without an OS sandbox on this platform")
    if result.finding_resolved is not None:
        lines.append(f"  finding   {'resolved' if result.finding_resolved else 'STILL THERE'}"
                     + (f", introduced {', '.join(result.findings_introduced)}"
                        if result.findings_introduced else ""))
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
