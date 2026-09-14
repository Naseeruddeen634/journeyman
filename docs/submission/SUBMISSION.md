# Journeyman: submission text

## Tagline
An AI engineering agent that works beside you, keeps working when you leave, and tells you the truth
about what it did.

## The problem
Teams shipping LLM features get paged by bugs a linter cannot see: a model id that gets deprecated
mid-function, no output limit, JSON parsed straight from a model, user text spliced into a prompt, a
prompt nobody has ever measured. Meanwhile small fixes pile up. Coding agents can write the fixes, but
an agent working unattended has a worse problem than capability: you cannot tell, the next morning,
whether what it left you is true.

## Who it is for
AI engineers and the teams around them: the person who owns the LLM features in a product, reviews
the PRs that touch prompts and model calls, and never has time for the backlog.

## What it does
- **`review`**: the problems a senior AI engineer would stop in a PR, in Python and TypeScript, each
  with why it matters and the fix. Deterministic, no model. In CI it fails a pull request only on
  findings that pull request introduced.
- **`inventory`**: every model call, which model or env var, whether output is bounded, and whether
  the text leaves the machine.
- **`eval`**: turns a prompt with no eval into cases with a held-out split, recorded responses, and a
  pytest or Vitest test that fails when quality drops.
- **`pair`**: while you work, runs only the tests your save affects, and stays silent unless something
  goes red.
- **`shift` / `install`**: while you are away, takes the most important broken thing, fixes it in an
  isolated git worktree on a new branch, verifies the work itself, and writes a morning report.
  `propose` turns it into a pull request description. It never pushes or merges.
- **On AWS**: the reviewer runs on Amazon Bedrock AgentCore Runtime, with a Strands agent on Claude in
  Bedrock explaining the findings, so a team can call it from CI or chat without local setup.

## How we built it
Strands Agents throughout:
- The shift worker is a Strands `Agent` with `@tool` functions bound to a sandboxed worktree.
- Budgets (turns, tool calls, paid calls) are a Strands `HookProvider`; a timer calls `Agent.cancel()`
  to enforce the wall clock during a single generation.
- The independent spec check is two more Strands agents that never see the worker's code: they write
  tests from the task and the pre-change docstrings, and the change must pass them.
- Three brains through Strands model providers: Qwen3-Coder on Ollama locally (every benchmark run), Kimi K3 through an
  OpenAI-compatible API, and Claude on Amazon Bedrock. The `build` engine uses `GraphBuilder` with
  bounded cycles.
- Tests the agent causes to run execute under the macOS sandbox with no network and no access to
  credential stores.

## Challenges
Honesty was harder than capability. Early shifts reported changes they had not made, deleted
documented behaviour to satisfy a test, and ran with budgets that were declared but never enforced.
Each became a verification step in code: independent test runs before and after, a docstring contract
gate, review-finding resolution checks, and the blind spec checker. We also found silent failures in
our own stack: Ollama's 4096-token default context silently discarding the task, a test suite that
could not start reading as green, and sandboxed tests running under the wrong Python.

## Accomplishments
Measured against hidden oracles the agent never sees, on a local 30B model:
- 12 routine bugs, three runs: 35 of 36 correct, no wrong fix delivered.
- 5 hard bugs, three runs: without the spec check, 3 of 15 correct and 10 wrong fixes delivered; with
  it, 6 of 15 correct and 4 wrong. We publish the bad numbers too.

## What we learned
Put the rules in code the model cannot talk its way past. Verify outcomes yourself; the agent's
summary is a claim. And when the tests that verify an agent's work were written by the same context
that wrote the code, nothing has been verified.

## What's next
A Linux sandbox so shifts can run on a team server, running the reviewer against a larger set of
real open-source LLM applications, and using the benchmark to decide when the spec check should be on
by default.

## Built with
Strands Agents, Amazon Bedrock, Amazon Bedrock AgentCore, Claude, Qwen3-Coder, Ollama, Kimi K3,
Python, pytest, Vitest, MCP, GitHub Actions, SARIF.
