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
import json
import re
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .autonomy.scout import _interpreter, fresh_env, skipped

FAIL_PREFIXES = ("FAILED ", "ERROR ")
# A save that breaks conftest.py stops pytest before any test runs, so nothing is
# listed as failed and pair mode used to stay silent. This node stands for "the
# tests could not run": red when they cannot, and passing on every run that works,
# so recovering from it is announced like any other test going green.
DID_NOT_RUN = "(tests did not run)"


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

    if is_test(changed, repo) and changed.name != "conftest.py":
        return [changed]
    if changed.name == "conftest.py":
        # pytest loads it for every test beneath it, and nothing imports it
        return sorted(f for f in graph if is_test(f, repo) and f.name != "conftest.py"
                      and changed.parent in f.parents)

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
    except (subprocess.TimeoutExpired, OSError) as exc:
        return set(), {DID_NOT_RUN}, f"tests did not finish: {exc}"
    out = r.stdout + r.stderr
    passed, failed = set(), set()
    for line in out.splitlines():
        if line.startswith("PASSED "):
            passed.add(line.split()[1])
        elif line.startswith(FAIL_PREFIXES):
            failed.add(line.split()[1])
    # pytest exit 3/4: internal or usage error, e.g. a conftest.py that does not import
    if r.returncode in (3, 4) or (r.returncode not in (0, 5) and not passed and not failed):
        failed.add(DID_NOT_RUN)
        errors = [l for l in out.splitlines() if "Error" in l]
        return passed, failed, (errors[-1] if errors else out[-200:]).strip()
    passed.add(DID_NOT_RUN)
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
        js_tests: set[Path] = set()
        runner = js_runner(self.repo)
        js_graph = js_import_graph(self.repo) if runner else {}
        for c in changed:
            if Path(c).suffix in JS_SUFFIXES:
                if runner:
                    js_tests.update(affected_js_tests(self.repo, c, js_graph))
            else:
                tests.update(affected_tests(self.repo, c, self.graph))

        runs = []
        if tests:
            runs.append(run_tests(self.repo, sorted(tests)))
        if js_tests:
            p_, f_, first = run_js_tests(self.repo, sorted(js_tests), runner)
            runs.append((p_, f_, first))
        for passed, failed, out in runs:
            names = ", ".join(Path(c).name for c in changed)
            broke = sorted(t for t in failed if self.status.get(t) != "fail")
            healed = sorted(t for t in passed if self.status.get(t) == "fail")
            for t in passed:
                self.status[t] = "pass"
            for t in failed:
                self.status[t] = "fail"
            if broke:
                first = next((l for l in out.splitlines() if "assert" in l.lower()
                              or "Error" in l), "") if "\n" in out else out
                what = "the tests no longer run" if DID_NOT_RUN in broke else ", ".join(broke[:3])
                messages.append(f"RED after saving {names}: {what}"
                                + (f"\n      {first.strip()[:110]}" if first else ""))
            if DID_NOT_RUN in healed:
                messages.append("the tests run again")
                healed.remove(DID_NOT_RUN)
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
        runner = js_runner(self.repo)
        if runner:
            js = sorted(f for f in js_import_graph(self.repo) if is_js_test(f))
            jp, jf, _ = run_js_tests(self.repo, js, runner, timeout=600)
            self.status |= {t: "pass" for t in jp} | {t: "fail" for t in jf}
        for f in review(self.repo):
            self.findings.setdefault(f.where.split(":")[0], set()).add(f"{f.code}: {f.title}")
        for f in [*self.graph, *(_js_files(self.repo) if runner else [])]:
            self.findings.setdefault(str(f.resolve().relative_to(self.repo)), set())


# ------------------------------------------------------------ TypeScript / JavaScript

JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
# Matched against the lexer's masked view, where string contents are blanked but
# the quotes stay put, so an import written inside a string or comment is not one.
_SPEC = re.compile(r"""(?:\bfrom\s*|\bimport\s*|\brequire\(\s*|\bimport\(\s*)(['"])""")


def js_runner(repo: Path) -> str | None:
    """vitest or jest, from package.json. None means no JavaScript tests to run."""
    pkg = Path(repo) / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf8"))
    except (json.JSONDecodeError, OSError):
        return None
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    for name in ("vitest", "jest"):
        if name in deps:
            return name
    return None


def is_js_test(path: Path) -> bool:
    n = path.name
    return any(n.endswith(f".{kind}{suf}") for kind in ("test", "spec") for suf in JS_SUFFIXES) \
        or "__tests__" in path.parts


def _js_files(repo: Path) -> list[Path]:
    return [p for p in repo.rglob("*") if p.suffix in JS_SUFFIXES and p.is_file()
            and not p.name.endswith(".d.ts") and not skipped(p, repo)]


def _resolve_js(importer: Path, spec: str) -> Path | None:
    base = (importer.parent / spec)
    stem = base.with_suffix("") if base.suffix in (".js", ".mjs", ".cjs", ".jsx") else base
    candidates = [base] + [stem.with_suffix(s) for s in JS_SUFFIXES] + \
                 [stem / f"index{s}" for s in JS_SUFFIXES]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def js_import_graph(repo: Path) -> dict[Path, set[Path]]:
    """file -> relative imports it makes. Comments are stripped by the lexer first,
    so an import in a comment does not create an edge."""
    from .patterns.smells_ts import lex

    graph: dict[Path, set[Path]] = {}
    for f in _js_files(repo):
        try:
            lexed = lex(f.read_text(encoding="utf8", errors="ignore"))
        except Exception:
            graph[f.resolve()] = set()
            continue
        literal_at = {t.start: t.text for t in lexed.strings if t.kind == "quote"}
        deps = set()
        for m in _SPEC.finditer(lexed.masked):
            spec = literal_at.get(m.start(1), "")
            target = _resolve_js(f, spec) if spec.startswith(("./", "../")) else None
            if target:
                deps.add(target)
        graph[f.resolve()] = deps
    return graph


def affected_js_tests(repo: str | Path, changed: str | Path,
                      graph: dict[Path, set[Path]] | None = None) -> list[Path]:
    repo = Path(repo).resolve()
    changed = (repo / changed).resolve() if not Path(changed).is_absolute() else Path(changed).resolve()
    graph = graph if graph is not None else js_import_graph(repo)
    if is_js_test(changed):
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
            (tests.add if is_js_test(importer) else queue.append)(importer)
    return sorted(tests)


def run_js_tests(repo: Path, tests: list[Path], runner: str,
                 timeout: int = 180) -> tuple[set[str], set[str], str]:
    """(passed, failed, first failure line) from vitest --reporter=json or jest --json.

    Both emit the same schema: testResults[].assertionResults[] with fullName and
    status. Node ids are 'path::full name'.
    """
    if not tests or runner not in ("vitest", "jest"):
        return set(), set(), ""
    repo = repo.resolve()
    files = [str(t.resolve().relative_to(repo)) for t in tests]
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        # The report goes to a file: a test's console.log also lands on stdout,
        # and a brace in it would be mistaken for the start of the JSON.
        argv = (["npx", "--no-install", "vitest", "run", "--reporter=json",
                 f"--outputFile={report}", *files] if runner == "vitest"
                else ["npx", "--no-install", "jest", "--json", f"--outputFile={report}", *files])
        try:
            r = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=timeout,
                               env={**fresh_env(), "CI": "1", "FORCE_COLOR": "0"})
        except (subprocess.TimeoutExpired, OSError) as exc:
            return set(), {DID_NOT_RUN}, f"{runner} did not run: {exc}"
        try:
            data = json.loads(report.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            tail = (r.stderr or r.stdout).strip().splitlines()
            return set(), {DID_NOT_RUN}, tail[-1][:160] if tail else f"{runner} did not produce a report"
    passed, failed, first = set(), set(), ""
    for suite in data.get("testResults", []):
        try:
            rel = str(Path(suite.get("name", "")).resolve().relative_to(repo))
        except ValueError:
            rel = suite.get("name", "")
        for a in suite.get("assertionResults", []):
            node = f"{rel}::{a.get('fullName') or a.get('title')}"
            if a.get("status") == "passed":
                passed.add(node)
            elif a.get("status") == "failed":
                failed.add(node)
                if not first and a.get("failureMessages"):
                    first = a["failureMessages"][0].strip().splitlines()[0][:160]
        if suite.get("status") == "failed" and not suite.get("assertionResults"):
            failed.add(f"{rel}::(suite failed to load)")
            lines = (suite.get("message") or "").strip().splitlines()
            first = first or (lines[0][:160] if lines else "")
    passed.add(DID_NOT_RUN)
    return passed, failed, first


def snapshot(repo: Path) -> dict[Path, float]:
    files = _py_files(repo) + (_js_files(repo) if js_runner(repo) else [])
    return {p: p.stat().st_mtime for p in files if p.exists()}


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
    real = {k: v for k, v in state.status.items() if k != DID_NOT_RUN}
    red = sum(1 for v in real.values() if v == "fail")
    out(f"  ready: {len(real)} tests, {red} already red"
        + (", and the tests do not currently run" if state.status.get(DID_NOT_RUN) == "fail" else "")
        + ". Quiet unless that changes.")

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
