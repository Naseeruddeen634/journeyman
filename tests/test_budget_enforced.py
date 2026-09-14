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
