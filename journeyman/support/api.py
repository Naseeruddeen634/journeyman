"""The read/write API the dashboard talks to.

Standard library only. This is the shape of the service, not the production server: in a real
deployment this is ASP.NET Core or FastAPI behind a gateway that terminates TLS and validates the
Entra ID token. What is worth pinning down here, and what the tests check, is the contract:

  - every route is tenant-scoped, and the tenant comes from the caller's identity
  - a decision is recorded once; a second one is a conflict, not a silent overwrite
  - readiness is separate from liveness and names the dependency that is failing

`/v1/ingest/case` is here too, so the demo can post a case and watch it flow through.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .queue import Queue
from .service import Ingest, health
from .store import Store


class Api:
    """Routing and behaviour, independent of the HTTP server, so it can be tested directly."""

    def __init__(self, store: Store, queue: Queue, metrics: dict, tenant_of_caller: str = "acme") -> None:
        self.store = store
        self.queue = queue
        self.metrics = metrics
        self.ingest = Ingest(store, queue, metrics)
        # Stands in for the tenant claim on the caller's token. Never read from the path or query,
        # so a caller cannot reach another tenant's data by editing a URL.
        self.tenant_of_caller = tenant_of_caller

    def handle(self, method: str, path: str, query: dict[str, list[str]], body: dict | None) -> tuple[int, Any]:
        parts = [p for p in path.strip("/").split("/") if p]
        if parts[:1] != ["v1"]:
            return 404, {"error": "unknown path"}
        route = parts[1:]

        if method == "GET" and route == ["health"]:
            return 200, {"status": "up"}                      # liveness: the process answers
        if method == "GET" and route == ["ready"]:
            return self._ready()
        if method == "GET" and route[:1] == ["health"] and len(route) == 2:
            return 200, health(self.store, self.queue, self.metrics, self.tenant_of_caller)
        if method == "GET" and route == ["suggestions"]:
            return 200, self._suggestions(int(query.get("limit", ["20"])[0]))
        if method == "POST" and len(route) == 3 and route[0] == "suggestions" and route[2] == "decision":
            return self._decide(int(route[1]), body or {})
        if method == "POST" and route == ["ingest", "case"]:
            return self._ingest(body or {})
        return 404, {"error": "unknown path"}

    # ------------------------------------------------------------------ routes

    def _ready(self) -> tuple[int, Any]:
        checks = {"database": "ok", "queue": "ok"}
        try:
            self.store.db.execute("SELECT 1").fetchone()
        except Exception as exc:
            checks["database"] = f"{type(exc).__name__}: {exc}"
        if len(self.queue.dead_letters()) > 0:
            checks["queue"] = f"{len(self.queue.dead_letters())} dead letter(s)"
        failing = [k for k, v in checks.items() if v != "ok"]
        return (503 if failing else 200), checks

    def _suggestions(self, limit: int) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT * FROM suggestions WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (self.tenant_of_caller, max(1, min(limit, 100)))).fetchall()
        return [{"id": r["id"], "case_id": r["case_id"], "payload": json.loads(r["payload"]),
                 "evidence": json.loads(r["evidence"]), "model_id": r["model_id"],
                 "degraded": bool(r["degraded"]), "confidence": r["confidence"],
                 "cost_cents": r["cost_cents"], "human_decision": r["human_decision"]}
                for r in rows]

    def _decide(self, suggestion_id: int, body: dict) -> tuple[int, Any]:
        decision = body.get("decision")
        if decision not in ("accepted", "edited", "rejected"):
            return 400, {"error": "decision must be accepted, edited or rejected", "field": "decision"}
        row = self.store.db.execute(
            "SELECT human_decision FROM suggestions WHERE id = ? AND tenant_id = ?",
            (suggestion_id, self.tenant_of_caller)).fetchone()
        if row is None:
            return 404, {"error": "no such suggestion"}       # another tenant's id looks missing, not forbidden
        if row["human_decision"] is not None:
            return 409, {"error": f"already decided: {row['human_decision']}"}
        self.store.record_decision(self.tenant_of_caller, suggestion_id,
                                   decision, body.get("by", "dashboard"))
        return 204, None

    def _ingest(self, body: dict) -> tuple[int, Any]:
        try:
            return 202, self.ingest.accept({**body, "tenant_id": self.tenant_of_caller})
        except ValueError as exc:
            return 400, {"error": str(exc)}


def serve(api: Api, port: int = 8787) -> ThreadingHTTPServer:
    """Start the API on a background thread and return the server so a caller can shut it down."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, payload: Any) -> None:
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            # The dashboard's dev server runs on another port; production serves both from one origin.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_OPTIONS(self) -> None:    # noqa: N802  (the stdlib spells it this way)
            self._respond(204, None)

        def do_GET(self) -> None:        # noqa: N802
            url = urlparse(self.path)
            status, payload = api.handle("GET", url.path, parse_qs(url.query), None)
            self._respond(status, payload)

        def do_POST(self) -> None:       # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._respond(400, {"error": "body is not JSON"})
                return
            status, payload = api.handle("POST", url.path, parse_qs(url.query), body)
            self._respond(status, payload)

        def log_message(self, fmt: str, *args) -> None:
            pass                          # the service logs through metrics and audit, not stderr

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
