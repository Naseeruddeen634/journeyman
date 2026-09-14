"""State for one engineering job.

The graph nodes are thin. Everything they compute lands here, and the graph's
routing conditions read values from here rather than parsing what an agent
wrote. That is what makes the loop a real control-flow decision instead of a
model being asked nicely to stop.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .evalset.grade import Report
from .evolve.optimizer import Candidate


@dataclass
class Job:
    goal: str
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    started: float = field(default_factory=time.time)

    # what exists in the world, which decides what patterns are even available
    resources: dict[str, bool] = field(default_factory=dict)

    # architect
    patterns: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    design_notes: list[str] = field(default_factory=list)

    # recall
    recalled: list[dict] = field(default_factory=list)
    priority_repairs: list[tuple[str, str, float]] = field(default_factory=list)

    # harness
    cases_train: list[dict] = field(default_factory=list)
    cases_heldout: list[dict] = field(default_factory=list)

    # evolution
    genome: dict[str, str] = field(default_factory=dict)
    baseline: Candidate | None = None
    history: list[Candidate] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    generation: int = 0
    max_generations: int = 8
    stalled: bool = False

    # outcome
    baseline_heldout: Report | None = None
    shipped: Candidate | None = None
    shipped_heldout: Report | None = None
    best_train: Candidate | None = None
    best_train_heldout: Report | None = None
    overfit_rejected: bool = False
    verdict: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    lessons: list[tuple[str, str, str, float]] = field(default_factory=list)

    log: list[str] = field(default_factory=list)

    # ---- the graph branches on these -------------------------------

    @property
    def keep_evolving(self) -> bool:
        return (not self.stalled) and self.generation < self.max_generations

    @property
    def current(self) -> Candidate | None:
        return self.history[-1] if self.history else self.baseline

    def note(self, msg: str) -> None:
        self.log.append(msg)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "goal": self.goal,
            "resources": self.resources,
            "patterns": self.patterns,
            "rejected": self.rejected,
            "design_notes": self.design_notes,
            "recalled": self.recalled,
            "priority_repairs": [list(p) for p in self.priority_repairs],
            "n_train": len(self.cases_train),
            "n_heldout": len(self.cases_heldout),
            "generations": [
                {"generation": c.generation, "train": round(c.score, 4),
                 "changed": f"{c.changed_slot}={c.changed_to}" if c.changed_slot else "baseline",
                 "reason": c.reason}
                for c in ([self.baseline] if self.baseline else []) + self.history
            ],
            "candidates_tried": len(self.ledger),
            "ledger": self.ledger,
            "baseline_heldout": round(self.baseline_heldout.score, 4) if self.baseline_heldout else None,
            "shipped_heldout": round(self.shipped_heldout.score, 4) if self.shipped_heldout else None,
            "best_train": round(self.best_train.score, 4) if self.best_train else None,
            "best_train_heldout": round(self.best_train_heldout.score, 4) if self.best_train_heldout else None,
            "overfit_rejected": self.overfit_rejected,
            "verdict": self.verdict,
            "artifacts": self.artifacts,
            "lessons": [list(l) for l in self.lessons],
            "elapsed_s": round(time.time() - self.started, 2),
        }
