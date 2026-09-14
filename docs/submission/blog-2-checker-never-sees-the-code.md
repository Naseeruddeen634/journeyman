# #Agents for Humans: a checker that never sees the code

My AI engineering agent, Journeyman, was reliable on routine bugs and quietly wrong on hard ones.
On five hard cases, repeated three times, it delivered a wrong fix 10 times out of 15. This is how I
used Strands Agents to build a second agent that catches that, what it took to stop it crying wolf,
and what it did and did not fix.

## The failure

Every wrong delivery had one shape. Take a cosine similarity function:

```python
def cosine(a, b):
    """Cosine similarity in [-1, 1]. A zero vector has similarity 0.0 with anything."""
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) + math.sqrt(sum(y * y for y in b)))
```

The task: "cosine similarity uses the wrong normaliser and crashes on zero vectors." The visible test
checked identical vectors. The agent changed `+` to `*`, the test passed, and it said DONE. The
zero-vector case now divided by zero. The docstring said so. The task title said so. Nothing that
ran looked.

A green suite only says that the tests which exist pass.

## Maker and checker

A human team handles this with review by someone who did not write the code. So after the worker
says DONE and every existing check passes, Journeyman asks two more Strands agents to write tests:

- each is a fresh `Agent` with no tools and no view of the worker's conversation or of the new code
- each gets only the task, and each changed function's signature, docstring and module constants
  as they were **before** the change
- their tests run in the OS sandbox against the change, and again against the original code

Running against the original separates two very different findings. A test that passed before and
fails now means the change broke documented behaviour. A test that failed before and still fails
means the job is not finished, or the checker is wrong. Failures go back to the worker, who is told
the tests may be wrong and that it may push back instead of bending correct code to fit them.

## Making it stop crying wolf

The first version caught all four wrong fixes I tested it on, and raised a false alarm on four of
five correct reference solutions. A checker that is wrong that often teaches the worker to break
good code. I tuned it offline with the real local model, against the correct solutions (it should
be silent) and against the wrong fixes the agent had actually delivered (it should speak):

1. **Float equality.** It was told to use `pytest.approx` and still wrote `cosine(a, b) == -1.0`.
   Journeyman now rewrites equality against float literals mechanically. That is always sound.
2. **Missing vocabulary.** For a retry wrapper documented to retry "transient errors", it guessed
   that meant any `Exception`. The module defined `TRANSIENT = (TimeoutError, ConnectionError)`, but
   the buggy function never referenced it. Module constants are now part of the brief.
3. **Failures that say nothing about the code.** Most false alarms were `OpenAIError: Missing
   credentials` raised inside the `openai` package, because the function needed a live client and the
   sandbox has no network, or a `NameError` for a `pytest` the checker forgot to import. Now only an
   assertion in the check, or an exception raised inside the repository's own code, counts. The real
   `ZeroDivisionError` in `lib/vectors.py` still counts.
4. **Agreement.** Two independent checkers, sampled at a higher temperature, and a problem stands
   only if both find one. One checker writing a wrong expected value happened for about one correct
   fix in four. Two writing wrong tests on the same fix is much rarer.

End state, offline: one false alarm on 17 correct solutions, and three of four wrong hard fixes
caught. It also flagged two routine fixes that the hidden oracle had passed. Both were real defects:
a truncation that dropped the documented `"..."` whenever the limit was 3 or less, and a bill
splitter that left shares like `3.3400000000000003`.

## What it did end to end

Offline numbers are not the product, so I ran the full benchmark with the checker on and off.
Qwen3-Coder 30B, local:

| | correct | wrong, delivered | withheld |
|---|---|---|---|
| 5 hard bugs × 3, without | 3 / 15 | 10 | 2 |
| 5 hard bugs × 3, with | 6 / 15 | 4 | 5 |
| 12 routine bugs, without (3 runs) | 35 / 36 | 0 | 1 |
| 12 routine bugs, with (2 runs) | 21 / 24 | 1 | 2 |

On hard bugs, wrong deliveries went from 10 to 4 and correct fixes from 3 to 6. The cosine case went
from wrong in every run to right in every run. On routine bugs it did slightly worse: two cases ended
unfixed and one wrong fix got through. It did not block a good fix: neither change it withheld would
have passed the oracle. Each shift also took two to four times longer.

So it ships as `--spec-check`, off by default, recommended for the work you cannot easily review.
Five cases repeated three times are not independent samples; the direction is clear, the decimals
are not.

## Why Strands made this straightforward

The checker is not a framework feature. It is three `Agent` objects with different system prompts,
tools and temperatures, sharing one budget hook, orchestrated by ordinary Python that decides what
each is allowed to see. Keeping the checker blind to the worker's code is a matter of what you pass
in, and in Strands that is explicit.

The lesson I would pass on: when an agent's output is verified by tests, ask who wrote the tests. If
the answer is "the same context that wrote the code", you have not verified anything.
