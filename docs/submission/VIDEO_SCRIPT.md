# Journeyman: 3-minute video

Record the terminal at a large font (Terminal > Settings > 18pt+). Every command below is real;
run them live or record each clip separately. Speak the **VO** lines; they are about 380 words,
which fits three minutes at a calm pace.

Setup before recording (not on camera):

```bash
cd ~/code/helpdesk-ai
export PATH="$HOME/Downloads/untitled folder/journeyman/.venv/bin:$PATH"
git checkout -q main
```

---

## 0:00–0:20 Problem and audience

**Screen:** title card, "Journeyman: an AI engineer that works beside you, and tells you the truth."

**VO:** "If you ship LLM features, the bugs that page you are not the ones a linter finds. A model id
that gets deprecated. No output limit. JSON parsed straight from a model. A prompt nobody measures.
And the fixes pile up faster than you can get to them. Journeyman is an AI engineering agent, built
on Strands Agents, that works beside you and keeps working when you leave."

## 0:20–0:50 What it sees

**Screen:**
```bash
journeyman inventory
journeyman review
```

**VO:** "This is helpdesk-ai, a realistic support-ticket app. Inventory shows every model call, which
model, and where the text goes. Review flags what a senior AI engineer would stop in a PR: a
hardcoded model id, no max_tokens, json.loads on model output, no timeout, and a prompt with no eval.
Each finding says why it matters and how to fix it. None of this uses a model, so it runs in CI on
every pull request, and only fails a PR on what that PR introduced."

## 0:50–1:50 While you are away

**Screen:**
```bash
journeyman shift --spec-check
```
(Show the run starting, cut to the finished report.)

**VO:** "Overnight, it picks the most important broken thing, here a failing test in context trimming,
and fixes it in an isolated git worktree. It cannot push, and the tests it runs are sandboxed with no
network. When it says done, Journeyman does not believe it. It runs the suite itself. Then two fresh
Strands agents that never see the code write tests from the docstring alone. In this run they caught
that the fix silently dropped the system prompt, which the visible test never checked." *(Adjust this
sentence to what the recorded run actually shows: fixed, withheld, or stuck, with the reason on screen.)*
"If it cannot verify the work, it does not commit it, and it tells you why."

## 1:50–2:15 Handing it back

**Screen:**
```bash
journeyman propose
```

**VO:** "In the morning, propose writes the pull request description from the evidence: what changed,
why it is believed right, and what was not checked. You read the diff and you open the PR. It never
does that for you."

## 2:15–2:40 On AWS

**Screen:** `agentcore invoke '{"repo": "https://github.com/<you>/helpdesk-ai", "mode": "explain"}'`
and the JSON response. *(Only if the deployment is live. Otherwise show `JOURNEYMAN_PREFER=bedrock
journeyman shift` running on Claude in Bedrock.)*

**VO:** "The same reviewer runs on Amazon Bedrock AgentCore, so a team can call it from CI or chat with
no local setup. Deterministic findings, explained by a Strands agent on Claude in Bedrock, reading the
code through a read-only tool. Locally it runs on an open model; on AWS, on Bedrock."

## 2:40–3:00 Proof and close

**Screen:** the benchmark table from the README.

**VO:** "We measured it against hidden oracles it never sees. On routine bugs, 35 of 36 correct and no
wrong fix delivered. On hard bugs it is still wrong too often, and we publish that. The independent
checker cut wrong deliveries from 10 to 4. Journeyman: it does the work, and it tells you the truth
about it."

---

## Recording checklist

- [ ] Font large, window uncluttered, no API keys or account IDs visible on screen
- [ ] `journeyman review` output shows at least AIE001, AIE002, AIE003, AIE008
- [ ] The shift clip shows the final report box (outcome, tests before and after, checked line)
- [ ] The benchmark numbers on screen match the README exactly
- [ ] Under 3 minutes
