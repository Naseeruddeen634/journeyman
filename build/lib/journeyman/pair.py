"""Pair mode: beside you while you work, not after you have left.

When you are in the middle of a problem you have the context and the agent does
not, so it should not try to solve your problem. It should do the things you
would otherwise have to stop and do:

  - you saved a file          -> run the tests that depend on it, and only those
  - a test you had green      -> tell you the moment it goes red, and which save did it
  - you just added a model call -> tell you if it is the kind of thing that pages someone

And otherwise say nothing. Silence is the feature. A pair that comments on
every keystroke is worse than no pair, because you learn to stop reading it.

No model is involved. Working out which tests a file affects is an import
graph, and running them is pytest, and neither is improved by a language model.
"""

from __future__ import annotations

import ast
import subprocess
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .autonomy.scout import _interpreter, fresh_env, skipped

FAIL_PREFIXES = ("FAILED ", "ERROR ")


def _py_files(repo: Path) -> list[Path]:
    return [p for p in repo.rglob("*.py") if not skipped(p, repo)]


def is_test(path: Path, repo: Path | None = None) -> bool:
    try:
        parts = path.resolve().relative_to(repo.resolve()).parts if repo else path.parts[-3:]
    except ValueError:
        parts = path.parts[-3:]
    return path.name.startswith("test_") or path.name.endswith("_test.py") \
        or "tests" in parts


def _module_candidates(repo: Path, importer: Path, node: ast.AST) -> list[Path]:
    """Files an import statement could refer to, inside this repo."""
    names: list[str] = []
    level = 0
    if isinstance(node, ast.Import):
        names = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
        level = node.level or 0
        base = node.module or ""
        names = [base] if base else []
        # `from pkg import mod` may name a submodule rather than an attribute
        names += [f"{base}.{a.name}" if base else a.name for a in node.names]

    roots = [repo]
    if level:
        anchor = importer.parent
        for _ in range(level - 1):
            anchor = anchor.parent
        roots = [anchor]

    out = []
    for name in names:
        rel = Path(*name.split(".")) if name else Path()
        for root in roots:
            for cand in (root / rel.with_suffix(".py"), root / rel / "__init__.py"):
                if cand.exists():
                    out.append(cand.resolve())
    return out


def import_graph(repo: Path) -> dict[Path, set[Path]]:
    """file -> files it imports, for files inside the repo only."""
    graph: dict[Path, set[Path]] = {}
    for f in _py_files(repo):
        deps: set[Path] = set()
        try:
            tree = ast.parse(f.read_text(encoding="utf8", errors="ignore"))
        except SyntaxError:
            graph[f.resolve()] = deps
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                deps.update(_module_candidates(repo, f, node))
        graph[f.resolve()] = deps
    return graph


def affected_tests(repo: str | Path, changed: str | Path,
                   graph: dict[Path, set[Path]] | None = None) -> list[Path]:
    """Test files that import the changed file, directly or through other modules."""
    repo = Path(repo).resolve()
    changed = (repo / changed).resolve() if not Path(changed).is_absolute() else Path(changed).resolve()
    graph = graph if graph is not None else import_graph(repo)

    if is_test(changed, repo):
        return [changed]

    reverse: dict[Path, set[Path]] = defaultdict(set)
    for f, deps in graph.items():
        for d in deps:
            reverse[d].add(f)

    seen, queue, tests = {changed}, deque([changed]), set()
    while queue:
        cur = queue.popleft()
        for importer in reverse.get(cur, ()):
            if importer in seen:
                continue
            seen.add(importer)
            if is_test(importer, repo):
                tests.add(importer)
            else:
                queue.append(importer)

    if not tests:  # nothing imports it: fall back to naming convention
        for f in graph:
            if is_test(f, repo) and f.stem in (f"test_{changed.stem}", f"{changed.stem}_test"):
                tests.add(f)
    return sorted(tests)


def run_tests(repo: Path, tests: list[Path], timeout: int = 120) -> tuple[set[str], set[str], str]:
    """(passed node ids, failed node ids, tail of output)."""
    if not tests:
        return set(), set(), ""
    # macOS: /var is a symlink to /private/var. Graph keys are resolved, so the
    # repo must be too, or relative_to raises for every temp-dir repo.
    repo = repo.resolve()
    tests = [t.resolve() for t in tests]
    args = [_interpreter(repo), "-m", "pytest", "-q", "-rA", "--no-header", "--tb=line",
            "-p", "no:cacheprovider", *[str(t.relative_to(repo)) for t in tests]]
    try:
        r = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=timeout,
                           env=fresh_env())
    except subprocess.TimeoutExpired:
        return set(), set(), "tests timed out"
    out = r.stdout + r.stderr
    passed, failed = set(), set()
    for line in out.splitlines():
        if line.startswith("PASSED "):
            passed.add(line.split()[1])
        elif line.startswith(FAIL_PREFIXES):
            failed.add(line.split()[1])
    return passed, failed, out[-1200:]


@dataclass
class PairState:
    """What pair mode remembers between saves, so it can speak only about changes."""

    repo: Path
    status: dict[str, str] = field(default_factory=dict)      # node id -> pass|fail
    findings: dict[str, set[str]] = field(default_factory=dict)  # file -> {code:title}
    graph: dict[Path, set[Path]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()

    def on_save(self, changed: list[Path]) -> list[str]:
        """Everything worth saying about one batch of saves. Usually nothing."""
        from .patterns.smells import review

        self.graph = import_graph(self.repo)
        messages: list[str] = []
        tests: set[Path] = set()
        for c in changed:
            tests.update(affected_tests(self.repo, c, self.graph))

        if tests:
            passed, failed, out = run_tests(self.repo, sorted(tests))
            names = ", ".join(Path(c).name for c in changed)
            broke = sorted(t for t in failed if self.status.get(t) != "fail")
            healed = sorted(t for t in passed if self.status.get(t) == "fail")
            for t in passed:
                self.status[t] = "pass"
            for t in failed:
                self.status[t] = "fail"
            if broke:
                first = next((l for l in out.splitlines() if "assert" in l.lower()
                              or "Error" in l), "")
                messages.append(f"RED after saving {names}: {', '.join(broke[:3])}"
                                + (f"\n      {first.strip()[:110]}" if first else ""))
            if healed:
                messages.append(f"green again: {', '.join(healed[:3])}")

        findings_now = review(self.repo)
        for c in changed:
            rel = str(Path(c).resolve().relative_to(self.repo))
            here = {f"{f.code}: {f.title}" for f in findings_now if f.where.split(":")[0] == rel}
            before = self.findings.get(rel)
            if before is not None:
                for new in sorted(here - before):
                    messages.append(f"in {rel}, new: {new}")
            self.findings[rel] = here
        return messages

    def prime(self) -> None:
        """Learn the current state quietly, so the first save is compared to reality."""
        from .patterns.smells import review

        self.graph = import_graph(self.repo)
        tests = sorted(f for f in self.graph if is_test(f, self.repo))
        passed, failed, _ = run_tests(self.repo, tests, timeout=600)
        self.status = {t: "pass" for t in passed} | {t: "fail" for t in failed}
        for f in review(self.repo):
            self.findings.setdefault(f.where.split(":")[0], set()).add(f"{f.code}: {f.title}")
        for f in self.graph:
            self.findings.setdefault(str(f.resolve().relative_to(self.repo)), set())


def snapshot(repo: Path) -> dict[Path, float]:
    return {p: p.stat().st_mtime for p in _py_files(repo) if p.exists()}


def watch(repo: str | Path, interval: float = 1.0, settle: float = 0.6, notify=None,
          out=print) -> None:
    """Poll for saves and speak only when something changed for the worse or recovered.

    Standard library only. A file-watching dependency would be one more thing to
    install for a loop that checks mtimes once a second.
    """
    repo = Path(repo).resolve()
    state = PairState(repo)
    out(f"  pairing on {repo}  (learning the current state...)")
    state.prime()
    red = sum(1 for v in state.status.values() if v == "fail")
    out(f"  ready: {len(state.status)} tests, {red} already red. Quiet unless that changes.")

    last = snapshot(repo)
    while True:
        time.sleep(interval)
        now = snapshot(repo)
        changed = [p for p, m in now.items() if last.get(p) != m]
        if not changed:
            continue
        time.sleep(settle)            # let a burst of saves finish
        now = snapshot(repo)
        changed = [p for p, m in now.items() if last.get(p) != m]
        last = now
        for msg in state.on_save(changed):
            stamp = time.strftime("%H:%M:%S")
            out(f"  {stamp}  {msg}")
            if notify and msg.startswith("RED"):
                notify(msg)


def macos_notify(message: str) -> None:
    """A banner, so you hear about red without looking at the terminal."""
    safe = message.replace('"', "'").splitlines()[0][:200]
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "Journeyman"'],
                   capture_output=True)
