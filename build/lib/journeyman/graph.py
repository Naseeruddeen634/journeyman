"""The agent graph.

    intake -> architect -> recall -> harness -> prove -> evolve <-+
                                                          |       |
                                                       select ----+  (keep going)
                                                          |
                                                         gate -> ship

The edge from `select` back to `evolve` is a genuine cycle in the Strands graph,
not a for-loop hidden inside one node. One node execution is one generation, so
the loop is visible in `execution_order`, bounded by `set_max_node_executions`,
and its exit is a conditional edge reading a value a tool computed.

Nodes that need judgement use a model. Nodes that are arithmetic do not: reading
a CSV, splitting an eval set and measuring a candidate are not improved by asking
a language model to do them, and paying for one there would be theatre.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from strands.agent.agent_result import AgentResult
from strands.multiagent import GraphBuilder
from strands.multiagent.graph import GraphState
from strands.telemetry.metrics import EventLoopMetrics

from .evalset.runner import evaluate_source
from .evolve.optimizer import Candidate, split_cases
from .evolve.strategies import BASELINE_GENOME, FIELD_TO_SLOT, SLOTS, assemble
from .memory.store import Episode, Lesson, Memory, fingerprint
from .patterns.library import PATTERNS, rejected_with_reasons, score_patterns
from .session import Job

MIN_GAIN = 0.005


class Step:
    """A graph node that runs plain Python.

    Strands' AgentBase is a runtime-checkable Protocol, so anything exposing
    invoke_async, __call__ and stream_async is a valid node.
    """

    def __init__(self, name: str, fn) -> None:
        self.name = name
        self.id = name
        self._fn = fn

    async def invoke_async(self, prompt: Any = None, **kwargs: Any) -> AgentResult:
        try:
            text = str(self._fn())
        except Exception as exc:  # a raising node kills the whole graph
            text = f"{self.name} failed: {exc}"
        return AgentResult(
            stop_reason="end_turn",
            message={"role": "assistant", "content": [{"text": text}]},
            metrics=EventLoopMetrics(), state={}, structured_output=None,
        )

    def __call__(self, prompt: Any = None, **kwargs: Any) -> AgentResult:
        return asyncio.run(self.invoke_async(prompt, **kwargs))

    async def stream_async(self, prompt: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        yield {"result": await self.invoke_async(prompt, **kwargs)}


def build_job_graph(job: Job, corpus: list[dict], memory: Memory, outdir: Path,
                    propose_extra=None):
    """Wire the graph for one engineering job."""

    # ---------------------------------------------------------- intake
    def intake() -> str:
        job.resources = {
            "a measurement function": True,
            "an eval set with a held-out split": True,
            "at least one real operation to perform": True,
            "a document corpus": False,
            "an embedding model": False,
            "observational data with a treatment that varies": False,
        }
        job.note(f"goal: {job.goal}")
        return (f"Read the goal. {len(corpus)} documents available. "
                f"Resources present: {[k for k, v in job.resources.items() if v]}")

    # -------------------------------------------------------- architect
    def architect() -> str:
        ranked = score_patterns(job.goal, job.resources)
        chosen = [k for k, s, _ in ranked if s > 0][:2] or [ranked[0][0]]
        job.patterns = chosen
        job.rejected = rejected_with_reasons(job.goal, chosen, job.resources, limit=4)
        for key, score, reasons in ranked:
            if key in chosen:
                job.design_notes.append(f"{PATTERNS[key].name}: {'; '.join(reasons) or 'best fit'}")
        lines = ["Chose: " + ", ".join(PATTERNS[k].name for k in chosen)]
        lines += [f"  Rejected {r['pattern']}: {r['why_not']}" for r in job.rejected]
        return "\n".join(lines)

    # ----------------------------------------------------------- recall
    def recall() -> str:
        hits = memory.recall(job.goal)
        job.recalled = [
            {"task": e.task, "similarity": round(s, 3), "gain": round(e.gain, 3),
             "generations": e.generations}
            for e, s in hits
        ]
        job.priority_repairs = memory.suggested_repairs(job.goal)
        if not hits:
            return "No prior work on anything like this. Searching from scratch."
        return (f"Recalled {len(hits)} similar job(s). Will try first: "
                + ", ".join(f"{s}={i}" for s, i, _ in job.priority_repairs))

    # ---------------------------------------------------------- harness
    def harness() -> str:
        train, held = split_cases(corpus, 0.5, seed=7)
        job.cases_train, job.cases_heldout = train, held
        return (f"Eval set built: {len(train)} cases to search against, {len(held)} held "
                f"back. The held-out half is not read again until selection.")

    # ------------------------------------------------------------ prove
    def prove() -> str:
        job.genome = dict(BASELINE_GENOME)
        rep = evaluate_source(assemble(job.genome), job.cases_train, "gen 0 (train)")
        job.baseline = Candidate(dict(job.genome), rep, 0, reason="starting point")
        clusters = rep.failure_clusters()[:3]
        lines = [f"Baseline: {rep.score:.1%} field accuracy, {rep.exact_match:.1%} of "
                 f"documents fully correct.", "What is actually wrong:"]
        lines += [f"  {c['count']} cases: {c['field']} - {c['pattern']}" for c in clusters]
        return "\n".join(lines)

    # ----------------------------------------------------------- evolve
    def evolve_once() -> str:
        cur = job.current
        if cur is None:
            job.stalled = True
            return "nothing to evolve"
        job.generation += 1
        gen = job.generation
        prio = {(s, i): w for s, i, w in job.priority_repairs}

        clusters = cur.train.failure_clusters()
        targets, seen = [], set()
        for c in clusters:
            slot = FIELD_TO_SLOT.get(c["field"])
            if slot and slot not in seen:
                seen.add(slot)
                targets.append((slot, f"{c['count']} cases fail on {c['field']}: {c['pattern']}"))
        if not targets:
            job.stalled = True
            return "No failures left to target."

        best: Candidate | None = None
        for slot, reason in targets:
            impls = list(SLOTS[slot])
            order = sorted(impls, key=lambda i: (-prio.get((slot, i), 0.0), impls.index(i)))
            for impl in order:
                if impl == cur.genome.get(slot):
                    continue
                trial = dict(cur.genome)
                trial[slot] = impl
                rep = evaluate_source(assemble(trial), job.cases_train, f"gen {gen}")
                from_mem = (slot, impl) in prio
                job.ledger.append({
                    "generation": gen, "tried": f"{slot}={impl}",
                    "train_score": round(rep.score, 4),
                    "delta": round(rep.score - cur.score, 4),
                    "targeted": reason, "from_memory": from_mem,
                })
                if best is None or rep.score > best.score:
                    best = Candidate(trial, rep, gen, slot, impl, reason)
                if from_mem and rep.score > cur.score + MIN_GAIN:
                    best = Candidate(trial, rep, gen, slot, impl,
                                     reason + " [recalled from a previous run]")
                    break

            if propose_extra is not None:
                for s_, n_, src in propose_extra(cur.genome, cur.train):
                    SLOTS.setdefault(s_, {})[n_] = src
                    trial = dict(cur.genome); trial[s_] = n_
                    rep = evaluate_source(assemble(trial), job.cases_train, f"gen {gen}")
                    job.ledger.append({
                        "generation": gen, "tried": f"{s_}={n_} (model proposed)",
                        "train_score": round(rep.score, 4),
                        "delta": round(rep.score - cur.score, 4),
                        "targeted": "model-proposed strategy", "from_memory": False,
                    })
                    if best is None or rep.score > best.score:
                        best = Candidate(trial, rep, gen, s_, n_, "model-proposed")

            if best is not None and best.score > cur.score + MIN_GAIN:
                break

        if best is None or best.score <= cur.score + MIN_GAIN:
            job.stalled = True
            return f"Generation {gen}: nothing beat {cur.score:.1%}. Stopping."
        job.history.append(best)
        tag = " (recalled)" if any(l.get("from_memory") and l["generation"] == gen for l in job.ledger) else ""
        return (f"Generation {gen}: {cur.score:.1%} -> {best.score:.1%} by changing "
                f"{best.changed_slot} to {best.changed_to}{tag}")

    def select() -> str:
        if job.stalled:
            return "Search has converged."
        if job.generation >= job.max_generations:
            return f"Generation budget of {job.max_generations} reached."
        return f"Still improving at generation {job.generation}. Going round again."

    # ------------------------------------------------------------- gate
    def gate() -> str:
        """First and only look at the held-out half."""
        job.baseline_heldout = evaluate_source(
            assemble(BASELINE_GENOME), job.cases_heldout, "baseline (held out)")
        pool = ([job.baseline] if job.baseline else []) + job.history
        scored = [(c, evaluate_source(assemble(c.genome), job.cases_heldout,
                                      f"gen {c.generation} (held out)")) for c in pool]
        job.best_train = max(pool, key=lambda c: c.score)
        job.best_train_heldout = next(h for c, h in scored if c is job.best_train)
        job.shipped, job.shipped_heldout = max(scored, key=lambda p: p[1].score)
        job.overfit_rejected = job.shipped is not job.best_train

        if job.shipped_heldout.score <= job.baseline_heldout.score + 1e-9:
            job.shipped, job.shipped_heldout = job.baseline, job.baseline_heldout
            job.verdict = ("No reliable improvement. Nothing found beat what you already "
                           "had on data it has not seen, so keep what you have.")
        elif job.overfit_rejected:
            job.verdict = (
                f"Generation {job.best_train.generation} scored {job.best_train.score:.1%} on "
                f"the eval set and only {job.best_train_heldout.score:.1%} on data it never "
                f"saw. That is overfitting, so it was rejected. Shipping generation "
                f"{job.shipped.generation}: {job.shipped_heldout.score:.1%} held out.")
        else:
            job.verdict = (f"Shipping generation {job.shipped.generation}. "
                           f"{job.baseline_heldout.score:.1%} to "
                           f"{job.shipped_heldout.score:.1%} on data it never saw.")
        return job.verdict

    # ------------------------------------------------------------- ship
    def ship() -> str:
        outdir.mkdir(parents=True, exist_ok=True)
        art = outdir / "extractor.py"
        art.write_text(assemble(job.shipped.genome), encoding="utf8")
        evalfile = outdir / "eval_set.json"
        evalfile.write_text(json.dumps(
            {"train": job.cases_train, "held_out": job.cases_heldout}, indent=2), encoding="utf8")
        gatefile = outdir / "test_regression.py"
        gatefile.write_text(_GATE_TEMPLATE.format(
            threshold=round(job.shipped_heldout.score - 0.02, 4)), encoding="utf8")
        job.artifacts = {"artifact": str(art), "eval_set": str(evalfile), "gate": str(gatefile)}

        prev = job.baseline.score if job.baseline else 0.0
        for c in job.history:
            if c.changed_slot:
                pat = c.reason.split(": ", 1)[-1] if ": " in c.reason else c.reason
                job.lessons.append((c.changed_slot, pat, c.changed_to, round(c.score - prev, 4)))
            prev = c.score

        if job.shipped_heldout.score > job.baseline_heldout.score:
            memory.record(Episode(
                task=job.goal, fingerprint=fingerprint(job.goal),
                patterns_chosen=job.patterns, generations=len(job.history),
                baseline_heldout=job.baseline_heldout.score,
                shipped_heldout=job.shipped_heldout.score,
                lessons=[Lesson(s, p, r, g) for s, p, r, g in job.lessons],
            ))
        return (f"Wrote {art.name}, {evalfile.name} and {gatefile.name}. "
                f"Recorded {len(job.lessons)} lesson(s) to memory.")

    # ----------------------------------------------------------- wiring
    b = GraphBuilder()
    for name, fn in (("intake", intake), ("architect", architect), ("recall", recall),
                     ("harness", harness), ("prove", prove), ("evolve", evolve_once),
                     ("select", select), ("gate", gate), ("ship", ship)):
        b.add_node(Step(name, fn), name)

    b.add_edge("intake", "architect")
    b.add_edge("architect", "recall")
    b.add_edge("recall", "harness")
    b.add_edge("harness", "prove")
    b.add_edge("prove", "evolve")
    b.add_edge("evolve", "select")
    # the cycle, and its exit. Both read state a tool computed.
    b.add_edge("select", "evolve", condition=lambda s: job.keep_evolving)
    b.add_edge("select", "gate", condition=lambda s: not job.keep_evolving)
    b.add_edge("gate", "ship")

    b.set_entry_point("intake")
    b.reset_on_revisit(True)
    b.set_max_node_executions(60)
    b.set_execution_timeout(900)
    b.set_node_timeout(300)
    b.set_graph_id("journeyman")
    return b.build()


_GATE_TEMPLATE = '''"""Regression gate, written by Journeyman.

Fails if a future edit scores worse on the held-out half than what was shipped.
Run it in CI. That is the whole point: the improvement stays improved.
"""

import json
import pathlib

from journeyman.evalset.runner import load_artifact, evaluate

HERE = pathlib.Path(__file__).parent
THRESHOLD = {threshold}


def test_does_not_regress():
    cases = json.loads((HERE / "eval_set.json").read_text())["held_out"]
    report = evaluate(load_artifact(HERE / "extractor.py"), cases, "regression")
    assert report.score >= THRESHOLD, (
        f"held-out score {{report.score:.3f}} is below the shipped {{THRESHOLD:.3f}}. "
        f"Worst clusters: {{report.failure_clusters()[:3]}}"
    )
'''
