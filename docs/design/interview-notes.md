# Journeyman for Support: the questions a reviewer will ask

Notes for defending this design in a room. Each answer is short on purpose; the detail is in
`journeyman-for-support.md` and `capacity.md`.

## Why a queue at all? The webhook could call the model directly.

Three reasons, in order of how much they hurt:

1. **The arrival curve.** 0.58 cases/s averaged over a day, 5.2/s at the Monday morning peak
   (`capacity.md` §1). A synchronous path has to be provisioned for the peak all night.
2. **Dependency blast radius.** A model outage would turn into webhook timeouts inside the
   customer's support system. With a queue, ingest has two dependencies (database, queue) and the
   model outage becomes latency or a degraded suggestion.
3. **Deployments.** Workers can be replaced mid-day without dropping cases.

The cost is that the system is now eventually consistent, and I have to design for redelivery
everywhere. That is why the case key includes the revision and suggestions are unique on their
inputs.

## Exactly-once? How do you avoid double-posting a suggestion to a case?

I don't get exactly-once delivery; I get at-least-once plus idempotent handling, which is the same
outcome and far simpler:

- `cases` is keyed `(tenant_id, case_id, revision)`, so a redelivered webhook inserts nothing and
  enqueues nothing (test: `test_a_redelivered_webhook_does_no_work_twice`).
- `suggestions` is unique on `(case, revision, kind, prompt_version, model_id, input_hash)`, so a
  worker that crashes after the model call but before the ack writes the same row on retry rather
  than a second one.
- Writeback uses a deterministic external id, so Dataverse updates the same note.

## What stops one tenant from ruining the day for everyone?

- **Cost:** a per-tenant daily ledger, checked before the call and charged after it. At zero, that
  tenant degrades to retrieval-only; everyone else is unaffected.
- **Throughput:** per-tenant concurrency caps, and a `Retry-After` on ingest under load.
- **Data:** every key starts with the tenant id, retrieval is tenant-scoped, and the prompt is
  assembled from that tenant's rows only. There is a test that a similar case belonging to another
  tenant never reaches the prompt.
- **Fix jobs:** a separate queue and pool, so a repository that takes ten minutes to test cannot
  delay anybody's triage.

## How do you know the triage is any good?

Agreement rate, from the humans, by prompt version: accepted, edited, rejected. Two rules:

- **Shadow mode first.** Suggestions are recorded and not posted until the rate is good enough on
  real cases. The threshold is agreed with the support lead before the experiment, not after it.
- **Decisions become the eval set.** Every human decision is a labelled example. A prompt or model
  change ships only if the **held-out** split does not regress. This is the same harness Journeyman
  already uses (`journeyman eval`), and the held-out split is assigned once so nobody can re-split
  their way to a better number.

I watch the *edit* rate as closely as the accept rate: a high accept rate with no edits can mean
people are rubber-stamping, which is a worse failure than a rejected suggestion.

## What happens when the model returns nonsense?

Nothing is posted. The answer is parsed, and the queue name, severity range and summary length are
checked against the tenant's real configuration. A failure is recorded with the raw output and the
case is left for a human (test: `test_a_rejected_answer_is_not_posted_and_is_kept_for_the_eval_set`).
The validation failure rate is on the dashboard and pages someone above 10%, because that pattern
usually means a prompt or model change, not a bad day.

## Prompt injection: the case text is written by a stranger.

- Case text goes in a delimited data block, never in the system prompt, and the instructions say
  the block is data.
- The model cannot act: it returns a label and a summary, and both are validated against a fixed
  set. There is no tool it can call, nothing it can send, and no code path where its output is
  executed.
- The reviewer that Journeyman already ships flags exactly this pattern (AIE004) in our own code,
  and CI fails the build on new findings.

## How do you roll back a bad prompt?

The prompt version is data, not a deployment: suggestions record the version that produced them, and
the active version is a per-tenant flag. Rollback is a flag flip, and the agreement rate is
comparable across versions because the eval set is fixed.

## Where does this fall over?

- **Retrieval quality.** Token overlap is a placeholder. If neighbours are weak, the suggestion is
  weak, and the honest answer is that this needs embeddings and an eval of retrieval itself.
- **Clustering.** Grouping by error signature works for errors with codes and poorly for prose.
  I would start with the codes and leave the rest to humans rather than pretend it generalises.
- **Fix jobs on hard bugs.** Measured: on five hard cases repeated three times, without the blind
  check the agent delivered 10 wrong fixes out of 15 attempts; with it, 4. That is better, not good.
  Hence branch-only, a human opens every PR, and repositories opt in.
- **Vector storage.** 4 KB per embedding is twice the case text; embedding everything would cost
  more in storage than the model calls. Only resolved cases are embedded.

## What would you cut to ship in six weeks?

Keep: ingest, queue, triage with validation, writeback, the budget, the dashboard's five numbers.
Cut: clustering and fix jobs (they depend on triage being trusted first), the vector store (token
overlap is enough to prove the loop), multi-region, and the reply-draft feature. Shadow mode makes
the cut safe: the first six weeks produce data about whether the rest is worth building.

## What would you do differently if this were Microsoft-internal rather than generic?

Lean into the platform instead of abstracting it: a Dataverse plug-in or custom API rather than a
webhook, Power Automate for the trigger so support ops can change it without an engineer, Entra ID
groups for RBAC, and Azure OpenAI in the same tenant so case text never leaves the compliance
boundary. The trade is portability, which matters less inside one company than shipping speed and
one fewer system to run.

## What in this is actually built?

`journeyman/support/` runs the ingest → queue → triage → validation → store → health path with 20
tests, and `journeyman support` demonstrates it end to end offline. The agent underneath it
(review, shift, verification, blind spec check, sandbox, budgets) is the existing, tested
Journeyman, and its reviewer is deployed on Amazon Bedrock AgentCore. Everything else on the
diagram — Dataverse integration, Azure deployment, vector retrieval, the dashboard UI — is designed
and not built, and the document says so in §11.
