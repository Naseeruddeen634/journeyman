"""Wiring: ingest, the worker loop, and the numbers a lead and an on-call engineer need.

`Ingest.accept` is what the Dataverse webhook calls. It does two durable things and returns:
write the case row, put a message on the queue. Nothing that can be slow or fail (retrieval, a
model call, writing back to Dataverse) happens on that path, so a model outage cannot make the
support system's webhook time out.

`Worker.run_once` is the consumer: lease, handle, ack. A handler that raises nacks the message,
which counts an attempt and eventually dead-letters it, so one malformed case cannot block a queue.

`health` is the shape of the dashboard and of the alerts: queue age rather than queue depth,
validation failure rate, degraded share, cost against budget, and agreement rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .queue import Message, Queue
from .store import Case, Store
from .triage import PROMPT_VERSION, TriageResult, TriageWorker


@dataclass
class Ingest:
    store: Store
    queue: Queue
    metrics: dict = field(default_factory=dict)

    def accept(self, event: dict) -> dict:
        """Validate, persist, enqueue. Idempotent on (tenant, case, revision)."""
        required = ("tenant_id", "case_id", "revision", "product", "title", "body")
        missing = [k for k in required if not event.get(k) and event.get(k) != 0]
        if missing:
            raise ValueError(f"missing field(s): {', '.join(missing)}")
        if self.store.tenant(event["tenant_id"]) is None:
            raise ValueError(f"unknown tenant {event['tenant_id']!r}")

        case = Case(tenant_id=event["tenant_id"], case_id=event["case_id"],
                    revision=int(event["revision"]), product=event["product"],
                    title=event["title"], body=event["body"])
        fresh = self.store.upsert_case(case)
        self.metrics["ingested"] = self.metrics.get("ingested", 0) + 1
        if not fresh:
            # A redelivered webhook, or Dataverse repeating itself. Do not queue it again.
            self.metrics["duplicates"] = self.metrics.get("duplicates", 0) + 1
            return {"accepted": True, "duplicate": True, "case_id": case.case_id}
        self.queue.enqueue({"tenant_id": case.tenant_id, "case_id": case.case_id,
                            "revision": case.revision})
        return {"accepted": True, "duplicate": False, "case_id": case.case_id}


@dataclass
class Worker:
    store: Store
    queue: Queue
    triage: TriageWorker
    metrics: dict = field(default_factory=dict)
    writeback: Callable[[TriageResult], None] | None = None

    def run_once(self) -> TriageResult | None:
        msg: Message | None = self.queue.lease()
        if msg is None:
            return None
        try:
            case = self._load(msg.body)
            result = self.triage.handle(case)
            if self.writeback is not None:
                self.writeback(result)          # the only component that writes to the support system
        except Exception as exc:
            # Hand it back. After max_attempts the queue dead-letters it, and the alert on
            # dead-letter depth is the one that pages someone.
            self.metrics["handler_errors"] = self.metrics.get("handler_errors", 0) + 1
            self.queue.nack(msg, f"{type(exc).__name__}: {exc}")
            return None
        self.queue.ack(msg)
        return result

    def drain(self, limit: int = 1000) -> list[TriageResult]:
        out = []
        for _ in range(limit):
            result = self.run_once()
            if result is None and self.queue.depth() == 0:
                break
            if result is not None:
                out.append(result)
        return out

    def _load(self, body: dict) -> Case:
        row = self.store.db.execute(
            "SELECT * FROM cases WHERE tenant_id = ? AND case_id = ? AND revision = ?",
            (body["tenant_id"], body["case_id"], body["revision"])).fetchone()
        if row is None:
            raise LookupError(f"case {body['case_id']} revision {body['revision']} is not in the store")
        return Case(row["tenant_id"], row["case_id"], row["revision"], row["product"], row["title"],
                    row["body"], row["created_at"], row["resolution"])


# --------------------------------------------------------------------- operations


ALERTS = {
    "queue_age_seconds": (600, "the oldest case has been waiting more than 10 minutes"),
    "dead_letters": (1, "a case could not be handled after every retry"),
    "validation_failure_rate": (0.1, "more than 10% of model answers are being rejected"),
    "budget_used": (1.0, "a tenant has spent its daily budget; triage is degraded"),
}


def health(store: Store, queue: Queue, metrics: dict, tenant_id: str) -> dict:
    """What the dashboard shows and what the alerts read. One function, so they cannot disagree."""
    calls = metrics.get("model_calls", 0)
    failures = metrics.get("validation_failures", 0)
    suggestions = metrics.get("suggestions", 0)
    degraded = metrics.get("degraded", 0)
    tenant = store.tenant(tenant_id) or {"budget_daily_cents": 0}
    budget = float(tenant["budget_daily_cents"]) or 1.0
    spent = store.spent_today(tenant_id)
    state = {
        "queue_depth": queue.depth(),
        "queue_age_seconds": round(queue.oldest_age(), 1),
        "dead_letters": len(queue.dead_letters()),
        "model_calls": calls,
        "model_errors": metrics.get("model_errors", 0),
        "validation_failure_rate": round(failures / calls, 3) if calls else 0.0,
        "degraded_share": round(degraded / (suggestions + degraded), 3) if (suggestions + degraded) else 0.0,
        "spent_cents": round(spent, 4),
        "budget_used": round(spent / budget, 3),
        "agreement_rate": store.agreement_rate(tenant_id, PROMPT_VERSION),
    }
    state["alerts"] = [f"{name}: {why}" for name, (threshold, why) in ALERTS.items()
                       if state.get(name) is not None and state[name] >= threshold]
    return state
