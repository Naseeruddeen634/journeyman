# Journeyman: 3-minute video

Everything on screen is real. The free commands run live; the parts that cost money or take ten
minutes are shown from the captured runs in `docs/submission/captured/`. Nothing here spends AWS
money. Speak the **VO** lines (about 400 words, three minutes at a calm pace).

## Before recording (off camera)

- Terminal font 18pt or larger, a dark or light theme with good contrast, window full screen.
- Close anything showing email, keys or your AWS account ID.

```bash
cd ~/code/helpdesk-ai
export PATH="$HOME/Downloads/untitled folder/journeyman/.venv/bin:$PATH"
export CAP="$HOME/Downloads/untitled folder/journeyman/docs/submission/captured"
git checkout -q main && clear
```

Optional, for the AWS shot (free to look at): in the AWS console, open **Amazon Bedrock AgentCore →
Runtimes → journeyman_reviewer** and take a screenshot. **Crop or blur the account ID and the ARN.**

---

## 0:00–0:20  The problem

**Screen:** title card: *Journeyman: an AI engineer that works beside you, and tells you the truth.*

**VO:** "If you ship LLM features, the bugs that page you are not the ones a linter finds. A model id
that gets deprecated. No output limit. JSON parsed straight from a model. A prompt nobody measures.
And when an AI agent fixes things for you overnight, the real question is whether what it left you is
true. Journeyman is an AI engineering agent, built on Strands Agents, that does the work and proves it."

## 0:20–0:50  What it sees (live)

```bash
journeyman inventory
```
```bash
journeyman review
```

**VO:** "This is helpdesk-ai, a small support-ticket app. Inventory shows every model call, which
model, and that the text goes to OpenAI. Review flags what a senior AI engineer would stop in a pull
request: a prompt with no eval, json.loads on model output, no timeout, a hardcoded model id, no
max_tokens. Each finding comes with why it matters and the fix. No model is involved, so it runs in
CI on every pull request, and only fails a PR on problems that PR introduced."

## 0:50–1:40  While you are away: the agent that would not lie (captured)

```bash
cat tests/test_context.py
```
```bash
sed -n 1,20p app/context.py
```

**VO:** "There's also a failing test. The docstring says the system message is always kept and the
oldest messages are dropped first. The test only checks that the newest message survives."

```bash
cat "$CAP/shift-local-qwen-withheld.txt"
```

**VO:** "Overnight, Journeyman picks this up and fixes it in an isolated git worktree, on a local open
model, sandboxed, with no network. It made the test pass. But Journeyman does not take the agent's
word. Two more Strands agents that never saw the code wrote tests from the docstring alone, and one
proved the fix now throws away the system prompt. So it was not committed. The report says exactly
why: made the tests pass, but broke behaviour the documentation describes."

## 1:40–2:10  The same task on Claude in Amazon Bedrock (captured)

```bash
cat "$CAP/shift-bedrock-claude-fixed.txt"
```

**VO:** "The same task on Claude in Amazon Bedrock. It was sent back once, then its fix passed the
repository's tests and all ten independent tests, in under a minute. That one is committed, on a
branch."

```bash
sed -n 1,20p "$CAP/propose-bedrock-fix.md"
```

**VO:** "In the morning, propose writes the pull request description from the evidence, including
what was not checked. It never pushes or merges. You read the diff, and you decide."

## 2:10–2:35  On AWS (captured)

**Screen:** the AgentCore console screenshot (account ID hidden), then:

```bash
sed -n 1,40p "$CAP/agentcore-explain-helpdesk-ai.txt"
```

**VO:** "For a team, the same reviewer runs on Amazon Bedrock AgentCore Runtime. You send it a
repository; it returns the same deterministic findings in about five seconds, and a Strands agent on
Claude in Bedrock reads the flagged code through a read-only tool and explains the fix. Nothing from
the repository is ever executed."

## 2:35–3:00  Proof

**Screen:** the "How good is it" table in the GitHub README.

**VO:** "We measured it against hidden oracles the agent never sees. On routine bugs, 35 of 36
correct and no wrong fix delivered. On hard bugs it is still wrong too often, and we publish that too.
The independent checker cut wrong deliveries from 10 to 4. Journeyman: it does the work, and it tells
you the truth about it."

---

## Checklist before you upload

- [ ] Under 3 minutes (trim the `cat` pauses if needed)
- [ ] `journeyman review` showed AIE008, AIE003, AIE005, AIE001, AIE002
- [ ] The words STUCK, "not committed" reasoning, and FIXED with "passed: 10 independent test(s)" are readable
- [ ] No account ID, ARN, email or key visible anywhere
- [ ] Benchmark numbers on screen match the README
