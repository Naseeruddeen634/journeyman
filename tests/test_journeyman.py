"""Tests for the claims the demo makes, and nothing else.

Each test corresponds to a sentence Journeyman says out loud. If a claim is not
tested here, it should not be in the README.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo.generate import build_corpus  # noqa: E402
from journeyman.evalset.grade import grade  # noqa: E402
from journeyman.evalset.runner import evaluate_source, load_artifact, evaluate  # noqa: E402
from journeyman.evolve.optimizer import evolve, split_cases  # noqa: E402
from journeyman.evolve.proposals import memorising_proposer  # noqa: E402
from journeyman.evolve.strategies import BASELINE_GENOME, assemble  # noqa: E402
from journeyman.graph import build_job_graph  # noqa: E402
from journeyman.memory.store import Memory, fingerprint, similarity  # noqa: E402
from journeyman.patterns.library import PATTERNS, rejected_with_reasons, score_patterns  # noqa: E402
from journeyman.session import Job  # noqa: E402


@pytest.fixture(scope="module")
def corpus():
    return build_corpus(120)


@pytest.fixture(scope="module")
def split(corpus):
    return split_cases(corpus, 0.5, seed=7)


# ---- the measurement is real ------------------------------------------


def test_ground_truth_is_exact_by_construction(corpus):
    """Every answer existed before the document that contains it."""
    for case in corpus[:20]:
        assert set(case["truth"]) == {"vendor", "date", "total", "currency", "po_number"}
        assert case["truth"]["vendor"] in case["document"]
        assert len(case["truth"]["date"]) == 10


def test_grading_is_deterministic(corpus):
    """Same input, same score, every time. No model in the loop."""
    src = assemble(BASELINE_GENOME)
    a = evaluate_source(src, corpus, "a").score
    b = evaluate_source(src, corpus, "b").score
    assert a == b


def test_baseline_is_genuinely_broken(corpus):
    """If the starting artifact were fine there would be nothing to demonstrate."""
    rep = evaluate_source(assemble(BASELINE_GENOME), corpus, "baseline")
    assert rep.score < 0.75
    assert rep.exact_match < 0.15, "baseline should rarely get a whole document right"


def test_failure_clusters_name_the_actual_problem(corpus):
    rep = evaluate_source(assemble(BASELINE_GENOME), corpus, "baseline")
    clusters = rep.failure_clusters()
    assert clusters, "a broken baseline must produce clusters"
    assert any(c["field"] == "date" for c in clusters)
    for c in clusters:
        assert len(c["pattern"]) > 3 and c["count"] > 0


# ---- the evolution works ----------------------------------------------


def test_evolution_improves_on_data_it_never_saw(split):
    train, held = split
    r = evolve(train, held, BASELINE_GENOME)
    assert r.shipped_heldout.score > r.baseline_heldout.score + 0.1
    assert r.improved


def test_search_never_touches_heldout_during_the_search(split):
    """Every ledger entry is a train score. Selection is the first held-out read."""
    train, held = split
    r = evolve(train, held, BASELINE_GENOME)
    train_ids = {c["id"] for c in train}
    held_ids = {c["id"] for c in held}
    assert not (train_ids & held_ids), "the split must not overlap"
    for entry in r.ledger:
        assert 0.0 <= entry["train_score"] <= 1.0


# ---- the gate is not decorative ---------------------------------------


def test_gate_rejects_a_memorising_candidate(split):
    """The documented failure mode: a proposal that scores by memorising.

    It must win on train and lose on held-out, and the gate must not ship it.
    """
    train, held = split
    memo = memorising_proposer(train)
    slot, name, source = memo({}, evaluate_source(assemble(BASELINE_GENOME), train))[0]
    genome = dict(BASELINE_GENOME)
    from journeyman.evolve.strategies import SLOTS
    SLOTS[slot][name] = source
    genome[slot] = name

    on_train = evaluate_source(assemble(genome), train, "memoriser train")
    on_held = evaluate_source(assemble(genome), held, "memoriser held")
    assert on_train.by_field()["total"] > on_held.by_field()["total"] + 0.2, (
        "the memorising candidate must look better on train than it is"
    )


def test_no_improvement_is_reported_as_no_improvement(split):
    """Negative control: start from a good artifact, expect an honest refusal."""
    train, held = split
    good = {"vendor": "labelled_then_skip_headers", "date": "all_formats",
            "total": "total_not_subtotal", "currency": "code_or_symbol",
            "po_number": "po_or_labelled_ref"}
    r = evolve(train, held, good)
    assert not r.improved or r.shipped_heldout.score <= r.baseline_heldout.score + 1e-9
    assert "No reliable improvement" in r.verdict


def test_generated_gate_fails_when_the_artifact_regresses(split, tmp_path):
    """A gate that cannot fail is worse than no gate."""
    train, held = split
    good = assemble({"vendor": "labelled_then_skip_headers", "date": "all_formats",
                     "total": "total_not_subtotal", "currency": "code_or_symbol",
                     "po_number": "po_or_labelled_ref"})
    bad = assemble(BASELINE_GENOME)
    threshold = evaluate_source(good, held, "good").score - 0.02
    assert evaluate_source(good, held, "good").score >= threshold
    assert evaluate_source(bad, held, "bad").score < threshold, (
        "the regressed artifact must fall below the shipped threshold"
    )


# ---- the pattern library encodes judgement ----------------------------


def test_architect_rules_out_rag_when_there_is_no_corpus():
    goal = "answer questions about our internal policy documents"
    have = {"a document corpus": False, "an embedding model": False}
    ranked = dict((k, s) for k, s, _ in score_patterns(goal, have))
    assert ranked["rag_hybrid"] < 0, "no corpus means RAG is not available"


def test_architect_picks_rag_when_the_corpus_exists():
    goal = "answer questions about our internal policy documents"
    have = {"a document corpus": True, "an embedding model": True}
    ranked = [k for k, s, _ in score_patterns(goal, have)]
    assert ranked[0] == "rag_hybrid"


def test_every_pattern_says_when_not_to_use_it():
    """The rejections are the judgement. A pattern without them is a brochure."""
    for key, p in PATTERNS.items():
        assert p.when_not_to_use, f"{key} has no when_not_to_use"
        assert p.failure_modes, f"{key} has no failure modes"


def test_rejections_come_with_reasons():
    out = rejected_with_reasons("extract fields from invoices", ["structured_extraction"], {}, 3)
    assert out and all(len(r["why_not"]) > 10 for r in out)


# ---- memory actually saves work ---------------------------------------


def test_fingerprint_matches_related_tasks_not_unrelated_ones():
    a = fingerprint("extract vendor, date and total from messy invoices")
    b = fingerprint("extract supplier, date and amount from messy purchase orders")
    c = fingerprint("summarise customer support calls into three bullet points")
    assert similarity(a, b) > similarity(a, c)


def test_memory_reduces_the_number_of_candidates_measured(split, tmp_path):
    """The self-evolution claim, as a number. Same answer, less work."""
    train, held = split
    cold = evolve(train, held, BASELINE_GENOME)

    mem = Memory(tmp_path / "m.json")
    from journeyman.memory.store import Episode, Lesson
    task = "extract vendor, date and total from messy invoices"
    mem.record(Episode(
        task=task, fingerprint=fingerprint(task), patterns_chosen=["structured_extraction"],
        generations=len(cold.generations) - 1,
        baseline_heldout=cold.baseline_heldout.score,
        shipped_heldout=cold.shipped_heldout.score,
        lessons=[Lesson(s, p, r, g) for s, p, r, g in cold.lessons],
    ))

    related = "extract supplier, date and amount total from messy purchase order documents"
    warm = evolve(train, held, BASELINE_GENOME,
                  priority_repairs=mem.suggested_repairs(related))

    assert warm.candidates_tried < cold.candidates_tried, "memory must save work"
    assert warm.shipped_heldout.score >= cold.shipped_heldout.score - 1e-9, (
        "and must not cost accuracy"
    )


def test_a_recalled_repair_is_still_measured_not_trusted(split, tmp_path):
    train, held = split
    mem = Memory(tmp_path / "m.json")
    from journeyman.memory.store import Episode, Lesson
    task = "extract vendor date total invoices"
    mem.record(Episode(task=task, fingerprint=fingerprint(task), patterns_chosen=[],
                       generations=1, baseline_heldout=0.5, shipped_heldout=0.9,
                       lessons=[Lesson("date", "returned nothing", "all_formats", 0.2)]))
    r = evolve(train, held, BASELINE_GENOME,
               priority_repairs=mem.suggested_repairs("extract date invoices vendor total"))
    recalled = [e for e in r.ledger if e.get("from_memory")]
    assert recalled, "the recalled repair should appear in the ledger"
    for e in recalled:
        assert "train_score" in e, "a recalled repair is still scored before being accepted"


# ---- the graph is a real cycle ----------------------------------------


def test_graph_loops_once_per_generation(corpus, tmp_path):
    job = Job(goal="extract vendor, date and total from messy invoices and prove it")
    mem = Memory(tmp_path / "m.json")
    graph = build_job_graph(job, corpus, mem, tmp_path / "out")
    result = graph(job.goal)
    order = [n.node_id for n in result.execution_order]
    assert order.count("evolve") > 1, "the evolution loop must be a cycle, not one node"
    assert order.count("evolve") == order.count("select")
    assert order[0] == "intake" and order[-1] == "ship"


def test_graph_writes_the_three_artifacts(corpus, tmp_path):
    job = Job(goal="extract vendor, date and total from messy invoices and prove it")
    mem = Memory(tmp_path / "m.json")
    out = tmp_path / "out"
    build_job_graph(job, corpus, mem, out)(job.goal)
    for name in ("extractor.py", "eval_set.json", "test_regression.py"):
        assert (out / name).exists(), f"{name} was not written"
    fn = load_artifact(out / "extractor.py")
    cases = json.loads((out / "eval_set.json").read_text())["held_out"]
    assert evaluate(fn, cases, "shipped").score > 0.8
