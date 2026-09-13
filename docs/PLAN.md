# JOURNEYMAN — Plan v1 (for council review)

**Hackathon:** Agents for Humans (AWS / Strands Agents). Deadline Sept 14, 5pm PT.
**Track:** Professional Agents.
**Second submission.** Entry one is Causa (Good Neighbor, causal decision agent).
Rules allow multiple entries if substantially different. This is a different
audience, a different problem, and a different graph topology (a loop, not a branch).

## The one-liner

Journeyman is an AI engineer that writes the eval you never wrote, evolves your
prompt against it, and refuses to hand back anything worse than what you had.

## The problem (specific, and every AI engineer has it)

You wrote a prompt. It "works". You tweak it. Does it still work? You do not know,
because you never built an eval set. Building one is more work than building the
thing, so nobody does. So AI engineers ship on vibes, tweak on vibes, and
regressions land silently.

Self-evolving agent frameworks exist. EvoAgentX optimises against HotPotQA, MBPP
and MATH, and reports real gains. But you do not have HotPotQA. You have a
structured-extraction prompt for your company's messy invoices and no ground truth
at all. **The bottleneck is not the optimiser, it is the missing measurement
function.** That is the gap Journeyman fills.

## Who it is for

AI engineers, ML engineers and anyone shipping LLM features: the person who already
writes prompts, chains, RAG pipelines and agents, and currently has no way to tell
whether today's change helped. It sits beside them in the repo, not in a chat box.

## What it does, end to end

Point it at a prompt file or a Python callable, describe the task in one sentence:

```bash
ratchet evolve prompts/extract.txt --task "pull vendor, date, total from invoices"
```

1. **Read** the artifact and infer its contract: inputs, expected output shape
2. **Manufacture the eval set** it never had, including adversarial and edge cases,
   split into train and a **held-out** set that the optimiser never sees
3. **Baseline** the artifact and report the score honestly
4. **Diagnose** the failure modes, clustered (not "62%", but "fails on multi-line
   addresses and any date before 2000")
5. **Evolve**: generate mutations targeted at those specific failure clusters,
   measure each, keep what wins. This is a real cycle in the agent graph.
6. **Validate on held-out** data. An improvement that does not survive held-out is
   reported as overfitting, not as a win.
7. **Ship**: write the improved artifact, the eval set, and a regression gate that
   fails CI if a future edit scores worse.

## Self-evolving, at two levels

- **Level 1** evolves the user's artifact. This is the visible one.
- **Level 2** evolves Journeyman. It records which mutation strategies fixed which
  failure classes, and prefers those next time. That memory persists across runs,
  so it gets better at being an AI engineer the more you use it.

Level 2 is what separates this from a prompt optimiser.

## Why the number is trustworthy

Same move that worked on entry one. The demo corpus is **generated from known
structured records**, so ground truth is exact by construction rather than judged
by another LLM. Grading is deterministic field-F1. Nobody has to trust an LLM judge
for the headline number.

Also a negative control: run Journeyman on an already-good prompt and it must report
"no reliable improvement" rather than inventing one.

## Architecture (Strands, and deliberately unlike entry one)

Entry one's graph is a **branch**. This one is a **cycle**: `GraphBuilder` with a
loop back from Select to Mutate, bounded by `set_max_node_executions` and
`reset_on_revisit`. The evolution loop is the graph, not a for-loop inside a node.

Nodes: Reader, EvalSmith, Baseline, Diagnostician, Mutator, Arena, Selector
(loops back to Mutator while budget remains and improvement continues),
Validator (held-out), Shipper.

Strands surface: cyclic `GraphBuilder`, conditional edges on the loop exit,
`@tool` for every measurement, `structured_output_model` for typed handoffs,
hooks for the run ledger, `FileSessionManager` for resumable evolution runs,
`Agent.as_tool` for the mutator strategies, BedrockModel + AgentCore.

## Deliverables

- `ratchet/` package, tested
- CLI: `ratchet evolve`, `ratchet gate`, `ratchet doctor`
- Web UI showing the score climbing generation by generation
- Generated demo corpus with exact ground truth
- README + architecture diagram + MIT LICENSE
- Video script

## Risks

- **An LLM is genuinely required here** (to mutate and to judge free-text), unlike
  entry one. Mitigation: the demo task grades deterministically, and offline mode
  ships a rule-based mutation library so the pipeline runs and the score still
  moves without a model provider.
- **Time.** Under a day. The evolution loop and the held-out validation are the
  product; the web UI is next; AgentCore is last.
- **Overfitting the eval set** is the obvious criticism. The held-out split is the
  answer and it must be visible in the demo.
