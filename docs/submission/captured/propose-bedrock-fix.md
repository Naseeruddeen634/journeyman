## What

failed: test_the_newest_message_survives_trimming - Ind...

Found at `tests/test_context.py` (failing test).

## Why this is believed to be right

- Test suite: 1 failing before, 0 failing after.
- Now passing: `tests/test_context.py::test_the_newest_message_survives_trimming`
- Newly failing: none
- Independent check passed: 10 independent test(s) (written from the task and the pre-change docstrings by agents that never saw this change).
- Claimed done 1 time(s) before it actually was; each time it was sent back with what was still wrong.
- Took 0.8 min, 11 model turns, on global.anthropic.claude-sonnet-4-6 (JOURNEYMAN_PREFER=bedrock, running in us-west-2).

## What was not checked

- Only the repository's own test suite, Journeyman's static review checks and the independent spec check were run.
- Behaviour those tests do not cover is unverified. A green suite is necessary, not sufficient.
- No integration, load, or manual testing was done.

## Changes

```
app/context.py | 9 ++++++++-
 1 file changed, 8 insertions(+), 1 deletion(-)
```

---
*Proposed by Journeyman, working unattended in an isolated worktree. It did not push this branch or open this pull request.*