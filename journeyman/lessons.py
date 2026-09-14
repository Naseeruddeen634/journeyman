"""What unattended shifts remember.

`build` already learns across runs. The shift loop, which does most of the real
work, did not: every AIE004 fix was worked out from scratch, in every repo,
every night.

After a shift delivers a clean fix (verified, and the shift raised no concerns
about its own change), this records what the problem looked like and how it was
fixed. Before the next shift, it looks for lessons about the same kind of
problem and puts at most two in the brief.

One boundary is deliberate. With a cloud model as the brain, anything from
repository A that appears in the prompt for repository B is sent to a provider
while working on a different team's code. So:

  - same repository: the lesson comes with its diff
  - another repository: only review-finding lessons cross (the knowledge is
    about the check, not the code), with the title reduced to the finding code
    and every identifier, path and quoted string scrubbed from the approach
  - a failing-test lesson never crosses repositories; its title and approach
    are made of that repository's names

An earlier version withheld only the diff and still leaked names through the
title and the agent's own summary.

Lessons are stored in ~/.journeyman/lessons.json and never leave the machine
except as part of a brief, under the rule above.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .home import HOME, ensure

PATH = HOME / "lessons.json"
STOP = {"the", "a", "an", "to", "of", "in", "is", "and", "or", "for", "on", "at", "be", "it",
        "assert", "error", "failed", "test", "tests", "py", "self", "none", "true", "false"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z_][a-z0-9_]{2,}", (text or "").lower()) if w not in STOP}


def _similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Lesson:
    repo: str
    kind: str
    code: str            # AIE code for review findings, "" for tests
    title: str
    signature: list[str]  # tokens describing the failure
    approach: str        # the agent's own one-line account of the fix
    diff: str
    ts: float = field(default_factory=time.time)
    uses: int = 0


def signature_for(task) -> list[str]:
    text = f"{task.title} {task.detail} {task.evidence[-1500:] if task.evidence else ''}"
    return sorted(_tokens(text))[:60]


IDENTIFIER = re.compile(
    r"`[^`]*`|'[^']*'|\"[^\"]*\""                      # quoted anything
    r"|[\w.-]+/[\w./-]+"                                 # paths
    r"|\b\w+\.(?:py|ts|tsx|js|json|ya?ml|txt|md)\b"       # file names
    r"|\b[a-z]+_[a-z0-9_]+\b"                            # snake_case
    r"|\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z]+[A-Z]\w*\b"        # camelCase, PascalCase
    r"|\b\w+\(\)")                                         # calls


def scrub(text: str) -> str:
    """Remove names that belong to one codebase, keeping the shape of the fix."""
    out = IDENTIFIER.sub("<name>", text or "")
    return re.sub(r"(<name>[\s,]*){2,}", "<names> ", out).strip()


def approach_from(summary: str) -> str:
    """The agent's DONE line, which is its own account of what it changed."""
    for line in reversed((summary or "").splitlines()):
        if line.strip().upper().startswith("DONE:"):
            return line.strip()[5:].strip()[:300]
    return ""


class LessonStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or PATH
        self.lessons: list[Lesson] = []
        if self.path.exists():
            try:
                self.lessons = [Lesson(**d) for d in json.loads(self.path.read_text(encoding="utf8"))]
            except (json.JSONDecodeError, TypeError):
                self.lessons = []

    def save(self) -> None:
        if self.path == PATH:
            ensure()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(l) for l in self.lessons], indent=2), encoding="utf8")

    def learn(self, repo: str, result) -> Lesson | None:
        """Record a clean, verified fix. Anything less teaches the wrong thing."""
        task = result.task
        if task is None or result.outcome != "fixed" or result.concerns:
            return None
        approach = approach_from(result.summary)
        if not approach or not result.diff:
            return None
        lesson = Lesson(
            repo=str(Path(repo).resolve()), kind=task.kind,
            code=(task.meta or {}).get("code", ""), title=task.title[:120],
            signature=signature_for(task), approach=approach, diff=result.diff[:1500])
        self.lessons.append(lesson)
        self.lessons = self.lessons[-500:]
        self.save()
        return lesson

    def relevant(self, repo: str, task, limit: int = 2) -> list[tuple[Lesson, bool]]:
        """(lesson, same_repo) pairs, most useful first."""
        repo = str(Path(repo).resolve())
        code = (task.meta or {}).get("code", "")
        sig = set(signature_for(task))
        scored = []
        for l in self.lessons:
            if l.kind != task.kind:
                continue
            if l.repo != repo and l.kind != "ai_review":
                continue      # failing-test lessons are made of one repo's names
            if task.kind == "ai_review":
                if l.code != code:
                    continue
                score = 1.0 + (0.5 if l.repo == repo else 0.0)
            else:
                sim = _similar(sig, set(l.signature))
                if sim < 0.3:
                    continue
                score = sim + (0.5 if l.repo == repo else 0.0)
            scored.append((score, l.ts, l))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        out = []
        for _, _, l in scored[:limit]:
            out.append((l, l.repo == repo))
        return out

    def brief_section(self, repo: str, task, limit: int = 2) -> str:
        found = self.relevant(repo, task, limit)
        if not found:
            return ""
        parts = ["\nEarlier shifts fixed a similar problem. Use this only if it genuinely "
                 "applies; the verification will be the same either way.\n"]
        for lesson, same_repo in found:
            lesson.uses += 1
            if same_repo:
                parts.append(f"- {lesson.title}\n  what worked: {lesson.approach}")
            else:
                parts.append(f"- a {lesson.code} finding in another repository\n"
                             f"  what worked: {scrub(lesson.approach)}")
            if same_repo and lesson.diff:
                parts.append("  the change, in this repository:\n" +
                             "\n".join("    " + l for l in lesson.diff.splitlines()[:30]))
        self.save()
        return "\n".join(parts) + "\n"
