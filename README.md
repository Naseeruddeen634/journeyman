# Journeyman

**An AI engineer that works beside you, keeps working when you leave, and tells you the truth about what it did.**

Built with the [Strands Agents SDK](https://strandsagents.com/). Runs on an open model on
your own machine by default. MIT licensed.

## The claim, and the proof

An AI agent will tell you it fixed something. Journeyman checks. Here is the same failing test
worked unattended twice, on the demo app in `examples/helpdesk-ai` (condensed from the full reports in
`docs/submission/captured/`):

```
  STUCK   test_the_newest_message_survives_trimming        brain: qwen3-coder:30b (local)
  tests     1 red before, 0 red after
  checked   An independent test written from the docstring and the task passed before the
            change and fails after it: test_system_message_always_kept
    Made the tests pass, but broke behaviour the documentation describes

  FIXED   test_the_newest_message_survives_trimming        brain: Claude Sonnet 4.6 on Amazon Bedrock
  tests     1 red before, 0 red after
  checked   passed: 10 independent test(s)
  took      0.8 min
```

Both fixes made the repository's own test pass. Only one was right, and Journeyman told them apart
with tests written by agents that never saw the code. The first was not committed; the second was
committed to a branch for you to review. Neither was pushed.

**Try the review in a minute, no model and no cloud account:**

```bash
git clone https://github.com/Naseeruddeen634/journeyman && cd journeyman
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/journeyman review --repo examples/helpdesk-ai
.venv/bin/journeyman inventory --repo examples/helpdesk-ai
```

Built for the hackathon: the first commit is 13 September 2026, and every step since is in the
git history, including the bugs listed under "What running it taught me".

A journeyman is a qualified tradesperson who works *beside* you rather than for you.
It does the job, leaves the work on a branch, and hands you the evidence, including the parts it
is unsure about.

---

## What it does for you

| when | command | what you get |
|---|---|---|
| you are working | `journeyman pair` | on every save, runs only the tests that depend on the file you changed. Silent unless something goes red |
| you join a team, or security asks | `journeyman inventory` | every model call in Python and TypeScript: which model, from which env var, bounded or not, and whether the text leaves the machine |
| before you merge | `journeyman review` | the problems a senior AI engineer would stop at in a PR, in Python and TypeScript, with the reason and the fix |
| on every pull request | `journeyman ci init` | a GitHub Actions workflow: findings inline on the diff, SARIF to code scanning |
| from your IDE agent | `journeyman mcp` | the same review and inspection tools, read-only, for Claude Code, Cursor or any MCP client |
| a prompt has no eval | `journeyman eval prompts/x.txt` | cases with a held-out split, recorded responses, and a CI test that fails if quality drops |
| you are away | `journeyman shift` / `watch` | takes the most important broken thing, fixes it in an isolated worktree, verifies it independently, leaves a branch |
| your team, from anywhere | AgentCore reviewer | the same review as a service on Amazon Bedrock AgentCore, explained by Claude on Bedrock |
| every few hours | `journeyman install` | the same, on a schedule, verified to actually run |
| the work is done | `journeyman propose` | a pull request description written from the evidence, and the two commands for **you** to run |
| you want to know if it is any good | `journeyman bench` | its fix rate against hidden oracles it never sees |
| something seems off | `journeyman doctor` | a check for every silent failure found while building it |

It never pushes, merges, or touches your checkout. That line is enforced in code, not
requested in a prompt, and every refusal is a test.

## How good is it

Measured, not claimed. `journeyman bench` gives the agent a bug in a throwaway repo and checks
its delivered fix against a hidden oracle it never sees. **Correct** means it delivered a fix and
the oracle passed. **Wrong** means it delivered a fix that made the visible tests pass but the
oracle failed: the failure that matters for an agent that works unattended. **Withheld** means it
refused to commit something it could not verify. All runs below: Qwen3-Coder 30B on a 24 GB
laptop, two feedback rounds, tests confined in the OS sandbox.

| suite | configuration | runs | correct | wrong, delivered | withheld |
|---|---|---|---|---|---|
| 12 routine bugs | default | 3 | **35 / 36** | 0 | 1 |
| 12 routine bugs | `--spec-check` | 2 | 21 / 24 | 1 | 2 |
| 5 hard bugs | default | 3 | 3 / 15 | **10** | 2 |
| 5 hard bugs | `--spec-check` | 3 | **6 / 15** | **4** | 5 |

What this says, plainly:

- On routine bugs it is reliable, and it did not deliver a wrong fix in three runs.
- On hard bugs, where the visible test covers one of two documented behaviours, it fixed the one
  the test checked and delivered, 10 times out of 15. That is the honest limit of a 30B local model
  with only the repository's own tests to go on.
- `--spec-check` adds a checker that never sees the worker's code: two fresh agents write tests
  from the task and the docstrings, and the change has to pass them. On hard bugs it halved the
  wrong deliveries (10 to 4) and doubled correct fixes (3 to 6). On routine bugs it did slightly
  worse: two cases ended unfixed (neither withheld change would have passed the oracle, so no good
  fix was blocked) and one wrong fix got through. It also makes a shift two to four times slower.
  It is off by default for those reasons.
- Five hard cases repeated three times are not independent samples. Read the direction, not the
  decimals. Every run is saved as JSON in `~/.journeyman/bench/`, per case, with the diff.


## Working beside you: `pair`

When you are in the middle of a problem you have the context and the agent does not,
so it should not try to solve your problem. It should do what you would otherwise
stop and do, and otherwise say nothing.

```
$ journeyman pair
  pairing on ~/code/billing  (learning the current state...)
  ready: 2 tests, 0 already red. Quiet unless that changes.
  09:04:03  RED after saving money.py: tests/test_invoice.py::test_total
      E   assert 132.0 == 123.0
  09:04:07  green again: tests/test_invoice.py::test_total
  09:04:10  in app/support.py, new: AIE004: user input goes straight into a prompt
```

That is a captured run, not an illustration. It also shows the silence working: the
first save broke `test_invoice.py` through `invoice.py`, and `test_money.py`, which
calls `vat(0)`, stayed green either way, so it was not mentioned.

Which tests a save affects comes from an import graph, followed transitively: saving
`money.py` runs `test_invoice.py` because `invoice.py` imports `money.py`. No model is
involved. `--notify` adds a macOS banner when something goes red.

**TypeScript and JavaScript** projects that use Vitest or Jest get the same loop, with the
graph built from `import`, `export ... from`, `require()` and `import()`, including the
`./money.js` specifier that TypeScript resolves to `money.ts`. Captured against a Vitest 3.2.7
project: fixing the VAT bug printed `green again: tests/invoice.test.ts::invoice adds vat`,
breaking it printed `RED after saving money.ts` with `AssertionError: expected 150 to be 123`,
and adding an OpenAI call to `invoice.ts` printed its AIE001, AIE002 and AIE003 findings.

## What models does this codebase use: `inventory`

The first week on an AI team, and every security review, starts with the same questions.
This is the summary half of Journeyman's own inventory, as printed (a per-call-site table follows it):

```
$ journeyman inventory
  Model inventory: journeyman
  3 call site(s) in 1 file(s)

  where the text goes
    AWS account (bedrock)                          1 site(s)  leaves this machine
      env JOURNEYMAN_BEDROCK_MODEL (default global.anthropic.claude-sonnet-4-6)
    openrouter.ai, unless JOURNEYMAN_HEAVY_BASE_URL is set   1 site(s)  leaves this machine
      env JOURNEYMAN_HEAVY_MODEL (default moonshotai/kimi-k3)
    local (ollama), unless OLLAMA_HOST is set      1 site(s)  stays local
      env JOURNEYMAN_LOCAL_MODEL (default qwen3-coder:30b)

  0 with the model id hardcoded at the call, 0 with no output limit, 0 whose destination depends on configuration
```

It is static (nothing runs), and where the code does not say, neither does the report: a
base URL read from configuration is "not visible in code" rather than assumed to be
api.openai.com, and a limit that could be inside `**config` is `limit ?` rather than
`NO LIMIT`. Twilio's `messages.create` is not mistaken for Anthropic's. `--format json`
for a governance record; also available to MCP clients.

## Knowing what AI engineering breaks: `review`

A linter finds unused imports. It does not know that a prompt with no eval is a
liability, or that `json.loads(response.content)` will page someone.

| | |
|---|---|
| **AIE001** | model id hardcoded mid-function instead of read from config |
| **AIE002** | no `max_tokens`: unbounded cost and unbounded p99 |
| **AIE003** | model output parsed with `json.loads` and no schema |
| **AIE004** | user text interpolated into a prompt. Graded: bare, delimited but never declared as data, or done properly |
| **AIE005** | an LLM call with no timeout or retry |
| **AIE006** | an exception swallowed in an LLM path, so quality drops silently |
| **AIE007** | a test that calls a live model |
| **AIE008** | a prompt with no eval |
| **AIE009** | model calls in a loop with no usage accounting |
| **AIE010** | Bedrock client with no adaptive retry. Throttling is the normal case |
| **AIE011** | `invoke_model` instead of `converse`, which locks the payload to one provider |
| **AIE012** | Bedrock client with no `region_name`. Model access is granted per region |
| **AIE013** | user-facing Bedrock call with no guardrail |
| **AIE014** | an Ollama model with no `num_ctx`: the default window is 4096 tokens and overflow silently discards the prompt |

AIE002 also checks where models are *constructed* (Strands `OllamaModel`, `BedrockModel`,
LangChain `ChatOpenAI`, and so on), because that is where frameworks set the output limit.
Journeyman's own brains were built without one, and the call-site version of the check
passed them.

**TypeScript and JavaScript** get the same codes (AIE001-004, 007, 014) for the OpenAI Node
SDK, the Vercel AI SDK and LangChain.js. There is no TypeScript parser in the Python standard
library, so a small lexer separates code, comments, strings, regex literals and template
`${expressions}`; a `${` inside an ordinary string or a comment is not a finding.

Every finding carries why it matters and what to do. The property that decides whether
anyone keeps a linter switched on is silence, so checks only fire in files doing LLM
work, a real f-string is told apart from `f"` inside another string by the tokenizer,
fixture directories go in `.journeymanignore`, and the tool is clean against its own
source, which a test keeps true.

## A prompt with no eval: `eval`

AIE008 is the finding that matters most, so there is a command that resolves it.

```bash
journeyman eval prompts/triage_prompt.txt --synthesize 12   # or edit the starter cases
journeyman eval prompts/triage_prompt.txt --record          # call the model once
pytest tests/test_triage_prompt_eval.py                     # replays, forever, for free
```

- Expectations are deterministic: `exact`, `one_of`, `json_keys`, `not_contains`,
  `regex`, `max_words`. No model grades a model.
- Cases are split into train and holdout once. The holdout score is the one that gates.
- Responses are recorded and replayed, so CI never calls a live model. Editing the prompt
  changes every key, so stale responses cannot grade a new prompt.
- Starter cases always include an injection attempt, because the case people forget is
  the one that pages someone.
- In a TypeScript project that uses Vitest the test is `tests/<prompt>.eval.test.ts`, replaying
  the same cassette with the same graders. A test runs both graders on the same inputs under a
  real Vitest so the two cannot drift apart unnoticed.

## While you are away: `scout`, `shift`, `watch`

`scout` finds work without being told: failing tests, AI review findings, unchecked
boxes in `BACKLOG.md`, `FIXME`s, untested public functions, ranked. No model decides
what is broken.

A shift takes the top item and:

1. opens a throwaway `git worktree` on a new branch
2. records which tests are red **before** the agent touches anything
3. gives the agent five tools, plus `check_my_fix` for review findings
4. when the agent says it is done, **checks for itself**, and if it is not done, says
   exactly what is still wrong and lets it continue (up to two rounds)
5. verifies again: nothing that passed now fails, the target test now passes or the
   target finding no longer fires, and what it *said* it changed matches the diff
6. commits to the branch only if all of that holds, and writes the report

The same bug, two brains, both captured in `docs/submission/captured/`. The demo repository is
`examples/helpdesk-ai`: `fit()` must trim a conversation to a word budget, and its docstring says the
system message is always kept and the oldest messages go first. The only test checks that the newest
message survives.

On the local model, the agent was sent back twice by its own verification. The version it finally
left passed the repository's test, and the independent checker showed it now dropped the system
message. It was not committed:

```
  STUCK   failed: test_the_newest_message_survives_trimming
  brain     qwen3-coder:30b
  tests     1 red before, 0 red after
  feedback  sent back 2 time(s) after claiming done
  checked   An independent test written from the docstring and the task passed before the
            change and fails after it: test_system_message_always_kept: AssertionError:
            assert 'user' == 'system'
  what it says:
    Made the tests pass, but broke behaviour the documentation describes
```

On Claude in Amazon Bedrock it was sent back once, and the change it then left passed every check:

```
  FIXED   failed: test_the_newest_message_survives_trimming
  brain     global.anthropic.claude-sonnet-4-6 (JOURNEYMAN_PREFER=bedrock)
  commit    468a708
  tests     1 red before, 0 red after
  took      0.8 min
  feedback  sent back 1 time(s) after claiming done
  checked   passed: 10 independent test(s)
  budget    11/40 turns, 4/120 commands, 11/40 paid calls
```

(Lines trimmed for width; nothing else changed. `--spec-check` was on for both.)


### It proposes, you dispose

| | |
|---|---|
| works in | a throwaway `git worktree` on a new branch |
| never touches | your checkout, your branch, your stash |
| never runs | `push`, `merge`, `reset`, `rebase`, `rm -rf`, `sudo`, `pip install`, a download piped into a shell |
| runs commands as | argument lists. No shell ever sees a string the model influenced |
| stops at | 45 min (enforced mid-generation by a watchdog), 20 model turns, 120 commands, 12 files, 6 paid-model calls |
| withholds a fix when | a test that passed now fails, the target still fails, what it said it changed is not in the diff, or the change deletes behaviour the docstring promises |
| stands down | after two shifts in a row change nothing |

### What contains the code it runs

A shift's job is to run your tests after the agent has edited your code, and a test file the
agent writes is code. A worktree restricts what the agent can ask for, not what runs. On macOS,
everything that executes the repository's code during a shift runs inside `sandbox-exec`:

| | |
|---|---|
| writes | only the worktree, the temp directory and `/dev/null`. Anywhere else: `Operation not permitted` |
| network | none |
| reads | denied for `~/.ssh`, `~/.aws`, `~/.gnupg`, keychains and similar, and for Journeyman's records of other repositories |
| environment | credential-looking variables removed, because a test that reads a key and writes it into the worktree would put it on a branch you might push |

This was probed with harmless canaries through the real tools, and the probe is a test:
agent-written test code cannot write outside its worktree, cannot see a secret environment
variable, and cannot reach the network. `journeyman doctor` re-runs a live probe, because
Apple has deprecated `sandbox-exec` and a sandbox that silently stopped enforcing would be
worse than none. Where no OS sandbox exists, the shift report says **UNCONFINED**.

Before this existed, the guardrails stopped accidents but were not a boundary, and this README
said otherwise. That is in the table of lessons below.

### Turning a branch into a pull request

```
$ journeyman propose
  journeyman/20260914-2028-failed--test-the-newest-message-survives -> main
  Description: .journeyman/proposals/journeyman-20260914-2028-...md

  Journeyman does not push or open pull requests.
  This repository has no remote, so there is nothing to push to. The branch is local: review it
  with  git diff main...journeyman/20260914-2028-...  and merge it yourself if you want it.
```

The description it wrote for the Bedrock fix above, opening section:

```markdown
## Why this is believed to be right

- Test suite: 1 failing before, 0 failing after.
- Now passing: `tests/test_context.py::test_the_newest_message_survives_trimming`
- Newly failing: none
- Independent check passed: 10 independent test(s) (written from the task and the pre-change
  docstrings by agents that never saw this change).
- Claimed done 1 time(s) before it actually was; each time it was sent back with what was still wrong.

## What was not checked

- Only the repository's own test suite, Journeyman's static review checks and the independent
  spec check were run.
- Behaviour those tests do not cover is unverified. A green suite is necessary, not sufficient.
```

With a remote, it prints the `git push` command for you to run instead, and `gh pr create` too if
`gh` is installed.

The description is built from the shift record: tests before and after, the review
verdict, how many times it claimed done before it was, the model and turns used, its
own concerns ahead of the diff, and a section on what was **not** checked.


## In CI and in your editor

```bash
journeyman ci init                      # writes .github/workflows/journeyman.yml
journeyman review --format github       # inline PR annotations
journeyman review --format sarif        # GitHub code scanning
journeyman review --fail-on 60          # exit code policy
journeyman review --new-since origin/main   # only what this branch introduced
```

On a pull request the workflow fails only on findings the PR introduced. Otherwise
installing a reviewer on an existing codebase turns every PR red on the first day, and the
check gets switched off. The merge-base is reviewed in a temporary worktree and findings are
matched by code, file and title, so moved code is not new and renames are followed. Comparing
whole reviews, rather than filtering to changed lines, also catches a PR that deletes the test
behind a prompt: the untouched prompt file is what goes red.

The generated workflow refuses to write `pip install journeyman`: a bare name installs
whatever owns it on PyPI, which is not this project. It uses an explicit git URL, which
fails loudly if wrong.

```bash
claude mcp add journeyman -- journeyman mcp
```

The MCP server exposes `review`, `scout`, `inventory`, `affected_tests`,
`run_affected_tests`, `eval_status` and `shift_history`, all annotated read-only. Editing stays with `shift`,
which runs sandboxed and verifies itself; giving a remote caller a way to start edits would
be a second path around all of that. Tested through a real stdio client.

## When something seems off: `doctor`

Every check exists because the failure it looks for happened and was silent:

```
  [ok  ] local model                      qwen3-coder:30b ready, requested context 16384
  [ -- ] bedrock                          not configured (optional)
  [ -- ] kimi k3                          OPENROUTER_API_KEY not set (optional)
  [ok  ] git                              /usr/bin/git
  [ok  ] pytest                           available to .../bin/python
  [ -- ] gh                               not installed (optional; propose will print browser steps)
  [ -- ] scheduler                        not installed
  [ok  ] repo journeyman: tests           suite is green
  [WARN] repo journeyman: schedulable     inside a folder macOS keeps from background agents
                                          -> Fine for shift, pair and review. For install,
                                             move it to e.g. ~/code or grant Full Disk Access.
```

That is the development machine, captured.

## On a schedule: `install`

```bash
journeyman install --self-contained --repo ~/code/billing --every 180
journeyman status
```

**macOS does not let background agents read `~/Downloads`, `~/Documents` or
`~/Desktop`.** This was proven on the development machine with a probe job, and it is
why the first scheduled install ran zero times. Keep the repositories you want worked on
somewhere like `~/code`, or grant Full Disk Access to the agent's Python.

`--self-contained` installs a copy under `~/.journeyman/app`, where launchd can reach it.
Before writing anything, `install` loads a throwaway job with the same program, arguments
and environment in `--dry-run` mode and waits for it to exit cleanly. If launchd cannot
actually start it, nothing is installed and you get the reason. `status` reports how many
times it has really run and its last exit code, not just whether it is registered.

## On AWS: the reviewer on Amazon Bedrock AgentCore

The same review runs as a service on **Amazon Bedrock AgentCore Runtime**, so a team can call it from
CI or a chat bot with no local model and nothing installed. Each invocation runs in its own isolated
session. The findings are deterministic (the same code as `journeyman review`); a Strands agent on
**Claude in Amazon Bedrock** only explains them, reading the flagged lines through a read-only tool
confined to the repository. Nothing from the repository is executed.

```bash
python scripts/agentcore_review.py ~/code/helpdesk-ai --arn <runtime ARN> --mode explain
```

Captured from the deployed runtime (us-east-1), unedited apart from truncation:

```
  helpdesk-ai: 6 finding(s), reviewed on AgentCore

  [85] AIE008  prompts/triage_prompt.txt is a prompt with no eval
  [70] AIE003  model output parsed with json.loads and no schema
        app/triage.py:21
  ...
  ### 1. `json.loads` with no fence-stripping will crash in production — `app/triage.py:21`

  The prompt says "Reply with JSON only" but models routinely wrap their output in a markdown
  code fence anyway ...
```

Review mode (findings only) answered in under 5 seconds, explain mode in about 20. The full
response is in `docs/submission/captured/`. It also shows why the explanation is kept apart from the
findings: the model wrote that the OpenAI client has "no timeout at all", when the SDK's default is
ten minutes. The finding (no timeout set in this file) is right; the prose around it is a model's.

A private or unpushed repository is sent as an archive of its committed HEAD, so the service never
needs access to where the code lives. Only `https://github.com/owner/name` URLs are fetched otherwise.

Deploying it (starter toolkit, direct code deploy, no Docker):

```bash
agentcore configure -e agentcore_app.py -n journeyman_reviewer -r us-east-1 \
  -rf requirements-agentcore.txt --deployment-type direct_code_deploy --runtime PYTHON_3_13
agentcore deploy
```

## The brains

| | model | where | when |
|---|---|---|---|
| **local** | Qwen3-Coder 30B-A3B (~18 GB) | your machine, via Ollama | by default. Free, private, works offline |
| **bedrock** | Claude on Amazon Bedrock | your AWS account | `JOURNEYMAN_PREFER=bedrock`, or hard tasks when configured |
| **heavy** | [Kimi K3](https://github.com/MoonshotAI/Kimi-K3) | an OpenAI-compatible endpoint | hard tasks, when `OPENROUTER_API_KEY` is set |

```bash
ollama pull qwen3-coder:30b
journeyman brain
```

Kimi K3 is 2.8T parameters; at MXFP4 that is on the order of a terabyte of weights, so it
is used through an API rather than locally. Anything other than the local model is metered
against the paid-call budget.

## What running it taught me

Every one of these passed its tests. Each was found by running the thing for real, and
each is now a test.

| what was wrong | how it was found |
|---|---|
| The gate demanded a green suite, so every fix in a repo that arrived red was reported STUCK | a real shift in a repo whose tests needed a credential |
| The agent claimed changes it had not made | reading the diff against its summary. Now `claims_vs_diff` |
| Review tasks were marked FIXED when the finding still fired | a shift that wrapped input in backticks |
| AIE004 flagged correctly delimited input too, so the right fix could never pass | watching the agent fail to satisfy it |
| The turn, time and paid-call budgets in this README were declared and never enforced | reading the code against the README |
| Every scanner judged skip directories by absolute path, so every sandbox (under `.journeyman/`) and every repo under a folder called `build` looked empty, and `check_my_fix` told the agent every finding was gone | the benchmark: review cases marked stuck whose hidden oracles passed |
| Python trusts cached bytecode by size and whole-second mtime, so a same-length edit tested within a second ran the old code | a pair test that passed standalone and failed under pytest |
| The installer split the program path on spaces; the scheduled agent was registered and ran zero times | checking launchd's own run count the next morning |
| The verifier read stale stderr from a previous attempt and misdiagnosed a healthy job | a result that could not be true |
| A test unloaded the real launchd agent | re-running the audit after the suite passed |
| A benchmark oracle demanded equal bill splits the docstring never promised | writing reference solutions for every oracle |
| The local model ran with Ollama's default 4096-token window; eight context shifts kept 4 tokens and discarded 2,045, which is the system prompt and the task | `ollama ps`, then Ollama's own server log |
| Journeyman's own model constructors had no output limit, and its own AIE002 could not see constructors | fixing the context bug |
| The budget hook could not stop a single generation that runs away | reading where the hook fires; now a watchdog calls `Agent.cancel()` |
| `propose` printed `gh` commands on a machine without `gh`, and a push for a repo with no remote | running it on the development machine |
| A commit left the CLI unable to start, because `pytest \| tail` reports tail's exit status | the next test run; now every subcommand has a smoke test |
| **The agent could never run tests.** Its `run_tests` tool built a shell string from a Python path containing a space, so every call on the development machine was refused. Every shift and benchmark run before the fix worked blind | probing the guardrails with canary files |
| The allowlist let through `python -c`, `$(...)`, backticks and redirects, and test code the agent wrote ran with full user permissions | the same probe |
| Twice a shift deleted documented behaviour (the ellipsis in `truncate`) to make a test pass, said so, and once it was delivered | the benchmark's hidden oracle |
| AIE003's advice recommended a fix that still fails on fenced JSON, and a shift that followed it exactly was delivered | the benchmark's hidden oracle |
| An early reading of the benchmark credited feedback rounds with two fixes that happened with zero feedback rounds | reading the per-case data instead of the totals |

The argument for the whole design in one line: **an agent will tell you it did something,
and the only defence is checking.**

## `build`: the evolution engine

The original job type. Given a goal, a nine-node Strands graph picks an architecture from
twelve patterns **and names the ones it rejected and why**, builds an eval set with a
held-out half, measures, and evolves the artifact in a loop that is a real cycle in the
graph (`select` has a conditional edge back to `evolve`). It ships what survives on data
the search never saw, not the best training score, and remembers which repairs worked,
so a related job measured 3 candidates instead of 9 for the same held-out result.

```bash
journeyman build "extract vendor, date and total from messy invoices and prove it is accurate"
```

It currently evolves a Python extractor against a generated invoice corpus with exact
ground truth; it is the least general part of the tool.

## Run it

```bash
git clone https://github.com/Naseeruddeen634/journeyman && cd journeyman
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
ollama pull qwen3-coder:30b
.venv/bin/python demo/generate.py
.venv/bin/python -m pytest -q
```

## Prior art, named on purpose

[DSPy](https://dspy.ai/) (MIPROv2, GEPA) does reflective optimisation with held-out
validation. [promptfoo](https://www.promptfoo.dev/), [DeepEval](https://deepeval.com/),
Braintrust and Langfuse ship synthetic eval generation and CI gating. AWS ships
[`strands-agents-evals`](https://github.com/strands-agents/evals). Coding agents that fix
issues on a branch are common.

What this puts together: a reviewer that knows LLM-specific failure modes, an eval harness
that records and replays, an unattended worker whose verdicts are checked independently
rather than taken from the agent, and a benchmark that separates a green test from a
correct fix. If you have an eval set and a metric, use DSPy.

## What it does not do

- The review reads Python and TypeScript/JavaScript; shifts fix Python repositories. Other
  languages are not covered.
- The OS sandbox for the code a shift runs is macOS only; elsewhere the report says UNCONFINED.
- It fixes what a failing test or a static check pins down precisely. It will not design
  your service, and `STUCK` is a normal, correct outcome.
- A green suite plus a resolved finding is necessary, not sufficient. That is what
  `READ THE DIFF BEFORE YOU MERGE` and the benchmark's oracles are for.
- The scheduler is macOS launchd only.

## Licence

MIT. Built by [Naseeruddeen Shahul Hameed](https://github.com/Naseeruddeen634).
