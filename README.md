# Journeyman

**An AI engineer that builds it, proves it on data it has not seen, and remembers what worked.**

Built with the [Strands Agents SDK](https://strandsagents.com/) for the Agents for
Humans hackathon. Track: **Professional Agents**.

A journeyman is a qualified tradesperson who works *beside* you rather than for
you. That is the intent: this is not a chatbot you ask about AI engineering. It
does the job, produces the artifacts, and hands you the evidence.

---

## The problem

An AI engineer's day is not writing prompts. It is three harder things:

1. **Choosing the architecture.** Most of the cost of a bad agent system is paid
   the moment someone reaches for multi-agent orchestration on a problem that
   needed one function call, or reaches for RAG when there is no corpus.
2. **Knowing whether it works.** Almost nobody has an eval set, because building
   one is more work than building the thing. So people ship on feel, tweak on
   feel, and regressions land silently.
3. **Not relearning the same lesson.** The date-parsing bug you fixed in March is
   the same bug in September, in a different repo.

Journeyman does all three, end to end, and writes down what it learned.

## What it does

```bash
journeyman build "extract vendor, date and total from these invoices and prove it works"
```

A nine-node Strands graph runs:

| node | what it does |
|---|---|
| `intake` | reads the goal, works out what actually exists to build with |
| `architect` | picks the pattern, **and names the ones it rejected and why** |
| `recall` | looks for prior jobs like this one and what worked on them |
| `harness` | builds the eval set and splits off a held-out half |
| `prove` | measures the starting point and clusters the failures into sentences |
| `evolve` | one generation: target the worst cluster, mutate, measure. **This node runs once per generation.** |
| `select` | keep going, or stop. A conditional edge back to `evolve`. |
| `gate` | first and only look at the held-out half |
| `ship` | writes the artifact, the eval set, a CI regression gate, and the lessons |

You get back working code, the eval set it was measured on, a pytest regression
gate for CI, and a report showing every candidate it tried and rejected.

## The three things that make it different

### 1. It tells you what it did not build, and why

Choosing a pattern is easy. Ruling one out is the judgement.

```
    chose    Structured extraction
    chose    Verification and validation
    not      Direct model call: the task needs current or private data the model cannot know
    not      Tool use: there is only ever one action: just call it directly
    not      Planning and decomposition: the steps are always the same: hard-code
             the sequence, it is cheaper and more reliable
```

Ask it to answer questions over documents you do not have, and it rules out RAG
rather than building a retrieval pipeline over an empty corpus. That rule-out is
a test, not a claim (`test_architect_rules_out_rag_when_there_is_no_corpus`).

### 2. It reports the number you will actually get

The eval set is split once. The search never reads the held-out half. At the end
the surviving generations are scored on it and the one that *holds up* ships.

```
    before  ###################.........  68.7%
     after  ############################  100.0%

    Both measured on the half of the eval set the search never saw.
```

If the best training score does not survive held-out, it is reported as
overfitting and rejected. If nothing beats what you already had, it says so:

> No reliable improvement. Nothing found beat what you already had on data it has
> not seen, so keep what you have.

That refusal is a test too (`test_no_improvement_is_reported_as_no_improvement`),
as is the gate catching a candidate that scores by memorising the eval cases,
which is a documented failure mode of model-driven optimisation.

### 3. It gets measurably cheaper the more you use it

Same job, run twice. The second time it recalls the repairs that worked on a
similar task, **verifies each one still works**, and skips the rest of the search.

| | candidates measured | held-out result |
|---|---|---|
| first run, cold | **9** | 100% |
| second run, related task | **3** | 100% |

Nothing is taken on trust. A recalled repair is scored like any other candidate
before it is accepted; memory changes the *order* of the search, not the standard
of evidence. `test_memory_reduces_the_number_of_candidates_measured` asserts both
halves: less work, no loss of accuracy.

## The evolution loop is a cycle in the graph

Not a `for` loop inside a node.

```mermaid
flowchart LR
  I[intake] --> A[architect] --> R[recall] --> H[harness] --> P[prove] --> E[evolve]
  E --> S{select}
  S -->|still improving| E
  S -->|converged| G[gate] --> Sh[ship]
  style S fill:#2a2418,stroke:#ffc857,color:#ffc857
  style E fill:#101a14,stroke:#27543c,color:#3ddc97
```

```python
b.add_edge("select", "evolve", condition=lambda s: job.keep_evolving)
b.add_edge("select", "gate",   condition=lambda s: not job.keep_evolving)
b.reset_on_revisit(True)
b.set_max_node_executions(60)
```

One `evolve` execution is one generation, so the loop is visible in
`execution_order` and bounded by the SDK rather than by a counter you have to
trust. A real run:

```
intake -> architect -> recall -> harness -> prove
  -> evolve -> select -> evolve -> select -> evolve -> select -> evolve -> select
  -> gate -> ship
```

Both edge conditions read a value computed in Python, not text a model wrote.

## Run it

Python 3.10+. **No AWS account needed.**

```bash
git clone https://github.com/Naseeruddeen634/journeyman && cd journeyman
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python demo/generate.py
.venv/bin/python -m pytest -q                  # 18 passed

.venv/bin/python -m journeyman.cli build \
  "extract vendor, date and total from messy invoices and prove it is accurate"

.venv/bin/python -m journeyman.cli build \
  "pull supplier, date and amount total out of messy purchase orders and prove it works"

.venv/bin/python -m journeyman.cli memory      # what it has learned
```

Run the second command and watch the candidate count drop from 9 to 3.

## Why the numbers are trustworthy

The demo corpus in `demo/generate.py` is built **backwards**: start from
structured records, render them into messy documents, keep the records as the
answer key. Ground truth is exact by construction and nothing is graded by a
language model. The traps are real ones: six date formats including day-first and
pre-2000, European `1.234,56` amounts, a `Subtotal` line above the real `Total`
(where "total" is a substring of "subtotal"), invoices with no PO where the right
answer is nothing, and an account number that is the largest figure on the page.

## Built on

- **[Strands Agents SDK](https://strandsagents.com/)** — `GraphBuilder` with a
  cyclic topology and conditional edges, the `AgentBase` protocol for
  deterministic nodes, `reset_on_revisit`, execution bounds.
- **Architecture taxonomy** adapted from
  [30 Agents Every AI Engineer Must Build](https://github.com/PacktPublishing/30-Agents-Every-AI-Engineer-Must-Build)
  (Packt), which is where the pattern list and the production concerns come from.
- **Self-evolution framing** from
  [EvoAgentX](https://github.com/ANative-Lab/EvoAgentX): generate, execute,
  evaluate, optimise, repeat. EvoAgentX optimises against existing benchmarks
  such as HotPotQA and MBPP. Journeyman's difference is that it manufactures the
  measurement for a task that has none, and holds half of it back.

### Prior art, named on purpose

Prompt and workflow optimisation is a crowded field and this README is not going
to pretend otherwise. [DSPy](https://dspy.ai/) (MIPROv2, GEPA) does reflective
failure diagnosis and held-out validation.
[promptfoo](https://www.promptfoo.dev/), [DeepEval](https://deepeval.com/),
Braintrust and Langfuse all ship synthetic eval generation and CI gating. AWS
ships [`strands-agents-evals`](https://github.com/strands-agents/evals), whose
`ExperimentGenerator` creates test suites from a context description.

What Journeyman puts together that those do not: architecture selection with
explicit rejections, the held-out result as the reported number rather than the
training score, and a memory that makes the next similar job measurably cheaper.
If you already have an eval set and a metric, use DSPy. This is for the much more
common case where you have neither.

## What it does not do

It evolves a Python artifact against a deterministic grader. It does not yet
optimise free-text prompts graded by a model, it will not rescue a task with no
checkable notion of correct, and the pattern library is a fixed set of twelve
architectures rather than anything open-ended. The honest summary: it does one
kind of engineering job properly rather than every kind badly.

## Licence

MIT. Built by [Naseeruddeen Shahul Hameed](https://github.com/Naseeruddeen634).
