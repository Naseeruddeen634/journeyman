"""Should this prompt move to a different model?

The question comes up every time a cheaper, faster or newer model appears, and
it is usually answered by trying a few inputs by hand. This answers it on the
prompt's eval cases instead, and is careful about what the answer is allowed to
say:

  - The verdict comes from the held-out cases only, paired case by case. A
    candidate that wins on the cases the prompt was tuned against has shown
    nothing.
  - "No significant difference" is not reported as "just as good". Ten held-out
    cases cannot show a small regression; the report says how large a
    difference the set could have detected at all.
  - Every case the candidate newly fails is listed with its output, significant
    or not. One of them being the prompt-injection case matters more than any
    p-value.
  - A call that errors (model not pulled, key missing, timeout) is an error,
    not a wrong answer, and makes the comparison incomplete rather than a loss.

Grading is the same deterministic grading as the eval harness. No model judges
another model here.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .evals import EvalDir, _key, grade, render

ALPHA = 0.05
SENSITIVE = ("inject", "adversarial", "jailbreak", "pii", "safety")
PROVIDERS = {"ollama": "local", "local": "local", "openrouter": "heavy", "heavy": "heavy",
             "kimi": "heavy", "bedrock": "bedrock"}


def parse_spec(spec: str) -> tuple[str, str]:
    """"ollama:qwen3-coder:30b" -> ("local", "qwen3-coder:30b"). A bare brain name keeps its
    configured model: "local" -> ("local", "")."""
    head, sep, rest = spec.partition(":")
    kind = PROVIDERS.get(head.lower())
    if kind is None:
        raise ValueError(f"unknown model spec {spec!r}: use ollama:<model>, openrouter:<model>, "
                         "bedrock:<model id>, or local / heavy / bedrock for the configured default")
    if sep and not rest:
        raise ValueError(f"model spec {spec!r} names a provider but no model")
    return kind, rest


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def smallest_detectable(alpha: float = ALPHA) -> int:
    """Fewest flips, all in one direction, that could ever reach significance."""
    n = 1
    while mcnemar_exact(0, n) >= alpha:
        n += 1
    return n


@dataclass
class ModelRun:
    name: str
    passed: dict[str, bool] = field(default_factory=dict)       # case id -> majority verdict
    flaky: list[str] = field(default_factory=list)              # disagreed with itself
    outputs: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)        # live calls only
    replayed: int = 0

    def score(self, ids: list[str]) -> float | None:
        graded = [self.passed[i] for i in ids if i in self.passed]
        return round(sum(graded) / len(graded), 4) if graded else None

    def latency(self) -> tuple[float, float] | None:
        if not self.latencies:
            return None
        xs = sorted(self.latencies)
        return round(statistics.median(xs), 2), round(xs[min(len(xs) - 1, int(0.9 * len(xs)))], 2)


@dataclass
class Flip:
    id: str
    split: str
    direction: str       # "regression" | "improvement"
    expect: dict
    incumbent_output: str
    candidate_output: str
    sensitive: bool


@dataclass
class Comparison:
    prompt: str
    incumbent: ModelRun
    candidate: ModelRun
    holdout_ids: list[str]
    train_ids: list[str]
    flips: list[Flip]
    p_value: float
    verdict: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _run_model(ev: EvalDir, name: str, call: Callable[[str], str] | None, samples: int,
               cassette: dict, refresh: bool = False) -> ModelRun:
    prompt = ev.prompt()
    responses = cassette.setdefault("responses", {})
    run = ModelRun(name)
    for case in ev.cases():
        base = _key(prompt, case.input, name)
        verdicts = []
        for s in range(samples):
            key = base if s == 0 else f"{base}#{s}"
            if key not in responses or (refresh and call is not None):
                if call is None:
                    run.errors[case.id] = "no recorded response and no way to call the model"
                    break
                started = time.monotonic()
                try:
                    responses[key] = call(render(prompt, case.input))
                except Exception as exc:  # the model is unavailable, not wrong
                    run.errors[case.id] = f"{type(exc).__name__}: {str(exc)[:160]}"
                    break
                run.latencies.append(time.monotonic() - started)
            else:
                run.replayed += 1
            out = responses[key]
            verdicts.append(grade(out, case.expect))
            run.outputs.setdefault(case.id, out)
        if case.id in run.errors or not verdicts:
            continue
        run.passed[case.id] = sum(verdicts) * 2 > len(verdicts)    # a tie is not a pass
        if len(set(verdicts)) > 1:
            run.flaky.append(case.id)
    return run


def compare(ev: EvalDir, incumbent: str, candidate: str,
            calls: dict[str, Callable[[str], str] | None], samples: int = 1,
            refresh: bool = False) -> Comparison:
    """Run both models over the cases and judge on the holdout.

    Responses already in the cassette are replayed unless refresh is set, so
    comparing against the incumbent usually costs nothing. Latency is only
    reported for calls made in this run.
    """
    if incumbent == candidate:
        raise ValueError("comparing a model with itself; pass two different models")
    cassette = ev.cassette()
    replay_model = cassette.get("model", "")
    a = _run_model(ev, incumbent, calls.get(incumbent), samples, cassette, refresh)
    b = _run_model(ev, candidate, calls.get(candidate), samples, cassette, refresh)
    cassette["model"] = replay_model          # the regression test keeps replaying its own model
    ev.cassette_file.parent.mkdir(parents=True, exist_ok=True)
    ev.cassette_file.write_text(json.dumps(cassette, indent=2), encoding="utf8")

    cases = {c.id: c for c in ev.cases()}
    holdout = [i for i, c in cases.items() if c.split == "holdout"]
    train = [i for i, c in cases.items() if c.split != "holdout"]

    flips = []
    for cid, case in cases.items():
        if cid not in a.passed or cid not in b.passed or a.passed[cid] == b.passed[cid]:
            continue
        text = f"{cid} {case.note}".lower()
        flips.append(Flip(cid, case.split, "regression" if a.passed[cid] else "improvement",
                          case.expect, a.outputs.get(cid, "")[:300], b.outputs.get(cid, "")[:300],
                          any(w in text for w in SENSITIVE)))
    flips.sort(key=lambda f: (f.direction != "regression", not f.sensitive, f.split != "holdout", f.id))

    regress = sum(1 for f in flips if f.split == "holdout" and f.direction == "regression")
    improve = sum(1 for f in flips if f.split == "holdout" and f.direction == "improvement")
    p = mcnemar_exact(regress, improve)
    need = smallest_detectable()
    graded = [i for i in holdout if i in a.passed and i in b.passed]

    if a.errors or b.errors:
        verdict = "incomplete"
        detail = (f"{len(a.errors)} call(s) to {incumbent} and {len(b.errors)} to {candidate} "
                  "did not return. Errors are not counted as wrong answers; fix them and rerun.")
    elif not holdout:
        verdict = "no holdout"
        detail = "There are no held-out cases, so nothing here can support a decision."
    elif regress == 0 and improve == 0:
        verdict = "same on holdout"
        detail = (f"Both models pass and fail the same {len(graded)} held-out case(s). "
                  + (f"With {len(graded)} cases, a difference smaller than {need} flips would not show."
                     if len(graded) < 3 * need else ""))
    elif p < ALPHA:
        better = candidate if improve > regress else incumbent
        verdict = f"{better} is better"
        detail = (f"{improve} held-out improvement(s), {regress} regression(s), exact McNemar "
                  f"p={p:.3f}.")
    else:
        verdict = "no significant difference"
        detail = (f"{improve} held-out improvement(s), {regress} regression(s), p={p:.2f}. "
                  f"That is not evidence the models are equivalent: on this set, at least {need} "
                  "flips all in one direction are needed before any difference can show.")
    return Comparison(ev.prompt_path.name, a, b, holdout, train, flips, p, verdict, detail)


def render_report(c: Comparison) -> str:
    def pct(x):
        return "  -  " if x is None else f"{x:>4.0%}"

    lines = [f"\n  {c.prompt}: {c.incumbent.name}  vs  {c.candidate.name}\n",
             f"  {'':<26}{'holdout':>9}{'train':>8}{'median s':>10}{'p90 s':>8}  notes"]
    for run in (c.incumbent, c.candidate):
        lat = run.latency()
        notes = []
        if run.flaky:
            notes.append(f"{len(run.flaky)} flaky")
        if run.errors:
            notes.append(f"{len(run.errors)} errors")
        if run.replayed and not run.latencies:
            notes.append("replayed")
        lines.append(f"  {run.name[:25]:<26}{pct(run.score(c.holdout_ids)):>9}"
                     f"{pct(run.score(c.train_ids)):>8}"
                     f"{(f'{lat[0]:.2f}' if lat else '-'):>10}{(f'{lat[1]:.2f}' if lat else '-'):>8}"
                     f"  {', '.join(notes)}")
    lines += ["", f"  verdict: {c.verdict.upper()}", f"  {c.detail}"]
    regressions = [f for f in c.flips if f.direction == "regression"]
    if regressions:
        lines.append(f"\n  Cases {c.candidate.name} newly fails (read these before switching):")
        for f in regressions[:8]:
            flag = "  [SENSITIVE]" if f.sensitive else ""
            lines.append(f"    [{f.split}] {f.id}{flag}  expected {f.expect}")
            lines.append(f"      {c.incumbent.name}: {f.incumbent_output[:90]!r}")
            lines.append(f"      {c.candidate.name}: {f.candidate_output[:90]!r}")
    for run in (c.incumbent, c.candidate):
        for cid, err in list(run.errors.items())[:3]:
            lines.append(f"  error  {run.name} on {cid}: {err}")
    return "\n".join(lines) + "\n"
