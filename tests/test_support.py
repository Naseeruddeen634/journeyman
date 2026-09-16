"""Journeyman for Support: the design's risky claims, as tests.

Each test here corresponds to a line in docs/design/journeyman-for-support.md that would otherwise
be an assertion in a document nobody can check.
"""

import json

import pytest

from journeyman.support import Queue, Store, ValidationError, validate_triage
from journeyman.support.service import Ingest, Worker, health
from journeyman.support.store import Case
from journeyman.support.triage import PROMPT_VERSION, TriageWorker, similar_cases

QUEUES = ["billing", "identity", "platform", "how-to"]


def make_store() -> Store:
    store = Store()
    store.add_tenant("acme", "Acme", QUEUES, budget_daily_cents=10.0)
    store.add_tenant("globex", "Globex", ["general"], budget_daily_cents=10.0)
    return store


def event(case_id="c-1", revision=1, tenant="acme", title="Sign-in fails with AADSTS50011",
          body="Users cannot sign in since this morning. Error AADSTS50011 redirect mismatch."):
    return {"tenant_id": tenant, "case_id": case_id, "revision": revision, "product": "portal",
            "title": title, "body": body}


def model(answer):
    return lambda system, prompt: answer


GOOD = json.dumps({"queue": "identity", "severity": 2,
                   "summary": "Sign-in broken by a redirect URI mismatch after this morning's change.",
                   "known_issue": None})


# ------------------------------------------------------------------ ingest and idempotency

def test_a_redelivered_webhook_does_no_work_twice():
    store, q = make_store(), Queue()
    ingest = Ingest(store, q)
    first = ingest.accept(event())
    second = ingest.accept(event())          # same tenant, case and revision
    assert first["duplicate"] is False and second["duplicate"] is True
    assert q.depth() == 1, "a duplicate webhook must not queue a second triage"


def test_an_edited_case_is_a_new_revision_and_is_triaged_again():
    store, q = make_store(), Queue()
    ingest = Ingest(store, q)
    ingest.accept(event(revision=1))
    ingest.accept(event(revision=2, body="Now also failing for admins."))
    assert q.depth() == 2


def test_ingest_refuses_an_unknown_tenant_and_missing_fields():
    store, q = make_store(), Queue()
    with pytest.raises(ValueError, match="unknown tenant"):
        Ingest(store, q).accept(event(tenant="who"))
    with pytest.raises(ValueError, match="missing field"):
        Ingest(store, q).accept({"tenant_id": "acme", "case_id": "c-9"})


def test_the_same_case_triaged_twice_writes_one_suggestion():
    store, q = make_store(), Queue()
    worker = TriageWorker(store, model(GOOD))
    case = Case("acme", "c-1", 1, "portal", "t", "b")
    store.upsert_case(case)
    first, second = worker.handle(case), worker.handle(case)
    assert first.suggestion_id == second.suggestion_id
    assert second.duplicate is True


# ------------------------------------------------------------------ validating model output

@pytest.mark.parametrize("answer, reason", [
    ("not json at all", "not JSON"),
    (json.dumps({"queue": "identity", "severity": 2}), "missing field"),
    (json.dumps({"queue": "made-up", "severity": 2, "summary": "x"}), "not one of this tenant"),
    (json.dumps({"queue": "identity", "severity": 9, "summary": "x"}), "1..4"),
    (json.dumps({"queue": "identity", "severity": 2, "summary": "   "}), "empty"),
    (json.dumps({"queue": "identity", "severity": 2, "summary": "word " * 61}), "limit"),
])
def test_bad_model_answers_are_refused_with_a_readable_reason(answer, reason):
    with pytest.raises(ValidationError, match=reason):
        validate_triage(answer, QUEUES)


def test_a_fenced_answer_is_accepted_because_models_do_that():
    payload = validate_triage(f"```json\n{GOOD}\n```", QUEUES)
    assert payload["queue"] == "identity" and payload["severity"] == 2


def test_a_rejected_answer_is_not_posted_and_is_kept_for_the_eval_set():
    store = make_store()
    worker = TriageWorker(store, model('{"queue": "does-not-exist", "severity": 2, "summary": "x"}'))
    case = Case("acme", "c-1", 1, "portal", "t", "b")
    store.upsert_case(case)
    result = worker.handle(case)
    assert result.suggestion_id is None and "not one of this tenant" in result.rejected
    assert store.latest_suggestion("acme", "c-1") is None
    trail = store.audit_trail("acme", "c-1")
    assert trail[-1]["action"] == "suggestion.rejected" and trail[-1]["detail"]["raw"]


# ------------------------------------------------------------------ degrading instead of failing

def test_a_model_outage_degrades_to_retrieval_instead_of_failing_the_case():
    store = make_store()

    def broken(system, prompt):
        raise ConnectionError("provider 503")

    store.upsert_case(Case("acme", "old-1", 1, "portal", "Sign-in fails AADSTS50011",
                           "redirect mismatch", resolution="Fixed the reply URL"))
    case = Case("acme", "c-1", 1, "portal", "Sign-in fails with AADSTS50011", "redirect mismatch")
    store.upsert_case(case)
    result = TriageWorker(store, broken).handle(case)
    assert result.degraded and result.model_id == "retrieval-only"
    assert result.payload["known_issue"] == "old-1", "retrieval still helps when the model is down"


def test_the_daily_budget_stops_spending_and_the_service_keeps_working():
    store = make_store()
    calls = []
    worker = TriageWorker(store, lambda s, p: calls.append(p) or GOOD)
    store.spend("acme", 10.0)                    # the whole daily budget
    case = Case("acme", "c-1", 1, "portal", "t", "b")
    store.upsert_case(case)
    result = worker.handle(case)
    assert calls == [], "no model call once the budget is gone"
    assert result.degraded and result.suggestion_id is not None


def test_spending_is_charged_per_call_and_visible_per_tenant():
    store = make_store()
    worker = TriageWorker(store, model(GOOD))
    case = Case("acme", "c-1", 1, "portal", "t", "b")
    store.upsert_case(case)
    worker.handle(case)
    assert 0 < store.spent_today("acme") < 1.0
    assert store.spent_today("globex") == 0.0


# ------------------------------------------------------------------ the queue

def test_a_poisoned_case_is_dead_lettered_and_does_not_block_the_queue():
    store, q = make_store(), Queue(max_attempts=2, visibility_timeout=0)
    ingest = Ingest(store, q)
    ingest.accept(event(case_id="poison"))
    ingest.accept(event(case_id="fine"))
    store.db.execute("DELETE FROM cases WHERE case_id = 'poison'")   # make the handler raise
    store.db.commit()

    worker = Worker(store, q, TriageWorker(store, model(GOOD)))
    results = [worker.run_once() for _ in range(6)]

    assert [r.case_id for r in results if r] == ["fine"], "the good case still got triaged"
    dead = q.dead_letters()
    assert len(dead) == 1 and dead[0].message.body["case_id"] == "poison"
    assert "LookupError" in dead[0].error
    assert q.depth() == 0


def test_a_worker_that_dies_mid_case_does_not_lose_it():
    clock = [1000.0]
    q = Queue(visibility_timeout=30, clock=lambda: clock[0])
    q.enqueue({"case_id": "c-1"})
    leased = q.lease()
    assert q.lease() is None, "hidden while the first worker holds it"
    clock[0] += 31                                   # that worker died; the lease expires
    again = q.lease()
    assert again is not None and again.id == leased.id and again.attempts == 2


# ------------------------------------------------------------------ tenant isolation

def test_one_tenants_cases_never_reach_another_tenants_prompt():
    store = make_store()
    store.upsert_case(Case("globex", "g-1", 1, "portal", "Sign-in fails with AADSTS50011",
                           "redirect mismatch", resolution="Globex-only secret detail"))
    case = Case("acme", "c-1", 1, "portal", "Sign-in fails with AADSTS50011", "redirect mismatch")
    store.upsert_case(case)

    assert similar_cases(store, case) == [], "no neighbours: the only similar case is another tenant's"

    seen = []
    TriageWorker(store, lambda s, p: seen.append(p) or GOOD).handle(case)
    assert "Globex-only secret detail" not in seen[0]


# ------------------------------------------------------------------ end to end and operations

def test_end_to_end_ingest_triage_decision_and_health():
    store, q = make_store(), Queue()
    metrics: dict = {}
    ingest = Ingest(store, q, metrics)
    triage = TriageWorker(store, model(GOOD), metrics=metrics)
    posted = []
    worker = Worker(store, q, triage, metrics, writeback=posted.append)

    store.upsert_case(Case("acme", "old-1", 1, "portal", "Sign-in fails AADSTS50011",
                           "redirect mismatch", resolution="Fixed the reply URL"))
    ingest.accept(event())
    results = worker.drain()

    assert len(results) == 1 and len(posted) == 1
    suggestion = store.latest_suggestion("acme", "c-1")
    assert suggestion.payload["queue"] == "identity"
    assert suggestion.evidence["similar_cases"][0]["case_id"] == "old-1"
    assert suggestion.evidence["prompt_version"] == PROMPT_VERSION

    store.record_decision("acme", suggestion.id, "accepted", "engineer@acme.test")
    assert store.agreement_rate("acme", PROMPT_VERSION) == 1.0

    state = health(store, q, metrics, "acme")
    assert state["queue_depth"] == 0 and state["dead_letters"] == 0
    assert state["validation_failure_rate"] == 0.0 and state["alerts"] == []


def test_health_raises_the_alerts_an_on_call_engineer_would_want():
    store, q = make_store(), Queue(max_attempts=1, visibility_timeout=0)
    metrics = {"model_calls": 10, "validation_failures": 3}
    q.enqueue({"case_id": "stuck"})
    q.nack(q.lease(), "boom")                       # straight to the dead-letter queue
    store.spend("acme", 10.0)                       # budget gone

    state = health(store, q, metrics, "acme")
    assert state["validation_failure_rate"] == 0.3
    assert any("10% of model answers" in a for a in state["alerts"])
    assert any("could not be handled" in a for a in state["alerts"])
    assert any("daily budget" in a for a in state["alerts"])


# ------------------------------------------------------------------ the API the dashboard uses

def make_api(store=None):
    from journeyman.support.api import Api

    store = store or make_store()
    q, metrics = Queue(), {}
    return Api(store, q, metrics), store, q, metrics


def test_the_api_serves_health_and_suggestions_for_the_callers_tenant_only():
    api, store, q, metrics = make_api()
    store.upsert_case(Case("acme", "c-1", 1, "portal", "t", "b"))
    store.upsert_case(Case("globex", "g-1", 1, "portal", "t", "b"))
    TriageWorker(store, model(GOOD), metrics=metrics).handle(Case("acme", "c-1", 1, "portal", "t", "b"))
    TriageWorker(store, model(json.dumps({"queue": "general", "severity": 1, "summary": "x"})),
                 metrics=metrics).handle(Case("globex", "g-1", 1, "portal", "t", "b"))

    status, body = api.handle("GET", "/v1/suggestions", {}, None)
    assert status == 200 and [s["case_id"] for s in body] == ["c-1"], "globex rows must not appear"

    status, health_body = api.handle("GET", "/v1/health/acme", {}, None)
    assert status == 200 and health_body["queue_depth"] == 0


def test_a_decision_is_recorded_once_and_a_second_one_is_a_conflict():
    api, store, _, metrics = make_api()
    store.upsert_case(Case("acme", "c-1", 1, "portal", "t", "b"))
    result = TriageWorker(store, model(GOOD), metrics=metrics).handle(Case("acme", "c-1", 1, "portal", "t", "b"))

    assert api.handle("POST", f"/v1/suggestions/{result.suggestion_id}/decision", {},
                      {"decision": "accepted"})[0] == 204
    status, body = api.handle("POST", f"/v1/suggestions/{result.suggestion_id}/decision", {},
                              {"decision": "rejected"})
    assert status == 409 and "already decided" in body["error"]
    assert api.handle("POST", f"/v1/suggestions/{result.suggestion_id}/decision", {},
                      {"decision": "maybe"})[0] == 400
    assert api.handle("POST", "/v1/suggestions/999/decision", {}, {"decision": "accepted"})[0] == 404


def test_readiness_names_the_failing_dependency_and_liveness_does_not():
    api, _, q, _ = make_api()
    assert api.handle("GET", "/v1/health", {}, None) == (200, {"status": "up"})
    assert api.handle("GET", "/v1/ready", {}, None)[0] == 200
    q.enqueue({"case_id": "x"})
    q.max_attempts = 0
    q.nack(q.lease(), "boom")
    status, checks = api.handle("GET", "/v1/ready", {}, None)
    assert status == 503 and "dead letter" in checks["queue"]


def test_ingest_over_http_uses_the_callers_tenant_not_the_body():
    api, store, q, _ = make_api()
    status, body = api.handle("POST", "/v1/ingest/case", {},
                              {"tenant_id": "globex", "case_id": "c-9", "revision": 1,
                               "product": "portal", "title": "t", "body": "b"})
    assert status == 202 and body["accepted"] is True
    assert store.db.execute("SELECT tenant_id FROM cases WHERE case_id = 'c-9'").fetchone()[0] == "acme"
