"""Running an artifact against eval cases.

An artifact is a Python file exposing `extract(document) -> dict`. It is loaded
from disk each time so a mutated variant can be measured without restarting
anything, and every call is wrapped, because the whole point is to run code that
might be wrong.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
import uuid
from pathlib import Path
from typing import Callable

from .grade import Report, grade


def load_artifact(path: str | Path) -> Callable[[str], dict]:
    """Import `extract` from a Python file. Each load gets a fresh module name."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no artifact at {path}")
    name = f"_ratchet_artifact_{uuid.uuid4().hex[:10]}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    fn = getattr(module, "extract", None)
    if not callable(fn):
        raise AttributeError(f"{path} does not define extract(document) -> dict")
    return fn


def load_artifact_from_source(source: str) -> Callable[[str], dict]:
    """Same, from a string. Used for candidate variants that are not on disk yet."""
    name = f"_ratchet_candidate_{uuid.uuid4().hex[:10]}"
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(name, loader=None)
    )
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    fn = module.__dict__.get("extract")
    if not callable(fn):
        raise AttributeError("candidate does not define extract(document) -> dict")
    return fn


def run(fn: Callable[[str], dict], cases: list[dict]) -> list[dict]:
    """Execute the artifact over every case. Never raises."""
    out = []
    for case in cases:
        try:
            result = fn(case["document"])
            out.append({"id": case["id"], "output": result if isinstance(result, dict) else {}})
        except Exception:
            out.append({
                "id": case["id"],
                "output": {},
                "error": traceback.format_exc(limit=2).strip().splitlines()[-1][:200],
            })
    return out


def evaluate(fn: Callable[[str], dict], cases: list[dict], label: str = "") -> Report:
    return grade(run(fn, cases), cases, label=label)


def evaluate_source(source: str, cases: list[dict], label: str = "") -> Report:
    """Compile and measure a candidate. A candidate that will not compile scores 0."""
    try:
        fn = load_artifact_from_source(source)
    except Exception as exc:
        return grade(
            [{"id": c["id"], "output": {}, "error": f"did not compile: {exc}"} for c in cases],
            cases, label=label,
        )
    return evaluate(fn, cases, label=label)
