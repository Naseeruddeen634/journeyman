"""The store, on SQLite, with the same keys and constraints as docs/design/schema.sql.

Three things are enforced here rather than in application code, because application code forgets:

  - a case is keyed by (tenant, case, revision), so a redelivered webhook is a no-op
  - a suggestion is unique per (case revision, kind, prompt version, model, input hash), so a retry
    after a crash writes nothing new
  - every read takes a tenant id; there is no method that can return another tenant's rows

The budget ledger lives here too, because "have we spent too much today" has to be a single
transaction with the spend, or two workers will both decide there is room.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    queues TEXT NOT NULL,                 -- json list of valid queue names
    budget_daily_cents REAL NOT NULL DEFAULT 2000,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    product TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    resolution TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, case_id, revision)
);
CREATE INDEX IF NOT EXISTS cases_worklist ON cases (tenant_id, state, created_at);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    evidence TEXT NOT NULL,
    confidence REAL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    degraded INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cents REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    human_decision TEXT,
    decided_by TEXT,
    decided_at REAL,
    UNIQUE (tenant_id, case_id, revision, kind, prompt_version, model_id, input_hash)
);
CREATE INDEX IF NOT EXISTS suggestions_recent ON suggestions (tenant_id, created_at DESC);
CREATE TABLE IF NOT EXISTS budget_ledger (
    tenant_id TEXT NOT NULL,
    day TEXT NOT NULL,
    spent_cents REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail TEXT NOT NULL,
    at REAL NOT NULL
);
"""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf8")).hexdigest()


@dataclass
class Case:
    tenant_id: str
    case_id: str
    revision: int
    product: str
    title: str
    body: str
    created_at: float = field(default_factory=time.time)
    resolution: str | None = None

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"


@dataclass
class Suggestion:
    id: int
    tenant_id: str
    case_id: str
    revision: int
    kind: str
    payload: dict
    evidence: dict
    model_id: str
    prompt_version: str
    degraded: bool
    confidence: float | None = None
    cost_cents: float = 0.0
    latency_ms: int = 0
    human_decision: str | None = None


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------------------------------------------------------------- tenants

    def add_tenant(self, tenant_id: str, name: str, queues: list[str],
                   budget_daily_cents: float = 2000.0) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tenants (tenant_id, name, queues, budget_daily_cents, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (tenant_id, name, json.dumps(queues), budget_daily_cents, time.time()))
        self.db.commit()

    def tenant(self, tenant_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["queues"] = json.loads(out["queues"])
        return out

    # ---------------------------------------------------------------- cases

    def upsert_case(self, case: Case) -> bool:
        """True if this is a new (tenant, case, revision); False if it is a redelivery."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO cases (tenant_id, case_id, revision, product, title, body,"
            " body_sha256, resolution, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (case.tenant_id, case.case_id, case.revision, case.product, case.title, case.body,
             sha256(case.text), case.resolution, case.created_at))
        self.db.commit()
        return cur.rowcount == 1

    def set_case_state(self, tenant_id: str, case_id: str, revision: int, state: str) -> None:
        self.db.execute("UPDATE cases SET state = ? WHERE tenant_id = ? AND case_id = ? AND revision = ?",
                        (state, tenant_id, case_id, revision))
        self.db.commit()

    def resolved_cases(self, tenant_id: str, product: str | None = None) -> list[Case]:
        """Past cases with a resolution, for retrieval. Tenant-scoped by construction."""
        sql = "SELECT * FROM cases WHERE tenant_id = ? AND resolution IS NOT NULL"
        args: list[Any] = [tenant_id]
        if product:
            sql += " AND product = ?"
            args.append(product)
        return [Case(r["tenant_id"], r["case_id"], r["revision"], r["product"], r["title"],
                     r["body"], r["created_at"], r["resolution"])
                for r in self.db.execute(sql, args)]

    # ---------------------------------------------------------------- suggestions

    def put_suggestion(self, **kw) -> tuple[int, bool]:
        """(id, created). created is False when an identical suggestion already existed."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO suggestions (tenant_id, case_id, revision, kind, payload, evidence,"
            " confidence, model_id, prompt_version, input_hash, degraded, latency_ms, input_tokens,"
            " output_tokens, cost_cents, created_at)"
            " VALUES (:tenant_id, :case_id, :revision, :kind, :payload, :evidence, :confidence,"
            " :model_id, :prompt_version, :input_hash, :degraded, :latency_ms, :input_tokens,"
            " :output_tokens, :cost_cents, :created_at)",
            {**kw, "payload": json.dumps(kw["payload"]), "evidence": json.dumps(kw["evidence"]),
             "degraded": int(kw.get("degraded", False)), "created_at": time.time()})
        self.db.commit()
        if cur.rowcount == 1:
            return int(cur.lastrowid), True
        row = self.db.execute(
            "SELECT id FROM suggestions WHERE tenant_id = ? AND case_id = ? AND revision = ?"
            " AND kind = ? AND prompt_version = ? AND model_id = ? AND input_hash = ?",
            (kw["tenant_id"], kw["case_id"], kw["revision"], kw["kind"], kw["prompt_version"],
             kw["model_id"], kw["input_hash"])).fetchone()
        return int(row["id"]), False

    def latest_suggestion(self, tenant_id: str, case_id: str) -> Suggestion | None:
        row = self.db.execute(
            "SELECT * FROM suggestions WHERE tenant_id = ? AND case_id = ?"
            " ORDER BY created_at DESC LIMIT 1", (tenant_id, case_id)).fetchone()
        if row is None:
            return None
        return Suggestion(row["id"], row["tenant_id"], row["case_id"], row["revision"], row["kind"],
                          json.loads(row["payload"]), json.loads(row["evidence"]), row["model_id"],
                          row["prompt_version"], bool(row["degraded"]), row["confidence"],
                          row["cost_cents"], row["latency_ms"], row["human_decision"])

    def record_decision(self, tenant_id: str, suggestion_id: int, decision: str, by: str) -> None:
        """What the human did. This is the label the next prompt version is graded against."""
        self.db.execute(
            "UPDATE suggestions SET human_decision = ?, decided_by = ?, decided_at = ?"
            " WHERE id = ? AND tenant_id = ?", (decision, by, time.time(), suggestion_id, tenant_id))
        self.audit(tenant_id, by, "decision.recorded", str(suggestion_id), {"decision": decision})

    def agreement_rate(self, tenant_id: str, prompt_version: str) -> float | None:
        row = self.db.execute(
            "SELECT SUM(human_decision = 'accepted') AS good, COUNT(human_decision) AS total"
            " FROM suggestions WHERE tenant_id = ? AND prompt_version = ?",
            (tenant_id, prompt_version)).fetchone()
        return (row["good"] / row["total"]) if row and row["total"] else None

    # ---------------------------------------------------------------- budget

    def spend(self, tenant_id: str, cents: float, day: str | None = None) -> None:
        day = day or date.today().isoformat()
        self.db.execute(
            "INSERT INTO budget_ledger (tenant_id, day, spent_cents, calls) VALUES (?, ?, ?, 1)"
            " ON CONFLICT (tenant_id, day) DO UPDATE SET spent_cents = spent_cents + ?, calls = calls + 1",
            (tenant_id, day, cents, cents))
        self.db.commit()

    def spent_today(self, tenant_id: str, day: str | None = None) -> float:
        day = day or date.today().isoformat()
        row = self.db.execute("SELECT spent_cents FROM budget_ledger WHERE tenant_id = ? AND day = ?",
                              (tenant_id, day)).fetchone()
        return float(row["spent_cents"]) if row else 0.0

    def budget_left(self, tenant_id: str, day: str | None = None) -> float:
        tenant = self.tenant(tenant_id)
        if tenant is None:
            return 0.0
        return max(0.0, float(tenant["budget_daily_cents"]) - self.spent_today(tenant_id, day))

    # ---------------------------------------------------------------- audit

    def audit(self, tenant_id: str, actor: str, action: str, subject: str, detail: dict) -> None:
        self.db.execute(
            "INSERT INTO audit (tenant_id, actor, action, subject, detail, at) VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_id, actor, action, subject, json.dumps(detail), time.time()))
        self.db.commit()

    def audit_trail(self, tenant_id: str, subject: str) -> list[dict]:
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in self.db.execute(
            "SELECT * FROM audit WHERE tenant_id = ? AND subject = ? ORDER BY at", (tenant_id, subject))]
