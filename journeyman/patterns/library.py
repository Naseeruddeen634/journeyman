"""The pattern library: what an AI engineer actually chooses between.

The job is not writing the code. The job is knowing which of a dozen known
architectures fits the problem in front of you, and more importantly which ones
do not, because most of the cost of a bad agent system is paid at the point
somebody reached for multi-agent orchestration on a problem that needed one
function call.

Each entry carries the thing a senior engineer would say out loud: when it fits,
when it does not, what it costs, and how it fails. `reject_reason` is as
load-bearing as `when_to_use`. An architect that only knows what to pick is a
brochure.

Taxonomy follows the agent patterns in "30 Agents Every AI Engineer Must Build"
(Packt), mapped onto the constructs Strands actually provides.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Pattern:
    key: str
    name: str
    summary: str
    when_to_use: list[str]
    when_not_to_use: list[str]
    strands_construct: str
    cost: str               # "low" | "medium" | "high" - latency and tokens
    failure_modes: list[str]
    needs: list[str] = field(default_factory=list)   # what must exist to use it
    deterministic_grading: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "summary": self.summary,
            "when_to_use": self.when_to_use, "when_not_to_use": self.when_not_to_use,
            "strands_construct": self.strands_construct, "cost": self.cost,
            "failure_modes": self.failure_modes, "needs": self.needs,
        }


PATTERNS: dict[str, Pattern] = {}


def _p(**kw) -> None:
    p = Pattern(**kw)
    PATTERNS[p.key] = p


_p(
    key="direct_call",
    name="Direct model call",
    summary="One prompt, one response, no tools and no loop.",
    when_to_use=[
        "the task is a single transformation: summarise, classify, rewrite",
        "there is nothing to look up and nothing to act on",
    ],
    when_not_to_use=[
        "the task needs current or private data the model cannot know",
        "the output must be checked against something",
    ],
    strands_construct="Agent(system_prompt=...) with no tools",
    cost="low",
    failure_modes=["silently confident on things it cannot know"],
)

_p(
    key="structured_extraction",
    name="Structured extraction",
    summary="Pull named fields out of unstructured text into a typed object.",
    when_to_use=[
        "the output is a fixed set of fields",
        "correctness is checkable field by field",
        "documents are messy but the target schema is stable",
    ],
    when_not_to_use=[
        "the answer is a judgement rather than a value",
        "the schema changes per document",
    ],
    strands_construct="Agent(structured_output_model=PydanticModel), or plain code when the format is regular",
    cost="low",
    failure_modes=[
        "hallucinating a plausible value instead of returning null",
        "silently picking the wrong one of several similar numbers on the page",
    ],
    deterministic_grading=True,
)

_p(
    key="tool_use",
    name="Tool use",
    summary="The model chooses which function to call and with what arguments.",
    when_to_use=[
        "the work is done by code, and the hard part is deciding which code to run",
        "results must be real rather than generated",
    ],
    when_not_to_use=[
        "there is only ever one action: just call it directly",
        "the tool surface is so large the model cannot choose reliably",
    ],
    strands_construct="@tool functions passed to Agent(tools=[...])",
    cost="medium",
    failure_modes=["tool-choice thrash", "arguments that typecheck but are wrong"],
    needs=["at least one real operation to perform"],
)

_p(
    key="rag_hybrid",
    name="Retrieval augmented generation, hybrid",
    summary="Retrieve from a corpus with both dense and keyword search, then answer over what came back.",
    when_to_use=[
        "answers must come from a specific body of documents",
        "the corpus is too large for the context window",
        "exact terms matter, which is why keyword search stays in the mix",
    ],
    when_not_to_use=[
        "there is no corpus. This is the most common wrong choice",
        "the corpus fits in context: just put it in context",
        "the question is computational rather than factual",
    ],
    strands_construct="@tool retrieval functions over a vector store plus BM25, called by an Agent",
    cost="medium",
    failure_modes=[
        "retrieving plausible but irrelevant chunks and answering confidently over them",
        "chunk boundaries splitting the answer in half",
    ],
    needs=["a document corpus", "an embedding model"],
)

_p(
    key="planning_decomposition",
    name="Planning and decomposition",
    summary="Break a goal into ordered subtasks, then execute them.",
    when_to_use=[
        "the goal needs several dependent steps",
        "the steps are not known until the goal is read",
    ],
    when_not_to_use=[
        "the steps are always the same: hard-code the sequence, it is cheaper and more reliable",
    ],
    strands_construct="GraphBuilder with a planner node feeding worker nodes",
    cost="high",
    failure_modes=["plans that look reasonable and are not executable", "step drift"],
)

_p(
    key="workflow_graph",
    name="Explicit workflow graph",
    summary="A fixed topology of specialised nodes with typed handoffs.",
    when_to_use=[
        "the stages are known in advance and always run in the same shape",
        "a stage's outcome must route execution rather than be advice",
    ],
    when_not_to_use=["the shape genuinely varies per input: use planning instead"],
    strands_construct="GraphBuilder.add_node / add_edge with conditional edges",
    cost="medium",
    failure_modes=[
        "fan-in with uneven path lengths executing a node twice",
        "one node raising and taking the whole graph down",
    ],
)

_p(
    key="evolution_loop",
    name="Evolution loop",
    summary="Generate a candidate, measure it, keep what wins, repeat.",
    when_to_use=[
        "there is a measurable score",
        "the search space is small enough to hill-climb",
        "a human would otherwise tweak this by hand and by feel",
    ],
    when_not_to_use=[
        "there is no metric. Optimising against a metric you do not have is the "
        "single most common failure in this space",
        "each evaluation is slow or expensive",
    ],
    strands_construct="Cyclic GraphBuilder: an edge from the selector back to the mutator, bounded by set_max_node_executions",
    cost="high",
    failure_modes=["overfitting the eval set", "reward hacking: scoring well without solving the task"],
    needs=["a measurement function", "an eval set with a held-out split"],
)

_p(
    key="verification",
    name="Verification and validation",
    summary="A second pass whose only job is to try to break the first one.",
    when_to_use=[
        "being wrong is expensive",
        "the output is checkable against rules, types or held-out data",
    ],
    when_not_to_use=["the check would just be the same model agreeing with itself"],
    strands_construct="A dedicated node after the producer, plus @tool checks that run real assertions",
    cost="medium",
    failure_modes=["a verifier that always passes, which is worse than no verifier"],
)

_p(
    key="memory_augmented",
    name="Memory augmented",
    summary="Carry what was learned across runs, not just across turns.",
    when_to_use=[
        "the same class of problem recurs",
        "earlier outcomes should change later behaviour",
    ],
    when_not_to_use=["every run is genuinely independent: memory is then just stale state"],
    strands_construct="FileSessionManager or S3SessionManager at graph level, plus an explicit store",
    cost="low",
    failure_modes=["recalling a superficially similar case and applying the wrong lesson"],
)

_p(
    key="multi_agent_swarm",
    name="Multi-agent swarm",
    summary="Several agents hand off to each other without a fixed topology.",
    when_to_use=[
        "genuinely separate expertises, and who acts next depends on what was found",
    ],
    when_not_to_use=[
        "a graph would do. Most 'multi-agent' systems are a graph with extra cost and less determinism",
        "the agents share one knowledge base and one goal: that is one agent",
    ],
    strands_construct="Swarm(nodes=[...])",
    cost="high",
    failure_modes=["handoff loops", "agents relitigating each other's decisions"],
)

_p(
    key="human_in_the_loop",
    name="Human in the loop",
    summary="Stop and ask a person at the point only a person can decide.",
    when_to_use=[
        "the decision is irreversible or expensive",
        "domain knowledge exists that is nowhere in the data",
    ],
    when_not_to_use=["asking on every step, which trains the user to click through without reading"],
    strands_construct="An interrupt or an explicit approval node before the side effect",
    cost="low",
    failure_modes=["approval fatigue"],
)

_p(
    key="causal_decision",
    name="Causal decision",
    summary="Separate what causes an outcome from what merely predicts it.",
    when_to_use=[
        "the user is deciding whether to DO something, not forecasting",
        "the data is observational rather than randomised",
    ],
    when_not_to_use=["the question is genuinely predictive: a forecast does not need a causal graph"],
    strands_construct="Tools implementing identification and estimation, with the identifiability check as a conditional edge",
    cost="medium",
    failure_modes=["estimating an effect that is not identifiable and reporting it with a caveat instead of refusing"],
    needs=["observational data with a treatment that varies"],
)


# ------------------------------------------------------------------ scoring

# Signals in a goal statement that argue for or against each pattern. Kept as
# data so the selection is inspectable and testable rather than buried in a
# prompt, and so offline mode can select without a model.
SIGNALS: dict[str, dict[str, list[str]]] = {
    "structured_extraction": {
        "for": ["extract", "pull out", "parse", "fields", "invoice", "receipt",
                "structured", "schema", "json from", "scrape fields"],
        "against": ["chat", "converse", "summarise", "summarize"],
    },
    "rag_hybrid": {
        "for": ["documents", "corpus", "knowledge base", "search our", "answer questions about",
                "manuals", "policy", "wiki", "pdfs", "retrieval"],
        "against": ["no documents", "compute", "calculate"],
    },
    "tool_use": {
        "for": ["call", "api", "look up", "fetch", "query", "database", "action", "book", "send"],
        "against": [],
    },
    "evolution_loop": {
        "for": ["improve", "optimi", "better", "tune", "accuracy", "score", "evolve", "self-improving"],
        "against": [],
    },
    "verification": {
        "for": ["verify", "check", "validate", "prove", "make sure", "accurate", "correct", "test"],
        "against": [],
    },
    "planning_decomposition": {
        "for": ["multi-step", "workflow", "pipeline", "then", "plan", "orchestrate"],
        "against": [],
    },
    "multi_agent_swarm": {
        "for": ["multi-agent", "agents collaborate", "team of agents", "swarm"],
        "against": [],
    },
    "causal_decision": {
        "for": ["did it work", "impact", "effect of", "caused", "should we", "attribution"],
        "against": ["forecast", "predict next"],
    },
    "memory_augmented": {
        "for": ["remember", "across runs", "learn from", "history", "previous"],
        "against": [],
    },
    "human_in_the_loop": {
        "for": ["approve", "review before", "sign off", "irreversible", "confirm"],
        "against": [],
    },
    "direct_call": {"for": ["summarise", "summarize", "rewrite", "classify", "translate"], "against": []},
    "workflow_graph": {"for": ["stages", "graph", "route", "branch", "depending on"], "against": []},
}


def score_patterns(goal: str, available: dict[str, bool] | None = None) -> list[tuple[str, float, list[str]]]:
    """Rank patterns against a goal statement.

    ``available`` says what actually exists (a corpus, a metric, and so on), so a
    pattern whose prerequisites are missing is ruled out rather than merely
    ranked low. That rule-out is the judgement the library exists to encode.
    """
    have = available or {}
    ranked: list[tuple[str, float, list[str]]] = []
    low = goal.lower()

    for key, pattern in PATTERNS.items():
        sig = SIGNALS.get(key, {"for": [], "against": []})
        hits = [w for w in sig["for"] if w in low]
        misses = [w for w in sig["against"] if w in low]
        score = len(hits) * 1.0 - len(misses) * 1.5
        reasons = [f"goal mentions {w!r}" for w in hits]

        blocked = [n for n in pattern.needs if not have.get(n, False)]
        if blocked and have:
            score -= 10.0
            reasons.append("ruled out: needs " + ", ".join(blocked))
        ranked.append((key, score, reasons))

    return sorted(ranked, key=lambda t: -t[1])


def rejected_with_reasons(goal: str, chosen: list[str], available: dict[str, bool] | None = None,
                          limit: int = 4) -> list[dict]:
    """Why the obvious alternatives were not chosen. The interesting half."""
    out = []
    for key, score, reasons in score_patterns(goal, available):
        if key in chosen or len(out) >= limit:
            continue
        p = PATTERNS[key]
        blocked = [n for n in p.needs if not (available or {}).get(n, False)]
        why = (f"needs {', '.join(blocked)}, which this task does not have"
               if blocked and available else p.when_not_to_use[0] if p.when_not_to_use else "not indicated")
        out.append({"pattern": p.name, "key": key, "why_not": why})
    return out
