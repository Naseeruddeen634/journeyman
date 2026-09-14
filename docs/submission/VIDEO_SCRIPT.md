# Journeyman: video run of show

Six short clips, recorded one at a time with your voice, then joined. Target: under 3 minutes.
Live AWS calls happen in clips 4 and 5 (about $0.20 per take). Everything else is free to retake.

## Setup, once (off camera)

Open **Terminal** and run:

```bash
conda deactivate 2>/dev/null; PROMPT='$ '; clear
cd ~/code/helpdesk-ai && git checkout -q main
export PATH="$HOME/Downloads/untitled folder/journeyman/.venv/bin:$PATH"
export ARN="$(awk '/agent_arn:/{print $2}' ~/.journeyman/agentcore-build/.bedrock_agentcore.yaml)"
export CAP="$HOME/Downloads/untitled folder/journeyman/docs/submission/captured"
aws sts get-caller-identity > /dev/null && echo AWS-OK
clear
```

If it does not print `AWS-OK`, run `aws login` and repeat. Then press **Cmd +** four or five times so
the text is big. Make the Terminal window fill the screen.

In the browser, open https://github.com/Naseeruddeen634/journeyman (after `git push`).

---

## Clip 1: the problem (browser, about 20 s)

**Show:** the GitHub page, scroll slowly to "The claim, and the proof".

**Say:** "If you ship LLM features, the bugs that page you are not the ones a linter finds. And when an
AI agent fixes things for you overnight, the real question is whether what it left you is true.
Journeyman is an AI engineering agent built on Strands Agents. It does the work, and it proves it."

## Clip 2: what it sees (terminal, about 30 s)

**Type:** `journeyman inventory` then `journeyman review`

**Say:** "This is a small support-ticket app. Inventory finds every model call and where the text
goes. Review flags what a senior AI engineer would stop in a pull request: a prompt with no eval,
JSON parsed straight from the model, no timeout, a hardcoded model id, no output limit. No model is
involved, so it runs in CI and only fails a pull request on problems that pull request added."

## Clip 3: the bug, and the fix that was refused (terminal, about 35 s)

**Type:** `sed -n 7,15p app/context.py` then `cat tests/test_context.py`

**Say:** "There's a failing test. The docstring says the system message is always kept and the oldest
messages go first. The test only checks that the newest message survives."

**Type:** `cat "$CAP/shift-local-qwen-withheld.txt"`

**Say:** "Overnight, on a local open model, Journeyman fixed it in an isolated worktree and the test
went green. But it doesn't take the agent's word. Two more agents that never saw the code wrote tests
from the docstring, and proved the fix threw away the system prompt. So it was not committed."

## Clip 4: live on Claude in Amazon Bedrock (terminal, about 60 s, costs about $0.15)

**Type:** `clear` then `JOURNEYMAN_PREFER=bedrock journeyman shift --spec-check --max-paid-calls 40`

**Say while it runs:** "Same task, live, on Claude in Amazon Bedrock. It works in a throwaway git
worktree, the tests run sandboxed with no network, and when it says done, Journeyman runs everything
itself."

**When the report appears, say:** "Fixed. The repository's test passes, and so do all ten independent
tests written without seeing the code. It's on a branch. It never pushes."

**Type:** `journeyman propose`

**Say:** "Propose writes the pull request description from the evidence, including what was not
checked. You read the diff, you decide."

## Clip 5: the reviewer on AgentCore (terminal, about 35 s, costs about $0.04)

**Type:** `clear` then
`python "$HOME/Downloads/untitled folder/journeyman/scripts/agentcore_review.py" . --arn "$ARN" --mode explain --region us-east-1 | head -45`

**Say while it runs:** "For a team, the same reviewer is deployed on Amazon Bedrock AgentCore Runtime.
The findings are deterministic; a Strands agent on Claude reads the flagged code through a read-only
tool and explains the fix. Nothing from the repository is ever executed."

## Clip 6: the proof (browser, about 25 s)

**Show:** the GitHub README, scroll to "How good is it" and its table.

**Say:** "We measured it against hidden checks the agent never sees. On routine bugs, 35 of 36 correct,
no wrong fix delivered. On hard bugs it's still wrong too often, and we publish that too. The
independent checker cut wrong deliveries from 10 to 4. Journeyman does the work, and tells you the
truth about it."

---

## Before you upload

- [ ] No account ID, ARN, email or key visible (the ARN is only in `$ARN`, set off camera)
- [ ] The words STUCK, "passed: 10 independent test(s)" and FIXED are readable
- [ ] Under 3 minutes after trimming the waiting in clip 4
