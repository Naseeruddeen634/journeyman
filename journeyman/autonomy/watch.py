"""Standing watch.

`shift` does one task. `watch` keeps doing them: survey, work the top item,
sleep, look again. This is the difference between a command you run and a
colleague who is around.

It stops itself. Consecutive shifts that fix nothing mean the queue is stale or
the agent is out of its depth, and grinding on is how you wake up to forty
branches. Better to stop and leave a note.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .guardrails import Budget
from .scout import describe, survey
from .shift import ShiftResult, report, work_one


def _signature(task) -> str:
    """Identifies a task across shifts, so the watch does not redo it."""
    return f"{task.kind}:{task.where}:{task.title[:60]}"


@dataclass
class WatchLog:
    started: float = field(default_factory=time.time)
    shifts: list[ShiftResult] = field(default_factory=list)
    stopped_because: str = ""

    @property
    def fixed(self) -> int:
        return sum(1 for s in self.shifts if s.outcome in ("fixed", "fixed_with_concerns"))

    @property
    def needs_a_look(self) -> list:
        return [s for s in self.shifts if s.concerns]

    def to_dict(self) -> dict:
        return {
            "started": self.started,
            "hours": round((time.time() - self.started) / 3600, 2),
            "shifts": len(self.shifts),
            "fixed": self.fixed,
            "stopped_because": self.stopped_because,
            "detail": [s.to_dict() for s in self.shifts],
        }


def stand_watch(
    repo: str | Path,
    max_shifts: int = 6,
    max_hours: float = 8.0,
    interval_s: float = 60.0,
    stop_after_barren: int = 2,
    on_shift=None,
) -> WatchLog:
    """Work the queue until it is empty, the budget runs out, or it stops helping.

    ``stop_after_barren`` is the important one: after this many consecutive
    shifts that change nothing, it stands down rather than thrashing.
    """
    repo = Path(repo).resolve()
    log = WatchLog()
    barren = 0
    deadline = time.time() + max_hours * 3600

    # Each shift branches fresh from the current HEAD, so a fix made in shift 1
    # is not present when shift 2 surveys. Without this, the watch attempts the
    # same failing test all night and hands you five branches for one bug.
    attempted: set[str] = set()

    while True:
        if len(log.shifts) >= max_shifts:
            log.stopped_because = f"reached the limit of {max_shifts} shifts"
            break
        if time.time() > deadline:
            log.stopped_because = f"reached the time limit of {max_hours}h"
            break

        queue = [t for t in survey(repo) if _signature(t) not in attempted]
        if not queue:
            log.stopped_because = (
                "nothing left to do" if not attempted
                else f"worked every item in the queue ({len(attempted)} attempted)"
            )
            break

        task = queue[0]
        attempted.add(_signature(task))
        result = work_one(repo, task=task, budget=Budget())
        log.shifts.append(result)
        if on_shift:
            on_shift(result)

        if result.outcome in ("fixed", "fixed_with_concerns"):
            barren = 0
        else:
            barren += 1
            if barren >= stop_after_barren:
                log.stopped_because = (
                    f"{barren} shifts in a row changed nothing. Standing down rather "
                    "than thrashing. The remaining work probably needs you."
                )
                break

        if time.time() < deadline and len(log.shifts) < max_shifts:
            time.sleep(interval_s)

    return log


def morning_report(log: WatchLog) -> str:
    """What you read with coffee."""
    lines = ["", "=" * 72,
             f"  OVERNIGHT: {log.fixed} fixed out of {len(log.shifts)} shift(s), "
             f"{round((time.time() - log.started) / 3600, 1)}h",
             "=" * 72, ""]
    if not log.shifts:
        lines += ["  Nothing needed doing.", ""]
    for s in log.shifts:
        mark = {"fixed": "[fixed]  ", "fixed_with_concerns": "[CHECK]  ",
                "stuck": "[stuck]  ", "refused": "[refused]",
                "error": "[error]  ", "no_work": "[none]   "}[s.outcome]
        title = s.task.title[:52] if s.task else "-"
        lines.append(f"  {mark} {title}")
        if s.branch:
            lines.append(f"            {s.branch}")
        if s.concerns:
            lines.append(f"            unsure: {s.concerns[0][:52]}")
        if s.outcome == "stuck" and s.summary:
            lines.append(f"            {s.summary.splitlines()[-1][:58]}")
        lines.append("")
    if log.stopped_because:
        lines += [f"  Stopped: {log.stopped_because}", ""]
    if log.needs_a_look:
        lines += [f"  {len(log.needs_a_look)} change(s) the agent was unsure about. "
                  "Read those diffs first.", ""]
    branches = [s.branch for s in log.shifts
                if s.outcome in ("fixed", "fixed_with_concerns")]
    if branches:
        lines += ["  Review:", ""]
        lines += [f"    git diff main..{b}" for b in branches]
        lines.append("")
    return "\n".join(lines)
