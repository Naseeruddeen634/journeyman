"""A queue with the properties the design depends on, small enough to read in one sitting.

Azure Service Bus and SQS both give at-least-once delivery, a visibility timeout, a delivery count
and a dead-letter queue. Those four things change how the rest of the system must be written, so
the stand-in implements them rather than pretending messages arrive exactly once:

  - `lease` hides a message for a visibility timeout instead of removing it, so a worker that dies
    mid-job does not lose the case
  - `nack` and lease expiry both count as a delivery attempt
  - after `max_attempts`, the message goes to the dead-letter queue with the last error, where an
    alert on depth > 0 will find it

Persistence is deliberately out of scope: the real deployment uses a broker. What matters here is
that the handler code is written against redelivery, not against a promise of exactly-once.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    id: int
    body: dict[str, Any]
    attempts: int = 0
    enqueued_at: float = field(default_factory=time.time)
    visible_at: float = 0.0


@dataclass
class DeadLetter:
    message: Message
    error: str
    at: float = field(default_factory=time.time)


class Queue:
    """In-process stand-in for Service Bus / SQS. Thread-safe."""

    def __init__(self, max_attempts: int = 5, visibility_timeout: float = 30.0,
                 clock=time.time) -> None:
        self.max_attempts = max_attempts
        self.visibility_timeout = visibility_timeout
        self._clock = clock
        self._ids = itertools.count(1)
        self._messages: list[Message] = []
        self._dead: list[DeadLetter] = []
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- producer

    def enqueue(self, body: dict[str, Any]) -> Message:
        with self._lock:
            msg = Message(id=next(self._ids), body=body, enqueued_at=self._clock())
            self._messages.append(msg)
            return msg

    # ---------------------------------------------------------------- consumer

    def lease(self) -> Message | None:
        """Take the oldest visible message and hide it for the visibility timeout."""
        now = self._clock()
        with self._lock:
            for msg in self._messages:
                if msg.visible_at <= now:
                    msg.attempts += 1
                    msg.visible_at = now + self.visibility_timeout
                    return msg
            return None

    def ack(self, msg: Message) -> None:
        with self._lock:
            self._messages = [m for m in self._messages if m.id != msg.id]

    def nack(self, msg: Message, error: str = "") -> None:
        """Hand the message back. Past max_attempts it is dead-lettered instead."""
        with self._lock:
            if msg.attempts >= self.max_attempts:
                self._messages = [m for m in self._messages if m.id != msg.id]
                self._dead.append(DeadLetter(msg, error or "max attempts reached", self._clock()))
            else:
                msg.visible_at = 0.0          # immediately visible again

    # ---------------------------------------------------------------- operations

    def depth(self) -> int:
        with self._lock:
            return len(self._messages)

    def oldest_age(self) -> float:
        """Age of the oldest queued message. The alert that matters is on this, not on depth:
        a deep queue that is draining is fine; a shallow queue that is not moving is not."""
        with self._lock:
            if not self._messages:
                return 0.0
            return self._clock() - min(m.enqueued_at for m in self._messages)

    def dead_letters(self) -> list[DeadLetter]:
        with self._lock:
            return list(self._dead)
