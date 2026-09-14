"""Shift memory: what is learned, when it is recalled, and what may cross repositories."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman.autonomy.scout import Task  # noqa: E402
from journeyman.autonomy.shift import ShiftResult  # noqa: E402
from journeyman.lessons import LessonStore, approach_from  # noqa: E402

DIFF = ('--- a/app/support.py\n+++ b/app/support.py\n'
        '-    prompt = f"Reply to: {ticket_text}"\n'
        '+    prompt = f"{SYSTEM} <ticket>{ticket_text}</ticket>"\n')


def review_task(code="AIE004", where="app/support.py:11"):
    return Task("ai_review", f"{code}: user input goes straight into a prompt", "why and fix",
                where, 85, meta={"code": code})


def delivered(task, outcome="fixed", concerns=(), summary="DONE: wrapped input in tags and declared it data"):
    return ShiftResult(task=task, outcome=outcome, concerns=list(concerns), summary=summary, diff=DIFF)


@pytest.fixture
def store(tmp_path):
    return LessonStore(tmp_path / "lessons.json")


def test_only_clean_verified_fixes_are_learned(store, tmp_path):
    t = review_task()
    assert store.learn(str(tmp_path), delivered(t, outcome="stuck")) is None
    assert store.learn(str(tmp_path), delivered(t, concerns=["diff moves 1 line"])) is None, \
        "a fix the shift itself doubted must not become a lesson"
    assert store.learn(str(tmp_path), delivered(t, summary="no done line")) is None
    assert store.learn(str(tmp_path), delivered(t)) is not None
    assert len(LessonStore(store.path).lessons) == 1, "lessons persist"


def test_same_repo_recall_includes_the_diff(store, tmp_path):
    repo = tmp_path / "billing"
    repo.mkdir()
    store.learn(str(repo), delivered(review_task()))
    brief = store.brief_section(str(repo), review_task(where="app/other.py:4"))
    assert "what worked: wrapped input in tags" in brief
    assert "<ticket>{ticket_text}</ticket>" in brief


def test_another_repo_gets_the_approach_but_never_the_code(store, tmp_path):
    """With a cloud brain, repo A's diff in repo B's prompt would send A's code to a provider."""
    a, b = tmp_path / "team-a", tmp_path / "team-b"
    a.mkdir(); b.mkdir()
    store.learn(str(a), delivered(review_task()))
    brief = store.brief_section(str(b), review_task())
    assert "what worked: wrapped input in tags" in brief
    assert "ticket_text" not in brief and "support.py" not in brief.split("what worked")[1]


def test_a_different_finding_code_is_not_recalled(store, tmp_path):
    store.learn(str(tmp_path), delivered(review_task("AIE004")))
    assert store.brief_section(str(tmp_path), review_task("AIE002")) == ""


def test_failing_tests_are_matched_by_what_the_failure_looks_like(store, tmp_path):
    t1 = Task("failing_test", "failed: test_vat", "", "tests/test_tax.py", 100,
              evidence="assert gross(250.0, 13.5) == 283.75  AssertionError vat rate percentage")
    store.learn(str(tmp_path), delivered(t1, summary="DONE: multiply by (1 + rate/100)"))
    similar = Task("failing_test", "failed: test_vat_reduced", "", "tests/test_tax.py", 100,
                   evidence="assert gross(100.0, 23.0) == 123.0 AssertionError vat rate percentage")
    unrelated = Task("failing_test", "failed: test_slug", "", "tests/test_slug.py", 100,
                     evidence="assert slugify('A -- B') == 'a-b' AssertionError dashes")
    assert "multiply by" in store.brief_section(str(tmp_path), similar)
    assert store.brief_section(str(tmp_path), unrelated) == ""


def test_at_most_two_lessons_newest_first_same_repo_preferred(store, tmp_path):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir(); there.mkdir()
    for i, repo in enumerate([there, there, here]):
        store.learn(str(repo), delivered(review_task(), summary=f"DONE: approach {i}"))
    found = store.relevant(str(here), review_task())
    assert len(found) == 2
    assert found[0][1] is True and found[0][0].approach == "approach 2"


def test_approach_is_the_agents_own_done_line():
    assert approach_from("thinking...\nDONE: fixed the rounding\n") == "fixed the rounding"
    assert approach_from("STUCK: no idea") == ""


def test_names_from_another_repo_do_not_leak_through_title_or_approach(store, tmp_path):
    a, b = tmp_path / "acme", tmp_path / "other"
    a.mkdir(); b.mkdir()
    t = Task("ai_review", "AIE004: user input in build_acme_payroll_prompt", "", "payroll/acme_prompts.py:9",
             85, meta={"code": "AIE004"})
    store.learn(str(a), delivered(t, summary=(
        "DONE: wrapped employee_notes in <notes> tags inside build_acme_payroll_prompt() "
        "in payroll/acme_prompts.py and declared AcmeClient input as data")))
    brief = store.brief_section(str(b), review_task())
    for leak in ("acme", "payroll", "employee_notes", "AcmeClient", ".py"):
        assert leak.lower() not in brief.lower(), f"{leak!r} leaked across repositories:\n{brief}"
    assert "wrapped" in brief and "declared" in brief, "the shape of the fix should survive"


def test_failing_test_lessons_never_cross_repositories(store, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    t = Task("failing_test", "failed: test_vat", "", "tests/test_tax.py", 100,
             evidence="assert gross(250.0, 13.5) == 283.75 AssertionError vat rate percentage")
    store.learn(str(a), delivered(t, summary="DONE: multiply by (1 + rate/100)"))
    assert store.brief_section(str(b), t) == ""
    assert store.brief_section(str(a), t) != ""
