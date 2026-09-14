"""An independent check of a change that made the tests pass.

On the hard benchmark, four of five fixes were delivered wrong. All four had the
same shape: the task and the docstring described two behaviours, the visible
test covered one, the agent fixed that one, the suite went green, and nothing
looked further. The cosine fix corrected the normaliser and still divided by
zero on a zero vector, which the docstring and the task title both mention.

A green suite only says the tests that exist pass. So after the worker says
DONE, a checker writes tests for what the change is supposed to do. The
checker is deliberately kept apart from the worker, the way a reviewer is:

  - it is a fresh model context with no tools and no view of the worker's
    reasoning or of the changed code, only the task and each changed function's
    signature and docstring as they were before the change
  - its tests are written to a file the worker never sees and cannot edit, run
    confined, and deleted before anything is committed
  - each failing test is also run against the code before the change, which
    separates "your change broke a documented behaviour" (it passed before) from
    "this is not fixed yet, or the check is wrong" (it failed before too)

A model writing tests can be wrong, so the worker is told it may push back
rather than bend correct code to a wrong test, and whatever still fails at the
end is reported as a concern with the test's own words, not silently dropped.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SPEC_FILE = "_journeyman_spec_test.py"

CHECKER_SYSTEM = """You write tests that check whether code does what its documentation says.
You are not shown the implementation. Work out each expected value by hand from
the documentation, using small concrete inputs. Test only what the documentation
and the task state; never test behaviour they do not mention. Reply with one
python code block containing pytest test functions and nothing else."""

FAIL_LINE = re.compile(r"^FAILED \S*::(\w+)(?: - (.*))?$", re.M)
PASS_LINE = re.compile(r"^PASSED \S*::(\w+)", re.M)
# --tb=line prints one "path:lineno: message" line per failure, in order
TB_LINE = re.compile(r"^(/.*?|[^\s:]+\.py):(\d+): (.*)$", re.M)
ASSERTION = re.compile(r"^(AssertionError|assert |Failed: DID NOT RAISE|AssertionError:|"
                       r"Regex pattern did not match|.*\bassert\b)")


@dataclass
class FunctionSpec:
    file: str
    module: str
    qualname: str
    signature: str
    docstring: str
    context: str = ""        # module-level names the function uses, e.g. TRANSIENT = (...)


@dataclass
class SpecRun:
    passed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)      # test name -> first failure line
    ignored: dict[str, str] = field(default_factory=dict)     # failures that say nothing about the code
    error: str = ""                                          # the check itself is unusable
    output: str = ""


def counts(location: str, message: str, tree: Path) -> bool:
    """Is this failure evidence about the code under test?

    Measured on the benchmark's reference solutions, most checker failures were not:
    OpenAIError "Missing credentials" raised inside the openai package (the function
    needs a live client, and the jail has no network), NameError for a pytest the
    checker forgot to import, a ValueError thrown by a helper the test itself defined.
    What does count: an assertion in the check, or any exception raised inside the
    repository's own code, like the ZeroDivisionError in lib/vectors.py that was the
    bug.
    """
    path = Path(location)
    if path.name == SPEC_FILE:
        return bool(ASSERTION.match(message))
    try:
        rel = path.resolve().relative_to(tree.resolve()) if path.is_absolute() else path
    except ValueError:
        return False
    return not any(part in ("site-packages", ".venv", "venv", "node_modules") for part in rel.parts)


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    from .contract import _functions as walk
    return walk(tree)


def _signature(fn: ast.AST) -> str:
    stub = ast.FunctionDef(name=fn.name, args=fn.args, body=[ast.Expr(ast.Constant(...))],
                           decorator_list=[], returns=fn.returns, type_comment=None,
                           type_params=getattr(fn, "type_params", []))
    prefix = "async " if isinstance(fn, ast.AsyncFunctionDef) else ""
    return prefix + ast.unparse(ast.fix_missing_locations(stub)).split("\n")[0]


def referenced_constants(module: ast.Module, fn: ast.AST, source: str, limit: int = 600) -> str:
    """Module-level constants the checker needs to read the docstring right.

    Without TRANSIENT = (TimeoutError, ConnectionError) the checker guessed that
    "transient" meant any Exception, and every test it wrote for a correct retry
    wrapper failed. The buggy function did not even reference TRANSIENT, so names the
    function uses are not enough: UPPER_CASE module constants are the module's
    vocabulary, and they are included too.
    """
    used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    lines = []
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and (t.id in used or t.id.isupper()) for t in targets):
                seg = ast.get_source_segment(source, node) or ""
                if len(seg) <= 200:
                    lines.append(seg)
    return "\n".join(lines)[:limit]


def module_for(rel: str) -> str:
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_test(rel: str) -> bool:
    p = Path(rel)
    return p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts \
        or p.name == "conftest.py"


def changed_functions(root: Path, files: list[str], limit: int = 3) -> list[FunctionSpec]:
    """Functions whose body the change touched, described as they were before it."""
    out: list[FunctionSpec] = []
    for rel in files:
        if not rel.endswith(".py") or _is_test(rel) or rel == SPEC_FILE:
            continue
        path = Path(root) / rel
        head = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True, text=True)
        if head.returncode != 0 or not path.exists():
            continue
        try:
            head_tree = ast.parse(head.stdout)
            before = _functions(head_tree)
            after = _functions(ast.parse(path.read_text(encoding="utf8", errors="ignore")))
        except SyntaxError:
            continue
        for name, b_fn in before.items():
            a_fn = after.get(name)
            if a_fn is None or ast.dump(b_fn) == ast.dump(a_fn):
                continue
            doc = ast.get_docstring(b_fn) or ""
            if name.split(".")[-1].startswith("_") and not doc:
                continue
            out.append(FunctionSpec(rel, module_for(rel), name, _signature(b_fn), doc,
                                    referenced_constants(head_tree, b_fn, head.stdout)))
    documented = [s for s in out if s.docstring]
    # A promise has to be written down somewhere. With one changed function the task
    # title can stand in for a missing docstring; with several it cannot be attributed.
    return (documented or (out if len(out) == 1 else []))[:limit]


def checker_prompt(title: str, detail: str, specs: list[FunctionSpec]) -> str:
    parts = [f"Someone was asked to fix this:\n  {title}\n"]
    if detail and detail.strip() != title.strip():
        parts.append(f"More detail:\n  {detail.strip()[:600]}\n")
    parts.append("They changed the functions below. This is how each was documented before "
                 "the change:\n")
    for s in specs:
        owner, _, fn = s.qualname.rpartition(".")
        how = (f"from {s.module} import {owner}   # {fn} is a method of {owner}" if owner
               else f"from {s.module} import {fn}")
        if s.context:
            parts.append(f"# defined in {s.module}:\n{s.context}")
        parts.append(f"{how}\n{s.signature}\n    \"\"\"{s.docstring or '(no docstring)'}\"\"\"\n")
    parts.append("Write at most five pytest test functions: one for each problem the task names and "
                 "one for each behaviour the docstring states, so that they pass on a correct "
                 "implementation. Use the smallest input that shows each behaviour. Above each "
                 "assert, write the expected value's working in a comment, step by step, using the "
                 "exact rules above (for example the default count function). Use pytest.approx for "
                 "floats. Import exactly as shown. Do not read files, use the network, or sleep.")
    return "\n".join(parts)


class _ApproxFloats(ast.NodeTransformer):
    """`x == 1.0` -> `x == pytest.approx(1.0)`. The checker was told to use approx and
    still wrote `cosine(a, b) == -1.0`, which is false for a correct cosine. For a float
    literal, approximate equality is what the assertion means."""

    changed = False

    @staticmethod
    def _float(node: ast.AST) -> bool:
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            node = node.operand
        return isinstance(node, ast.Constant) and isinstance(node.value, float)

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            wrap = lambda v: ast.Call(ast.Attribute(ast.Name("pytest", ast.Load()), "approx", ast.Load()), [v], [])
            if self._float(node.comparators[0]):
                node.comparators[0] = wrap(node.comparators[0])
                self.changed = True
            elif self._float(node.left):
                node.left = wrap(node.left)
                self.changed = True
        return node


def approx_floats(code: str) -> str:
    """Also adds `import pytest` when the tests use it without importing it, which the
    checker did for a whole file of pytest.approx calls."""
    tree = ast.parse(code)
    t = _ApproxFloats()
    tree = t.visit(tree)
    uses = any(isinstance(n, ast.Name) and n.id == "pytest" for n in ast.walk(tree))
    imported = any(isinstance(n, ast.Import) and any(a.name == "pytest" for a in n.names)
                   for n in tree.body)
    if uses and not imported:
        tree.body.insert(0, ast.Import([ast.alias("pytest")]))
    if not t.changed and (imported or not uses):
        return code
    return ast.unparse(ast.fix_missing_locations(tree))


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text or "", re.S)
    candidates = sorted(blocks, key=len, reverse=True) or [text or ""]
    for code in candidates:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
               for n in ast.walk(tree)):
            return approx_floats(code)
    return None


def _pytest(tree: Path, code: str, timeout: int = 120, python: str | None = None) -> SpecRun:
    from . import jail
    from .scout import _interpreter, fresh_env

    target = tree / SPEC_FILE
    target.write_text(code, encoding="utf8")
    env = fresh_env({"COLUMNS": "400"})      # pytest truncates the failure summary to the terminal width
    if (tree / "src").is_dir():
        env["PYTHONPATH"] = str(tree / "src") + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    argv, env, _ = jail.wrap([python or _interpreter(tree), "-m", "pytest", "-q", "-rA", "--no-header",
                              "--tb=line", "-p", "no:cacheprovider", SPEC_FILE], tree, env)
    run = SpecRun()
    try:
        r = subprocess.run(argv, cwd=tree, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        run.error = "the independent check timed out"
        return run
    finally:
        target.unlink(missing_ok=True)
    out = r.stdout + r.stderr
    run.output = out[-3000:]
    run.passed = PASS_LINE.findall(out)
    names = [(name, (msg or "").strip()[:200]) for name, msg in FAIL_LINE.findall(out)]
    failures = re.search(r"^=+ FAILURES =+$(.*?)^=+ ", out, re.M | re.S)
    locations = TB_LINE.findall(failures.group(1)) if failures else []
    for i, (name, msg) in enumerate(names):
        where, message = (locations[i][0], locations[i][2]) if i < len(locations) else ("", msg)
        if where and counts(where, message, tree):
            run.failed[name] = msg
        else:
            run.ignored[name] = msg
    if r.returncode not in (0, 1) or ("error" in out.lower() and not run.passed and not run.failed
                                      and not run.ignored):
        run.error = "the independent check could not be collected: " + (out.strip().splitlines() or ["?"])[-1][:200]
    return run


def run_against_change(root: Path, code: str) -> SpecRun:
    return _pytest(Path(root), code)


def run_against_head(root: Path, code: str) -> SpecRun:
    """The same tests on the code as it was before the change.

    HEAD is exported to a temporary directory, which has no .venv, so it runs with
    the interpreter the change was tested with rather than whatever that directory
    would resolve to."""
    from .scout import _interpreter

    python = _interpreter(Path(root))
    with tempfile.TemporaryDirectory(prefix="journeyman-head-") as tmp:
        archive = Path(tmp) / "head.tar"
        made = subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"],
                              cwd=root, capture_output=True, text=True)
        if made.returncode != 0:
            return SpecRun(error="could not export HEAD")
        tree = Path(tmp) / "head"
        tree.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(tree, filter="data")
        return _pytest(tree, code, python=python)


@dataclass
class Verdict:
    usable: bool
    broke: dict[str, str] = field(default_factory=dict)       # passed before, fails now
    unfixed: dict[str, str] = field(default_factory=dict)     # failed before, fails now
    passed: int = 0
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.usable or not (self.broke or self.unfixed)

    def feedback(self) -> str:
        lines = ["An independent check wrote tests from the task and the documentation only; it "
                 "never saw your code. Some of them fail on your change."]
        if self.broke:
            lines.append("\nThese passed on the code before your change and fail now, so your "
                         "change broke documented behaviour:")
            lines += [f"  {name}: {msg}" for name, msg in self.broke.items()]
        if self.unfixed:
            lines.append("\nThese failed before your change and still fail, so what they describe "
                         "is not fixed yet:")
            lines += [f"  {name}: {msg}" for name, msg in self.unfixed.items()]
        lines.append("\nThe tests may be wrong. If one expects something the documentation and the "
                     "task do not actually promise, do not change correct code to satisfy it: reply "
                     "DONE and say in one sentence which test is wrong and why. Otherwise fix the code.")
        return "\n".join(lines)

    def concern(self) -> str:
        worst = next(iter(self.broke.items()), None) or next(iter(self.unfixed.items()))
        kind = "passed before the change and fails after it" if self.broke else "still fails"
        return (f"An independent test written from the docstring and the task {kind}: "
                f"{worst[0]}: {worst[1]}".rstrip(": "))


def judge(root: Path, code: str) -> Verdict:
    now = run_against_change(root, code)
    if now.error:
        return Verdict(False, note=now.error)
    if not now.failed:
        return Verdict(True, passed=len(now.passed))
    before = run_against_head(root, code)
    broke, unfixed = {}, {}
    for name, msg in now.failed.items():
        if not before.error and name in before.passed:
            broke[name] = msg
        else:
            unfixed[name] = msg
    return Verdict(True, broke, unfixed, len(now.passed))


def write_checks(ask, title: str, detail: str, specs: list[FunctionSpec], samples: int = 2) -> list[str]:
    """`samples` independent test files. `ask(prompt) -> reply` must be a fresh context each call."""
    prompt = checker_prompt(title, detail, specs)
    out = []
    for _ in range(samples):
        code = extract_code(ask(prompt))
        if code:
            out.append(code)
    return out


def judge_all(root: Path, codes: list[str]) -> Verdict:
    """A problem stands only when every independent check finds one.

    On the benchmark's reference solutions a single checker still wrote a wrong
    expected value for about one correct fix in four (a rounding remainder put on the
    wrong element, an overlap it counted by hand and got wrong). Two checkers writing
    the same wrong test independently is much rarer than one, while a behaviour the
    fix really missed is named in the task, so both checkers tend to test it.
    """
    verdicts = [judge(root, c) for c in codes]
    usable = [v for v in verdicts if v.usable]
    if len(usable) < 2:
        return Verdict(False, note="fewer than two usable independent checks")
    if any(v.ok for v in usable):
        return Verdict(True, passed=sum(v.passed for v in usable))
    merged = Verdict(True, passed=sum(v.passed for v in usable))
    for v in usable:
        merged.broke.update(v.broke)
        merged.unfixed.update({k: m for k, m in v.unfixed.items() if k not in merged.broke})
    return merged

