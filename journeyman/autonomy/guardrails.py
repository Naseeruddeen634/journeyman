"""What the agent is not allowed to do.

An agent that edits code while nobody is watching needs its limits written down
in one place, enforced in code, and cheap to audit. Not in a system prompt. A
prompt is a request; this is the thing that actually says no.

The governing rule: **it proposes, you dispose.** Every shift ends as a branch
and a report. Nothing reaches your working tree, your main branch, or a remote
unless you put it there yourself.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class Refused(Exception):
    """Raised when the agent tries something it is not permitted to do."""


PROTECTED_BRANCHES = {"main", "master", "develop", "release", "prod", "production"}

# Anything the agent may run unattended. Everything else is refused.
# Deliberately dull: read, test, inspect. Nothing that reaches the network or
# mutates state outside the worktree.
ALLOWED = {
    "python", "python3", "pytest", "ruff", "mypy", "black", "isort",
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "diff",
    "git", "echo", "true",
}

# git is allowed, but only these subcommands.
GIT_ALLOWED = {
    "status", "diff", "log", "show", "add", "commit", "checkout", "branch",
    "rev-parse", "ls-files", "stash", "worktree", "config", "restore",
}
GIT_REFUSED = {"push", "reset", "clean", "rebase", "merge", "cherry-pick", "filter-branch", "gc"}

DANGEROUS = re.compile(
    r"(\brm\s+-rf?\b|\bsudo\b|\bchmod\s+777|\bcurl\b|\bwget\b|\bnc\b|\bssh\b|"
    r"\bpip\s+install\b|\bnpm\s+i(nstall)?\b|>\s*/dev/|\bmkfs\b|\bdd\s+if=|:\(\)\{)"
)


@dataclass
class Budget:
    """Hard stops. The agent cannot vote to extend them."""

    max_minutes: float = 45.0
    max_files_changed: int = 12
    max_commands: int = 120
    max_iterations: int = 8
    max_heavy_calls: int = 6          # calls to the paid model

    commands_run: int = 0
    heavy_calls: int = 0
    iterations: int = 0

    def spend_command(self) -> None:
        self.commands_run += 1
        if self.commands_run > self.max_commands:
            raise Refused(f"command budget exhausted ({self.max_commands})")

    def spend_heavy(self) -> None:
        self.heavy_calls += 1
        if self.heavy_calls > self.max_heavy_calls:
            raise Refused(f"heavy-model call budget exhausted ({self.max_heavy_calls})")

    def spend_iteration(self) -> None:
        self.iterations += 1
        if self.iterations > self.max_iterations:
            raise Refused(f"iteration budget exhausted ({self.max_iterations})")

    def to_dict(self) -> dict:
        return {
            "commands_run": self.commands_run, "max_commands": self.max_commands,
            "heavy_calls": self.heavy_calls, "max_heavy_calls": self.max_heavy_calls,
            "iterations": self.iterations, "max_iterations": self.max_iterations,
        }


@dataclass
class Sandbox:
    """A git worktree the agent may write to, and nothing else."""

    root: Path
    branch: str
    budget: Budget = field(default_factory=Budget)
    log: list[str] = field(default_factory=list)

    # ---- paths -------------------------------------------------------

    def resolve(self, path: str | Path) -> Path:
        """Resolve a path and refuse anything outside the sandbox.

        `~` is expanded before the check. Without that, "~/.ssh/id_rsa" is not
        absolute, so it resolves to a literal directory named "~" inside the
        sandbox and passes. Harmless today, a hole the moment anything
        downstream expands it.
        """
        raw = Path(path).expanduser()
        p = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        root = self.root.resolve()
        if not (p == root or root in p.parents):
            raise Refused(f"path {p} is outside the sandbox at {root}")
        return p

    def write(self, path: str | Path, content: str) -> Path:
        p = self.resolve(path)
        if p.suffix in {".pem", ".key", ".env"} or p.name in {".env", "credentials"}:
            raise Refused(f"refusing to write {p.name}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf8")
        self.log.append(f"wrote {p.relative_to(self.root)}")
        return p

    # Build noise the agent did not write and should not be judged on.
    NOISE = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
             ".DS_Store", ".journeyman/")

    def changed_files(self) -> list[str]:
        """Files the agent actually changed, excluding build noise.

        Without this, running the tests creates .pyc files, those count against
        the file-change budget, and they end up in the commit. The agent gets
        blamed for bytecode.
        """
        out = self._git(["status", "--porcelain"])
        files = [l[3:].strip() for l in out.splitlines() if l.strip()]
        return [f for f in files if not any(n in f for n in self.NOISE)]

    # ---- commands ----------------------------------------------------

    def run(self, command: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run a command inside the sandbox, if it is on the allowlist."""
        self.budget.spend_command()
        check_command(command)
        self.log.append(f"$ {command}")
        return subprocess.run(
            command, shell=True, cwd=self.root, capture_output=True,
            text=True, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    def _git(self, args: list[str]) -> str:
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        return r.stdout


def check_command(command: str) -> None:
    """Allowlist check. Raises Refused rather than returning False."""
    if DANGEROUS.search(command):
        raise Refused(f"refused, matches a dangerous pattern: {command!r}")

    for segment in re.split(r"&&|\|\||;|\|", command):
        parts = shlex.split(segment.strip())
        if not parts:
            continue
        exe = Path(parts[0]).name
        if exe.startswith("-"):
            continue
        if exe not in ALLOWED:
            raise Refused(f"refused, {exe!r} is not on the allowlist")
        if exe == "git" and len(parts) > 1:
            sub = parts[1]
            if sub in GIT_REFUSED:
                raise Refused(f"refused, `git {sub}` is never run unattended")
            if sub not in GIT_ALLOWED:
                raise Refused(f"refused, `git {sub}` is not on the allowlist")


def check_branch(branch: str) -> None:
    if branch.strip().lower() in PROTECTED_BRANCHES:
        raise Refused(f"refusing to work on {branch!r}: protected branch")


def open_sandbox(repo: Path, branch: str, budget: Budget | None = None) -> Sandbox:
    """Create an isolated git worktree for one shift.

    The agent never touches the user's checkout. If a shift goes wrong, the
    remedy is deleting a directory.
    """
    check_branch(branch)
    repo = Path(repo).resolve()
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True).stdout.strip()
    if current.lower() in PROTECTED_BRANCHES:
        pass  # branching off main is fine; working on it is not

    worktrees = repo / ".journeyman" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    path = worktrees / branch.replace("/", "-")
    if path.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                       cwd=repo, capture_output=True, text=True)
    r = subprocess.run(["git", "worktree", "add", "-b", branch, str(path)],
                       cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise Refused(f"could not create worktree: {r.stderr.strip()[:200]}")

    # Keep bytecode out of the shift's commit without touching the user's
    # own .gitignore, which is theirs.
    exclude = path / ".git" / "info" / "exclude"
    try:
        if exclude.parent.exists():
            existing = exclude.read_text(encoding="utf8") if exclude.exists() else ""
            if "__pycache__" not in existing:
                exclude.write_text(existing + "\n__pycache__/\n*.pyc\n.pytest_cache/\n"
                                   ".journeyman/\n", encoding="utf8")
    except OSError:
        pass
    return Sandbox(root=path, branch=branch, budget=budget or Budget())


def close_sandbox(repo: Path, sandbox: Sandbox, keep: bool = True) -> None:
    """Leave the branch behind for review; remove the working directory."""
    if keep:
        return
    subprocess.run(["git", "worktree", "remove", "--force", str(sandbox.root)],
                   cwd=Path(repo).resolve(), capture_output=True, text=True)


class BudgetGuard:
    """Enforces the time, turn and paid-call budgets inside the agent loop.

    The first version of this file declared max_minutes, max_iterations and
    max_heavy_calls on Budget and never enforced any of them. The README
    promised all three. An agent left running overnight with no turn limit is
    only bounded by the model deciding to stop, which is not a bound.

    This is a Strands HookProvider. Before every model call it counts the turn
    and checks the clock; when a limit is hit it sets `cancel`, which ends the
    invocation cleanly with a stop message as the final response. The shift's
    own verification still runs afterwards, so a budget stop is reported as
    what it is rather than as a crash.
    """

    def __init__(self, budget: Budget, paid: bool = False, clock=None) -> None:
        import time as _time

        self.budget = budget
        self.paid = paid
        self._clock = clock or _time.monotonic
        self.started = self._clock()
        self.stopped_by: str = ""
        self.model_calls = 0
        self.tool_calls = 0

    @property
    def elapsed_minutes(self) -> float:
        return (self._clock() - self.started) / 60.0

    def register_hooks(self, registry, **kwargs) -> None:
        from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent

        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(BeforeToolCallEvent, self._before_tool)

    def _stop(self, why: str) -> str:
        if not self.stopped_by:
            self.stopped_by = why
        return f"STUCK: stopped by the budget, {why}. Nothing further was attempted."

    def _before_model(self, event) -> None:
        if self.stopped_by:
            event.cancel = self._stop(self.stopped_by)
            return
        if self.elapsed_minutes > self.budget.max_minutes:
            event.cancel = self._stop(f"{self.budget.max_minutes:g} minute limit reached")
            return
        try:
            self.budget.spend_iteration()
            if self.paid:
                self.budget.spend_heavy()
        except Refused as exc:
            event.cancel = self._stop(str(exc))
            return
        self.model_calls += 1

    def _before_tool(self, event) -> None:
        self.tool_calls += 1
        if self.stopped_by:
            event.cancel_tool = f"Refused: {self.stopped_by}"
        elif self.elapsed_minutes > self.budget.max_minutes:
            self._stop(f"{self.budget.max_minutes:g} minute limit reached")
            event.cancel_tool = f"Refused: {self.stopped_by}"
