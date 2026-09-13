"""What Journeyman remembers between runs.

This is the self-evolving part, and it is deliberately the least magical thing
in the repo. After every run it writes down what the task looked like, which
patterns it chose, and which specific repairs moved the score. On the next run
it looks for prior work on a similar task and seeds the search with the repairs
that worked before.

The effect is measurable rather than asserted: the second run on a related task
reaches the same score in fewer generations, and the report says which memory it
used. A claim of "it learns" that cannot be counted is not worth making.

Stored as JSON on disk. A database here would be architecture for its own sake.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path.home() / ".journeyman" / "memory.json"

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "for", "from", "with", "that",
    "this", "it", "is", "are", "be", "build", "me", "my", "an", "agent", "make",
    "create", "i", "want", "need", "please", "can", "you", "into", "out", "on",
}


@dataclass
class Lesson:
    """One thing learned, tied to the failure it fixed."""

    field_or_slot: str
    failure_pattern: str
    repair: str
    gain: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Episode:
    """One completed run."""

    task: str
    fingerprint: list[str]
    patterns_chosen: list[str]
    generations: int
    baseline_heldout: float
    shipped_heldout: float
    lessons: list[Lesson] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lessons"] = [l.to_dict() if isinstance(l, Lesson) else l for l in self.lessons]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        d = dict(d)
        d["lessons"] = [Lesson(**l) for l in d.get("lessons", [])]
        return cls(**d)

    @property
    def gain(self) -> float:
        return self.shipped_heldout - self.baseline_heldout


def fingerprint(task: str) -> list[str]:
    """A crude bag of content words. Crude on purpose: it is inspectable."""
    words = re.findall(r"[a-z][a-z0-9_]{2,}", task.lower())
    return sorted({w for w in words if w not in STOPWORDS})


def similarity(a: list[str], b: list[str]) -> float:
    """Jaccard over fingerprints."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class Memory:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self.episodes: list[Episode] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf8"))
                self.episodes = [Episode.from_dict(e) for e in raw.get("episodes", [])]
            except (json.JSONDecodeError, TypeError, KeyError):
                self.episodes = []   # a corrupt memory is not worth dying over

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"episodes": [e.to_dict() for e in self.episodes]}, indent=2),
            encoding="utf8",
        )

    # ---- writing -----------------------------------------------------

    def record(self, episode: Episode) -> None:
        self.episodes.append(episode)
        self.save()

    # ---- reading -----------------------------------------------------

    def recall(self, task: str, threshold: float = 0.25, limit: int = 3) -> list[tuple[Episode, float]]:
        """Prior runs on a similar task, most similar first."""
        fp = fingerprint(task)
        scored = [(e, similarity(fp, e.fingerprint)) for e in self.episodes]
        hits = [(e, s) for e, s in scored if s >= threshold and e.gain > 0]
        return sorted(hits, key=lambda t: -t[1])[:limit]

    def suggested_repairs(self, task: str) -> list[tuple[str, str, float]]:
        """(slot, repair, gain) worth trying first, best-known first.

        This is what actually makes the next run cheaper: the search starts from
        the repairs that paid off before instead of walking the library in
        declaration order.
        """
        seen: dict[tuple[str, str], float] = {}
        for episode, sim in self.recall(task):
            for lesson in episode.lessons:
                key = (lesson.field_or_slot, lesson.repair)
                seen[key] = max(seen.get(key, 0.0), lesson.gain * sim)
        return [(slot, repair, round(w, 4))
                for (slot, repair), w in sorted(seen.items(), key=lambda kv: -kv[1])]

    def stats(self) -> dict:
        return {
            "episodes": len(self.episodes),
            "lessons": sum(len(e.lessons) for e in self.episodes),
            "mean_gain": round(
                sum(e.gain for e in self.episodes) / len(self.episodes), 4
            ) if self.episodes else 0.0,
        }
