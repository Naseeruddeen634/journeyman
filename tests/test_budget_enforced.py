"""The budgets the README promises, enforced against a model that never stops.

The threat is not a model that misbehaves dramatically. It is one that keeps
calling tools politely, forever, at 3am, on a metered API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strands import Agent, tool  # noqa: E402
from strands.models.model import Model  # noqa: E402

from journeyman.autonomy.guardrails import Budget, BudgetGuard  # noqa: E402


class NeverStops(Model):
    """Calls a tool on every single turn and never ends the conversation."""

    def __init__(self):
        self.turns = 0
        self.cfg = {}

    def update_config(self, **kw):
        self.cfg.update(kw)

    def get_config(self):
        return self.cfg

    async def structured_output(self, *a, **k):
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        self.turns += 1
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": f"t{self.turns}", "name": "poke"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"n": self.turns})}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


@tool
def poke(n: int) -> str:
    """Do nothing useful.

    Args:
        n: a counter
    """
    return f"poked {n}"


def _run(budget, clock=None, paid=False):
    model = NeverStops()
    guard = BudgetGuard(budget, paid=paid, clock=clock)
    agent = Agent(model=model, tools=[poke], hooks=[guard], callback_handler=None)
    out = str(agent("go"))
    return model, guard, out


def test_turn_budget_stops_a_model_that_never_stops():
    model, guard, out = _run(Budget(max_iterations=5, max_minutes=999))
    assert model.turns == 5, f"model ran {model.turns} turns against a limit of 5"
    assert "iteration budget" in guard.stopped_by
    assert out.startswith("STUCK")


def test_time_budget_stops_it_even_under_the_turn_limit():
    now = [0.0]

    def clock():
        now[0] += 30.0          # every check advances half a minute
        return now[0]

    model, guard, out = _run(Budget(max_iterations=1000, max_minutes=2), clock=clock)
    assert "minute limit" in guard.stopped_by
    assert model.turns < 10, f"ran {model.turns} turns past a 2 minute limit"


def test_paid_calls_are_metered_and_local_calls_are_not():
    _, paid_guard, _ = _run(Budget(max_iterations=100, max_heavy_calls=3), paid=True)
    assert "heavy-model call budget" in paid_guard.stopped_by
    assert paid_guard.model_calls == 3

    _, free_guard, _ = _run(Budget(max_iterations=6, max_heavy_calls=3), paid=False)
    assert "iteration budget" in free_guard.stopped_by, \
        "a free local model should hit the turn limit, not the paid-call limit"


def test_a_stopped_agent_ends_cleanly_rather_than_raising():
    """The shift's own verification has to run after a budget stop."""
    model, guard, out = _run(Budget(max_iterations=2))
    assert isinstance(out, str) and guard.stopped_by


class StreamsForever(Model):
    """A single generation that never finishes. The between-calls hook cannot see it."""

    def __init__(self):
        self.cfg = {}

    def update_config(self, **kw):
        self.cfg.update(kw)

    def get_config(self):
        return self.cfg

    async def structured_output(self, *a, **k):
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        import asyncio

        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        while True:
            yield {"contentBlockDelta": {"delta": {"text": "and again "}}}
            await asyncio.sleep(0.01)


def test_the_watchdog_interrupts_a_generation_in_progress():
    """What work_one does: a timer calls Agent.cancel() from another thread."""
    import threading
    import time

    agent = Agent(model=StreamsForever(), callback_handler=None)
    timer = threading.Timer(0.5, agent.cancel)
    timer.start()
    started = time.monotonic()
    try:
        agent("go")
    except Exception:
        pass            # cancelled is the outcome; how it surfaces is the SDK's business
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10, "a runaway generation must be interruptible"


def test_the_local_brain_sets_a_real_context_window_and_bounded_output(monkeypatch):
    """Ollama defaults to 4096 tokens and, when full, kept 4 tokens and discarded
    2045: the system prompt and the task. Measured in Ollama's own log."""
    import importlib

    import journeyman.brain.models as models
    importlib.reload(models)
    m = models.local_brain()
    assert m.config["options"]["num_ctx"] >= 16384
    assert 0 < m.config["max_tokens"] <= 8192
    assert m.config["keep_alive"]

    monkeypatch.setenv("JOURNEYMAN_LOCAL_CTX", "32768")
    importlib.reload(models)
    assert models.local_brain().config["options"]["num_ctx"] == 32768
    monkeypatch.delenv("JOURNEYMAN_LOCAL_CTX")
    importlib.reload(models)
