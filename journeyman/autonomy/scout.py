"""Finding work without being told.

The difference between a tool and a colleague is that a colleague notices. This
walks a repository and builds a ranked queue of things that are actually wrong,
so the agent has something to do at 2am without anyone filing a ticket.

Everything here is deterministic inspection. No model is involved in deciding
what is broken, which means the queue is the same whether or not the laptop has
a GPU, and you can audit why a task was picked.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".journeyman",
             "dist", "build", ".pytest_cache", ".mypy_cache", "runs", ".idea"}

MARKER = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)", re.I)


@dataclass
class Task:
    kind: str               # failing_test | todo | untested | backlog | lint
    title: str
    detail: str
    where: str              # file[:line]
    priority: int           # higher first
    evidence: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "detail": self.detail,
                "where": self.where, "priority": self.priority,
                "evidence": self.evidence[:2000], "meta": self.meta}


def _py_files(repo: Path) -> list[Path]:
    out = []
    for p in repo.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------- sources


def _interpreter(repo: Path) -> str:
    """The project's own Python, not whatever is on PATH.

    Running the repo's tests with the wrong interpreter reports an import error
    as a failing test, which sends the agent off to fix a bug that does not
    exist. Worth the six lines.
    """
    for candidate in (repo / ".venv" / "bin" / "python",
                      repo / "venv" / "bin" / "python",
                      repo / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def failing_tests(repo: Path, timeout: int = 300) -> list[Task]:
    """A red test is the least ambiguous work there is."""
    try:
        r = subprocess.run(
            [_interpreter(repo), "-m", "pytest", "-q", "--no-header", "--tb=short"],
            cwd=repo, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if r.returncode == 0:
        return []

    out = r.stdout + r.stderr
    tasks = []
    # pytest's summary lines look like: FAILED path/to/test.py::test_name - msg
    for m in re.finditer(r"^(FAILED|ERROR)\s+([^\s:]+\.py)(::[^\s]+)?(?:\s+-\s+(.*))?$",
                         out, re.M):
        status, path, node, msg = m.group(1), m.group(2), (m.group(3) or ""), (m.group(4) or "")
        name = node.lstrip(":") or path
        tasks.append(Task(
            kind="failing_test",
            title=f"{status.lower()}: {name}" + (f" - {msg[:60]}" if msg else ""),
            detail=msg or "A test is failing. Fix the cause, not the assertion.",
            where=path,
            priority=100,
            evidence=out[-4000:],
            meta={"node": node.lstrip(":")},
        ))
    if not tasks and r.returncode != 0:
        tasks.append(Task(
            kind="failing_test", title="the test suite does not pass",
            detail="pytest exited non-zero.", where="tests/",
            priority=100, evidence=out[-4000:],
        ))
    return tasks


def todos(repo: Path, limit: int = 40) -> list[Task]:
    """TODO and FIXME comments, which are work somebody already admitted to."""
    tasks = []
    for path in _py_files(repo):
        try:
            lines = path.read_text(encoding="utf8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = MARKER.search(line)
            if not m:
                continue
            kind, text = m.group(1).upper(), m.group(2).strip()
            if not text:
                continue
            context = "\n".join(lines[max(0, i - 6): i + 8])
            tasks.append(Task(
                kind="todo",
                title=f"{kind}: {text[:70]}",
                detail=text,
                where=f"{path.relative_to(repo)}:{i}",
                priority=60 if kind in ("FIXME", "XXX") else 40,
                evidence=context,
            ))
            if len(tasks) >= limit:
                return tasks
    return tasks


def untested_functions(repo: Path, limit: int = 25) -> list[Task]:
    """Public functions whose name appears nowhere in the tests.

    Crude by design. It is a prompt for attention, not a coverage tool, and it
    is ranked below real failures for that reason.
    """
    test_text = ""
    for d in ("tests", "test"):
        tdir = repo / d
        if tdir.exists():
            for p in tdir.rglob("*.py"):
                test_text += p.read_text(encoding="utf8", errors="ignore")
    if not test_text:
        return []

    tasks = []
    for path in _py_files(repo):
        if "test" in path.parts or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf8", errors="ignore"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            if re.search(rf"\b{re.escape(node.name)}\b", test_text):
                continue
            doc = ast.get_docstring(node) or ""
            tasks.append(Task(
                kind="untested",
                title=f"no test covers {node.name}()",
                detail=doc.strip().splitlines()[0] if doc else "",
                where=f"{path.relative_to(repo)}:{node.lineno}",
                priority=25,
                meta={"function": node.name},
            ))
            if len(tasks) >= limit:
                return tasks
    return tasks


def ai_engineering_review(repo: Path, limit: int = 15) -> list[Task]:
    """Findings a senior AI engineer would raise in review.

    This is what separates the queue from a generic linter's. A hardcoded model
    id, a prompt with no eval, user text interpolated into a system prompt: none
    of those are syntax errors, all of them are the thing that pages someone.
    """
    from ..patterns.smells import review

    tasks = []
    for f in review(repo)[:limit]:
        tasks.append(Task(
            kind="ai_review",
            title=f"{f.code}: {f.title}",
            detail=f"{f.why}\n\nWhat to do: {f.fix}",
            where=f.where,
            # slot below failing tests, above TODOs: these are real problems,
            # but a red test is a problem you already know about
            priority=min(95, 45 + f.severity // 2),
            evidence=f.evidence,
            meta={"code": f.code, "severity": f.severity},
        ))
    return tasks


def backlog(repo: Path) -> list[Task]:
    """Anything you left in BACKLOG.md. Unchecked boxes are the queue."""
    f = repo / "BACKLOG.md"
    if not f.exists():
        return []
    tasks = []
    for i, line in enumerate(f.read_text(encoding="utf8").splitlines(), 1):
        m = re.match(r"\s*[-*]\s*\[ \]\s*(.+)", line)
        if m:
            tasks.append(Task(
                kind="backlog", title=m.group(1).strip()[:80],
                detail=m.group(1).strip(), where=f"BACKLOG.md:{i}",
                priority=80,
            ))
    return tasks


def survey(repo: str | Path) -> list[Task]:
    """The full sweep, ranked. This is what the agent wakes up to."""
    repo = Path(repo).resolve()
    tasks: list[Task] = []
    tasks += failing_tests(repo)
    tasks += ai_engineering_review(repo)
    tasks += backlog(repo)
    tasks += todos(repo)
    tasks += untested_functions(repo)
    return sorted(tasks, key=lambda t: -t.priority)


def describe(tasks: list[Task]) -> str:
    if not tasks:
        return "Nothing to do. Tests pass, no TODOs, no backlog."
    by_kind: dict[str, int] = {}
    for t in tasks:
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
    parts = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in by_kind.items())
    return f"{len(tasks)} thing(s) worth doing: {parts}"
