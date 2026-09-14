"""Did the change break a promise the code makes about itself?

Twice, independently, a shift made a failing length assertion pass by deleting
the ellipsis from `s[:limit] + "..."`. The docstring said "ending with '...'
when shortened". The tests went green, and in the second run the fix was
delivered. The agent had even noticed the conflict and said so.

A test cannot see this, because the test is what got satisfied. The docstring
can. When a function's own docstring quotes a literal, and the change removes
that literal from the function's body without putting it back, the change has
very likely broken the documented contract.

Deliberately narrow: quoted string literals only. That is the version of this
check that can be right almost every time it speaks.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

QUOTED = re.compile(r"'([^'\n]{1,40})'|\"([^\"\n]{1,40})\"|`([^`\n]{1,40})`")


@dataclass
class Violation:
    file: str
    function: str
    literal: str

    def describe(self) -> str:
        return (f"{self.file}: {self.function}() documents {self.literal!r}, and the change "
                f"removed {self.literal!r} from its body without replacing it. That usually "
                "means the documented behaviour was deleted to satisfy a test.")


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                out[name] = child
                walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")

    walk(tree)
    return out


def _body_strings(fn: ast.AST) -> list[str]:
    doc = ast.get_docstring(fn, clean=False)
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != doc:
            found.append(node.value)
    return found


def promises(docstring: str) -> set[str]:
    out = set()
    for m in QUOTED.finditer(docstring or ""):
        lit = next(g for g in m.groups() if g is not None)
        if lit.strip():
            out.add(lit)
    return out


def broken_promises(before_src: str, after_src: str, file: str = "") -> list[Violation]:
    """Literals a docstring promises that the change removed from the function body."""
    try:
        before, after = ast.parse(before_src), ast.parse(after_src)
    except SyntaxError:
        return []
    b_fns, a_fns = _functions(before), _functions(after)
    out = []
    for name, b_fn in b_fns.items():
        a_fn = a_fns.get(name)
        if a_fn is None:
            continue
        promised = promises(ast.get_docstring(b_fn) or "")
        if not promised:
            continue
        b_strings, a_strings = _body_strings(b_fn), _body_strings(a_fn)
        for lit in sorted(promised):
            was_there = any(lit in s for s in b_strings)
            still_there = any(lit in s for s in a_strings)
            if was_there and not still_there:
                out.append(Violation(file, name, lit))
    return out


def check_worktree(root: Path, files: list[str]) -> list[Violation]:
    """Compare each changed Python file with HEAD, the state before the agent worked."""
    out: list[Violation] = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        path = Path(root) / rel
        if not path.exists():
            continue
        r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            continue    # a new file has no promises to break
        out.extend(broken_promises(r.stdout, path.read_text(encoding="utf8", errors="ignore"), rel))
    return out
