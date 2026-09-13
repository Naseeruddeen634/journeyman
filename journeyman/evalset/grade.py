"""Deterministic grading.

Nothing here asks a language model whether an answer is good. Every field is
compared against a value that existed before the document did, using rules
written down in advance. That is what makes a score movement mean something.

The grader also clusters failures, because "62%" is not actionable and
"every pre-2000 date is wrong" is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FIELDS = ("vendor", "date", "total", "currency", "po_number")


# ---------------------------------------------------------------- compare


def _norm_text(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.,\-]", "", str(v))
    if not s:
        return None
    # "1.234,56" (European) vs "1,234.56" (Anglo): the last separator wins
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") \
            else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s) - s.rfind(",") - 1 == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def field_correct(name: str, predicted: Any, truth: Any) -> bool:
    """One field, one boolean. The comparison rule depends on the field."""
    if truth is None:
        return predicted is None or predicted == ""
    if predicted is None or predicted == "":
        return False
    if name == "total":
        p = _as_float(predicted)
        return p is not None and abs(p - float(truth)) < 0.005
    if name == "date":
        return str(predicted).strip()[:10] == str(truth).strip()[:10]
    if name == "currency":
        return str(predicted).strip().upper() == str(truth).strip().upper()
    if name == "po_number":
        return re.sub(r"\s+", "", str(predicted)).upper() == re.sub(r"\s+", "", str(truth)).upper()
    return _norm_text(predicted) == _norm_text(truth)


# ----------------------------------------------------------------- result


@dataclass
class CaseResult:
    case_id: str
    correct: dict[str, bool]
    predicted: dict[str, Any]
    truth: dict[str, Any]
    error: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def score(self) -> float:
        return sum(self.correct.values()) / len(self.correct) if self.correct else 0.0

    @property
    def perfect(self) -> bool:
        return bool(self.correct) and all(self.correct.values())


@dataclass
class Report:
    """A score plus everything needed to act on it."""

    results: list[CaseResult]
    label: str = ""

    @property
    def n(self) -> int:
        return len(self.results)

    @property
    def score(self) -> float:
        """Mean field-level accuracy. The headline number."""
        return sum(r.score for r in self.results) / self.n if self.n else 0.0

    @property
    def exact_match(self) -> float:
        """Share of documents where every field is right."""
        return sum(r.perfect for r in self.results) / self.n if self.n else 0.0

    @property
    def crashed(self) -> int:
        return sum(1 for r in self.results if r.error)

    def by_field(self) -> dict[str, float]:
        out = {}
        for f in FIELDS:
            vals = [r.correct[f] for r in self.results if f in r.correct]
            out[f] = sum(vals) / len(vals) if vals else 0.0
        return out

    def failure_clusters(self) -> list[dict]:
        """Group failures by what they have in common.

        This is the part the optimiser actually needs. A mutation aimed at
        "dates written day-first" is a different thing from a mutation aimed at
        "the score is 62%".
        """
        clusters: dict[tuple[str, str], list[str]] = {}
        for r in self.results:
            for f, ok in r.correct.items():
                if ok:
                    continue
                tag = self._tag(f, r)
                clusters.setdefault((f, tag), []).append(r.case_id)

        out = [
            {
                "field": f,
                "pattern": tag,
                "count": len(ids),
                "share_of_all_failures": 0.0,
                "example_ids": ids[:4],
            }
            for (f, tag), ids in clusters.items()
        ]
        total = sum(c["count"] for c in out) or 1
        for c in out:
            c["share_of_all_failures"] = round(c["count"] / total, 3)
        return sorted(out, key=lambda c: -c["count"])

    @staticmethod
    def _tag(fieldname: str, r: CaseResult) -> str:
        """Name the shape of one failure in words a person can act on."""
        if r.error:
            return "extractor raised an exception"
        pred, truth = r.predicted.get(fieldname), r.truth.get(fieldname)
        if pred in (None, ""):
            return "returned nothing"
        if fieldname == "date":
            styles = {0: "ISO yyyy-mm-dd", 1: "day-first dd/mm/yyyy", 2: "15 Mar 2024",
                      3: "March 15, 2024", 4: "15-Mar-24", 5: "dotted dd.mm.yyyy"}
            s = styles.get(r.meta.get("date_style"), "unknown format")
            if truth and str(truth) < "2000":
                return f"{s}, and the year is before 2000"
            return s
        if fieldname == "total":
            styles = {0: "symbol prefix", 1: "code prefix", 2: "code suffix",
                      3: "European 1.234,56", 4: "symbol with space"}
            p = _as_float(pred)
            t = _as_float(truth)
            if p is not None and t is not None and p < t:
                return f"{styles.get(r.meta.get('amount_style'),'?')}, picked a line item instead of the total"
            return styles.get(r.meta.get("amount_style"), "unknown amount format")
        if fieldname == "vendor":
            return f"template {r.meta.get('template','?')}"
        return f"template {r.meta.get('template','?')}"

    def summary(self) -> str:
        bf = self.by_field()
        parts = "  ".join(f"{k}={v:.0%}" for k, v in bf.items())
        return (f"{self.label or 'report'}: score {self.score:.1%}  "
                f"exact {self.exact_match:.1%}  n={self.n}  [{parts}]")

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "n": self.n,
            "score": round(self.score, 4),
            "exact_match": round(self.exact_match, 4),
            "crashed": self.crashed,
            "by_field": {k: round(v, 4) for k, v in self.by_field().items()},
            "failure_clusters": self.failure_clusters()[:8],
        }


def grade(predictions: list[dict], cases: list[dict], label: str = "") -> Report:
    """Grade predictions against the answer key."""
    by_id = {c["id"]: c for c in cases}
    results = []
    for pred in predictions:
        case = by_id.get(pred.get("id"))
        if case is None:
            continue
        got = pred.get("output") or {}
        truth = case["truth"]
        results.append(
            CaseResult(
                case_id=case["id"],
                correct={f: field_correct(f, got.get(f), truth.get(f)) for f in FIELDS},
                predicted={f: got.get(f) for f in FIELDS},
                truth=truth,
                error=pred.get("error", ""),
                meta=case.get("meta", {}),
            )
        )
    return Report(results=results, label=label)
