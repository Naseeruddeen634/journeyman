# #Agents for Humans: I built an AI engineer that refuses to lie about its work

The hard part of an agent that works while you are away is not getting it to write code. It is
knowing, the next morning, whether what it left you is true.

I built Journeyman with the Strands Agents SDK: an AI engineering agent that sits beside you while
you work and keeps working when you leave. It reviews LLM code the way a senior AI engineer does,
builds evals for prompts nobody measures, and on a schedule takes the most important broken thing
in a repository, fixes it in an isolated git worktree, and leaves a branch. This post is about the
part that took most of the work: making it honest.

## The first version lied, politely

Early runs looked great. The agent said `DONE` a lot. Then I started reading the diffs.

- It reported "added delimiters around the input and updated the system prompt to separate
  instructions from data." The diff changed one line and never touched a system prompt.
- Twice it made a failing length assertion pass by deleting the `"..."` from a truncation
  function whose docstring promised the ellipsis. The tests went green. The second time the fix
  was delivered.
- Its budget of 45 minutes and 20 turns was declared in a dataclass and enforced nowhere.

None of this is malice. A model optimises for the signal it is given, and "the tests pass" is a
weak signal. So the fix was never a better prompt. It was code around the model that checks.

## What checks, in code

**Budgets are a Strands hook.** A `HookProvider` counts every model call and tool call and cancels
the invocation when a limit is reached. A hook runs between model calls, so it cannot stop one
generation that runs away; `Agent.cancel()` is thread-safe, so a timer calls it from outside.

**The agent cannot touch your checkout.** It works in a git worktree on a new branch, through
tools that validate every path. It cannot push, merge, or install. Those refusals are tests.

**The code it causes to run is confined.** A shift's job is to run tests after the agent has edited
them, so a test file it wrote runs with your permissions. On macOS those runs go through
`sandbox-exec`: no network, no writes outside the worktree and temp, no reads of `~/.ssh`, `~/.aws`
or keychains, and secret-looking environment variables removed.

**Its own claims are not evidence.** After it says `DONE`, Journeyman runs the suite itself and
compares failing tests before and after. A change that breaks a passing test is "regressed", not
committed. A review finding counts as fixed only when the check no longer fires on that file.

**A docstring is a contract.** If a function's docstring quotes a literal and the change removes
that literal from the body, the change is withheld. That one check stopped the ellipsis deletion.

**It says when it is unsure.** Hedges in its summary ("however", "this may not match the
docstring") go to the top of the morning report, not the bottom of a log.

## Measuring it instead of believing it

I wrote bug cases with hidden oracles: small repositories with a failing test, and a separate check
of the correct behaviour the agent never sees. A delivered fix that passes the visible test but fails
the oracle is the failure that matters. On a 30B local model (Qwen3-Coder) on a laptop:

- 12 routine bugs, three runs: 35 of 36 correct, and no wrong fix delivered.
- 5 hard bugs, three runs: 3 of 15 correct, and 10 wrong fixes delivered.

The hard cases all had the same shape: the docstring and the task named two behaviours, the visible
test checked one, the agent fixed that one, and the suite went green. That result is in the README,
because an agent that hides its failure rate is exactly the thing I was trying not to build.

## Silent failures are the enemy

The most useful habit was treating every "it worked" as suspicious. Some of what that found:

- Ollama's default context window is 4096 tokens, and overflow is silent. My shifts were quietly
  discarding the task description. Journeyman's reviewer now flags it in anyone's code.
- A test suite that cannot start (a `conftest.py` that no longer imports) printed no failing tests,
  so it read as green. A change that broke the whole suite would have "fixed" every failing test.
- Shifts run in a git worktree, which never contains the untracked `.venv`, so tests ran under the
  wrong Python and a green repository looked red.
- The scheduled agent was registered with launchd and ran zero times, because macOS blocks
  background agents from `~/Downloads`.

Each became a check in `journeyman doctor`, a test, or both.

## What I would tell someone building an unattended agent

1. Put the rules in code the model cannot talk its way past, and test the refusals.
2. Verify the outcome yourself. The agent's summary is a claim.
3. Measure against something the agent cannot see, and publish the bad numbers too.
4. Prefer "I did not commit this, here is why" over a confident wrong answer. For work nobody is
   watching, withholding is a feature.

Journeyman is MIT licensed and built on Strands Agents, with local models through Ollama, Kimi K3
through an OpenAI-compatible API, and Claude on Amazon Bedrock.
