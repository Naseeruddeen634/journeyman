"""The evolution loop.

The protocol is the ordinary honest one and it matters more than the search:

  - the eval set is split once, and the optimiser **never sees the held-out half**
  - every candidate is scored on train only
  - at the end, the surviving generations are scored on held-out, and the one
    that holds up is what ships

That last step is the whole product. An optimiser that reports its best training
score is reporting the number it overfit to. Journeyman reports the number you
will actually get, even when that number is worse and less impressive.

Search itself is failure-directed hill climbing. Each round reads the current
failure clusters, works out which slot they implicate, and tries the alternative
implementations for that slot. Targeting the search at the diagnosis is what
makes it converge in a handful of rounds instead of enumerating 144 genomes.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..evalset.grade import Report
from ..evalset.runner import evaluate_source
from .strategies import FIELD_TO_SLOT, SLOTS, assemble


@dataclass
class Candidate:
    genome: dict[str, str]
    train: Report
    generation: int
    changed_slot: str = ""
    changed_to: str = ""
    reason: str = ""

    @property
    def score(self) -> float:
        return self.train.score


@dataclass
class EvolutionResult:
    baseline: Candidate
    generations: list[Candidate]
    shipped: Candidate
    shipped_heldout: Report
    baseline_heldout: Report
    best_train: Candidate
    best_train_heldout: Report
    overfit_rejected: bool
    verdict: str
    ledger: list[dict] = field(default_factory=list)

    @property
    def candidates_tried(self) -> int:
        return len(self.ledger)

    @property
    def lessons(self) -> list[tuple[str, str, str, float]]:
        """(slot, failure pattern, repair, gain) for everything that worked."""
        out = []
        prev = self.baseline.score
        for c in self.generations[1:]:
            if c.changed_slot:
                pattern = c.reason.split(": ", 1)[-1] if ": " in c.reason else c.reason
                out.append((c.changed_slot, pattern, c.changed_to, round(c.score - prev, 4)))
            prev = c.score
        return out

    @property
    def improved(self) -> bool:
        return self.shipped_heldout.score > self.baseline_heldout.score + 1e-9

    def to_dict(self) -> dict:
        return {
            "baseline_train": round(self.baseline.score, 4),
            "baseline_heldout": round(self.baseline_heldout.score, 4),
            "shipped_train": round(self.shipped.score, 4),
            "shipped_heldout": round(self.shipped_heldout.score, 4),
            "best_train": round(self.best_train.score, 4),
            "best_train_heldout": round(self.best_train_heldout.score, 4),
            "overfit_rejected": self.overfit_rejected,
            "improved": self.improved,
            "verdict": self.verdict,
            "generations": [
                {
                    "generation": c.generation,
                    "train": round(c.score, 4),
                    "changed": f"{c.changed_slot}={c.changed_to}" if c.changed_slot else "baseline",
                    "reason": c.reason,
                }
                for c in self.generations
            ],
            "ledger": self.ledger,
        }


def split_cases(cases: list[dict], train_share: float = 0.5, seed: int = 7):
    """One split, made once, never revisited."""
    shuffled = list(cases)
    random.Random(seed).shuffle(shuffled)
    cut = int(len(shuffled) * train_share)
    return shuffled[:cut], shuffled[cut:]


def _targets(report: Report) -> list[tuple[str, str]]:
    """Which slots to attack, worst first, with the reason in plain English."""
    out: list[tuple[str, str]] = []
    seen = set()
    for cluster in report.failure_clusters():
        slot = FIELD_TO_SLOT.get(cluster["field"])
        if slot and slot not in seen:
            seen.add(slot)
            out.append((
                slot,
                f"{cluster['count']} cases fail on {cluster['field']}: {cluster['pattern']}",
            ))
    return out


def evolve(
    cases_train: list[dict],
    cases_heldout: list[dict],
    baseline_genome: dict[str, str],
    max_generations: int = 8,
    min_gain: float = 0.005,
    propose_extra=None,
    priority_repairs: list[tuple[str, str, float]] | None = None,
) -> EvolutionResult:
    """Hill-climb the genome on train, then select on held-out.

    ``propose_extra`` is the hook a model plugs into: given the current genome
    and its failure clusters it may return extra (slot, name, source) triples to
    widen the search beyond the built-in library. Offline it is simply absent.

    ``priority_repairs`` comes from memory: (slot, implementation, weight) that
    worked on a similar task before. They are tried first, which is the entire
    mechanism by which a second run on a related problem costs fewer generations
    than the first. Nothing is taken on trust: a recalled repair still has to win
    on measurement like any other candidate.
    """
    prio = {(slot, impl): w for slot, impl, w in (priority_repairs or [])}

    def _order(slot: str) -> list[str]:
        """Library order, except anything memory recommends comes first."""
        impls = list(SLOTS[slot])
        return sorted(impls, key=lambda i: (-prio.get((slot, i), 0.0), impls.index(i)))

    base_report = evaluate_source(assemble(baseline_genome), cases_train, "gen 0 (train)")
    current = Candidate(dict(baseline_genome), base_report, 0, reason="starting point")
    history = [current]
    ledger: list[dict] = []

    for gen in range(1, max_generations + 1):
        targets = _targets(current.train)
        if not targets:
            break

        best: Candidate | None = None
        for slot, reason in targets:
            for impl in _order(slot):
                if impl == current.genome.get(slot):
                    continue
                trial = dict(current.genome)
                trial[slot] = impl
                report = evaluate_source(assemble(trial), cases_train, f"gen {gen} (train)")
                ledger.append({
                    "generation": gen,
                    "tried": f"{slot}={impl}",
                    "train_score": round(report.score, 4),
                    "delta": round(report.score - current.score, 4),
                    "targeted": reason,
                    "from_memory": (slot, impl) in prio,
                })
                if best is None or report.score > best.score:
                    best = Candidate(trial, report, gen, slot, impl, reason)

                # Memory earns its keep here. If a repair that worked on a
                # similar task still works on this one, take it and stop
                # searching this slot. The saving is real evaluations, not a
                # claim: nothing is trusted without being measured first.
                if (slot, impl) in prio and report.score > current.score + min_gain:
                    best = Candidate(trial, report, gen, slot, impl,
                                     reason + " [recalled from a previous run]")
                    break

            if propose_extra is not None:
                for slot_name, impl_name, source in propose_extra(current.genome, current.train):
                    SLOTS.setdefault(slot_name, {})[impl_name] = source
                    trial = dict(current.genome)
                    trial[slot_name] = impl_name
                    report = evaluate_source(assemble(trial), cases_train, f"gen {gen} (train)")
                    ledger.append({
                        "generation": gen, "tried": f"{slot_name}={impl_name} (proposed)",
                        "train_score": round(report.score, 4),
                        "delta": round(report.score - current.score, 4),
                        "targeted": "model-proposed strategy",
                    })
                    if best is None or report.score > best.score:
                        best = Candidate(trial, report, gen, slot_name, impl_name, "model-proposed")

            if best is not None and best.score > current.score + min_gain:
                break  # took a step; re-diagnose before choosing the next target

        if best is None or best.score <= current.score + min_gain:
            break
        current = best
        history.append(current)

    # ---- selection. The held-out set is touched here for the first time. ----
    baseline_heldout = evaluate_source(
        assemble(baseline_genome), cases_heldout, "baseline (held out)"
    )
    scored = [
        (c, evaluate_source(assemble(c.genome), cases_heldout, f"gen {c.generation} (held out)"))
        for c in history
    ]
    best_train = max(history, key=lambda c: c.score)
    best_train_heldout = next(h for c, h in scored if c is best_train)
    shipped, shipped_heldout = max(scored, key=lambda pair: pair[1].score)

    overfit_rejected = shipped is not best_train
    if shipped_heldout.score <= baseline_heldout.score + 1e-9:
        shipped, shipped_heldout = history[0], baseline_heldout
        verdict = (
            "No reliable improvement. Nothing found here beats what you already had "
            "on data it had not seen, so your original is what you should keep."
        )
    elif overfit_rejected:
        verdict = (
            f"Generation {best_train.generation} scored {best_train.score:.1%} on the eval "
            f"set but only {best_train_heldout.score:.1%} on data it never saw. That is "
            f"overfitting, so it was rejected. Shipping generation {shipped.generation} "
            f"instead: {shipped.score:.1%} train, {shipped_heldout.score:.1%} held out."
        )
    else:
        verdict = (
            f"Shipping generation {shipped.generation}. "
            f"{baseline_heldout.score:.1%} to {shipped_heldout.score:.1%} on data it never "
            f"saw during the search."
        )

    return EvolutionResult(
        baseline=history[0],
        generations=history,
        shipped=shipped,
        shipped_heldout=shipped_heldout,
        baseline_heldout=baseline_heldout,
        best_train=best_train,
        best_train_heldout=best_train_heldout,
        overfit_rejected=overfit_rejected,
        verdict=verdict,
        ledger=ledger,
    )
