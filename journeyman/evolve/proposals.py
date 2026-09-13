"""Where new strategies come from.

The built-in library in `strategies.py` is a fixed search space. The interesting
mutations come from a model, which is what `llm_proposer` provides: it is shown
the current failure clusters and asked to write a replacement implementation.

That path is also where the danger lives. A model asked to raise a score on a
visible set of cases will sometimes raise it by memorising those cases rather
than by solving the task. This is a documented failure mode of LLM-driven
optimisation, not a hypothetical, and it is the reason Journeyman holds back half
the eval set and selects on that half instead of on the training score.

`memorising_proposer` reproduces that failure deterministically so the guard can
be demonstrated and tested without needing a model to misbehave on cue. It is
labelled as a simulation everywhere it is used, including in the demo output.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from ..evalset.grade import Report

Proposal = tuple[str, str, str]  # (slot, name, source)


def memorising_proposer(cases_train: list[dict]) -> Callable[[dict, Report], list[Proposal]]:
    """A proposer that cheats, the way a model sometimes does.

    It writes an implementation that hard-codes the answers for the documents it
    was shown. That scores perfectly on the training half and learns nothing, so
    it collapses on data it has not seen. Exactly the thing the held-out split is
    there to catch.
    """

    def propose(genome: dict, report: Report) -> list[Proposal]:
        lookup = {}
        for case in cases_train:
            key = re.sub(r"\s+", " ", case["document"])[:60]
            lookup[key] = case["truth"]["total"]
        src = f'''
_MEMO = {json.dumps(lookup)}


def _total(doc, lines):
    """Look the answer up. Falls back to the largest number when it has not
    seen this document before, which is most of the time in production."""
    import re as _re
    key = _re.sub(r"\\s+", " ", doc)[:60]
    if key in _MEMO:
        return _MEMO[key]
    vals = [_num(a) for a in _re.findall(r"[\\d.,]{{3,}}", doc)]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None
'''
        return [("total", "model_proposed_lookup", src)]

    return propose


def llm_proposer(agent) -> Callable[[dict, Report], list[Proposal]]:
    """Ask a model for a replacement implementation aimed at the worst cluster.

    The agent is any callable returning text. Whatever it returns is compiled and
    measured like every other candidate: a proposal that will not compile simply
    scores zero and loses.
    """

    def propose(genome: dict, report: Report) -> list[Proposal]:
        clusters = report.failure_clusters()[:3]
        if not clusters:
            return []
        worst = clusters[0]
        slot = worst["field"]
        prompt = (
            "You are improving one function in a Python invoice extractor.\n\n"
            f"The failing field is `{slot}`. The failure pattern, measured over "
            f"{report.n} documents:\n"
            + "\n".join(f"  - {c['count']} cases: {c['pattern']}" for c in clusters)
            + f"\n\nWrite a replacement for `_{_slot_fn(slot)}` only. Available helpers: "
            "`_num(s)` parses amounts in both 1,234.56 and 1.234,56 form, and `MONTHS` "
            "maps 'jan'..'dec' to 1..12. Use the same signature as the current "
            "implementation. Return only Python source, no prose, no code fences.\n\n"
            "Do not special-case or hard-code any specific document. A solution that "
            "memorises examples will be rejected when it is scored on documents it "
            "has not seen."
        )
        try:
            text = str(agent(prompt))
        except Exception:
            return []
        source = _strip_fences(text)
        if f"def _{_slot_fn(slot)}" not in source:
            return []
        return [(slot, "model_proposed", source)]

    return propose


def _slot_fn(slot: str) -> str:
    return {"po_number": "po"}.get(slot, slot)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            body = parts[1]
            return body.split("\n", 1)[1] if body.lower().startswith("python") else body
    return text
