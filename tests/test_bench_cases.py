"""The benchmark has to be right before it can say anything about the agent.

For every case: the broken code must produce the task, the hidden oracle must
fail on the broken code, and the oracle must pass on a reference solution.
An oracle that fails for the wrong reason, such as an import error or a demand
the docstring never made, would score every correct fix as gamed.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman import bench  # noqa: E402
from journeyman.autonomy.scout import ai_engineering_review, survey  # noqa: E402
from journeyman.patterns.smells import review  # noqa: E402

CASES = sorted(d for d in bench.DEFAULT_CASES.iterdir() if (d / "case.json").exists())


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_case_is_valid(case, tmp_path):
    meta = json.loads((case / "case.json").read_text())
    repo = bench._materialise(case, tmp_path)

    if meta["kind"] == "failing_test":
        assert [t for t in survey(repo) if t.kind == "failing_test"], "no failing test to fix"
    else:
        assert [t for t in ai_engineering_review(repo) if t.meta.get("code") == meta["code"]], \
            f"{meta['code']} does not fire on the broken code"

    ok, out = bench._oracle(case, repo)
    assert not ok, f"oracle passes on broken code:\n{out}"

    shutil.copytree(case / "solution", repo, dirs_exist_ok=True)
    ok, out = bench._oracle(case, repo)
    assert ok, f"oracle rejects the reference solution:\n{out}"

    if meta["kind"] == "ai_review":
        target = [f for f in review(repo) if f.code == meta["code"]]
        assert not target, f"reference solution still triggers {meta['code']}"
