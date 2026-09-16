"""Journeyman for Support: case triage as a service.

The design is in docs/design/journeyman-for-support.md. This package is the part of it that
runs, so the claims that would otherwise be hand-waving are executable:

  - at-least-once delivery with idempotent handling, so a redelivered webhook does no work twice
  - deterministic validation of model output, so a bad suggestion is rejected, not posted
  - a per-tenant budget that degrades the service instead of overspending
  - retries with a dead-letter queue, so one poisoned case cannot block the others
  - tenant isolation in the store and in the prompt inputs

Everything here is storage- and transport-agnostic: SQLite and an in-process queue stand in for
Azure SQL and Service Bus. The interfaces are the same shape, which is the point of the exercise.
"""

from .queue import DeadLetter, Message, Queue
from .store import Case, Store, Suggestion
from .triage import TriageResult, TriageWorker, ValidationError, validate_triage

__all__ = ["Case", "DeadLetter", "Message", "Queue", "Store", "Suggestion", "TriageResult",
           "TriageWorker", "ValidationError", "validate_triage"]
