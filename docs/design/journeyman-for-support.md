# Journeyman for Support: system design

**Status:** design, with a working slice in `journeyman/support/`.
**Author:** Naseeruddeen Shahul Hameed.
**Scope:** turning the single-machine Journeyman agent into a multi-tenant service that triages
support cases, keeps the product's own code honest, and escalates to a human when it cannot prove
its work.

Numbers in this document are labelled **measured** (taken from a real run of Journeyman) or
**assumed** (a planning figure, to be replaced by production data). Nothing here claims to be
measured unless it says so.

---

## 1. The problem

A support organisation running on Dynamics 365 Customer Service has three recurring costs:

1. **Triage latency.** A case arrives, and someone reads it to decide the queue, the severity and
   whether it is a known issue. Median human triage is minutes; the case waits.
2. **Repeat work.** The same product defect arrives as dozens of cases. Each is handled once.
3. **Silent quality drift.** The product's own AI features (summarisers, classifiers, reply
   drafters) fail quietly: an unbounded model call, an output nobody validates, a prompt nobody
   measures. Support absorbs the failure as ticket volume.

Journeyman already solves (3) on a laptop for one repository. This design turns it into a service
that also does (1) and (2), and — the part that matters — **will not hand back work it cannot
verify**.

### Users

| User | What they need |
|---|---|
| Support engineer | A case that arrives pre-triaged, with the evidence behind the suggestion |
| Support lead | Queue health, and a record of what the agent decided and why |
| Product engineer | A pull request for the defect behind repeated cases, already verified |
| Compliance / security | Proof that customer text did not leave the tenant boundary unexpectedly |

### Non-goals

- Replacing the support engineer. Every outward action is a suggestion until a human accepts it.
- Auto-replying to customers. The service drafts; a person sends.
- Auto-merging code. Journeyman commits to a branch and never pushes (already enforced in code).

---

## 2. Requirements

### Functional

- **F1** Ingest new and updated cases from Dynamics 365 / Dataverse, near real time.
- **F2** Triage a case: queue, severity, a summary, and up to three similar past cases.
- **F3** Write the triage back onto the case as a suggestion, with a confidence and the evidence.
- **F4** Detect clusters of cases pointing at one defect, and open a *fix job* for the owning repo.
- **F5** Run a fix job through the existing shift pipeline (worktree, verification, blind spec
  check) and produce a branch plus a pull-request description, or a refusal with the reason.
- **F6** Give leads a dashboard: queue, throughput, agreement rate, cost, refusals, live-site health.
- **F7** Let a human accept, edit or reject any suggestion; record the decision as eval data.

### Non-functional

| Id | Requirement | Target |
|---|---|---|
| N1 | Triage latency | p95 under 30 s from case created to suggestion posted (**assumed**) |
| N2 | Availability of the triage API | 99.9% monthly |
| N3 | Ingestion durability | No case dropped; at-least-once with idempotent handling |
| N4 | Tenant isolation | One tenant's case text is never in another tenant's prompt or store |
| N5 | Cost ceiling | Hard budget per tenant per day; the service degrades rather than overspends |
| N6 | Quality gate | A prompt change ships only if the held-out eval does not regress |
| N7 | Explainability | Every suggestion links to the inputs and the model version that produced it |
| N8 | Recovery | A poisoned or stuck job never blocks the queue; retries are bounded and visible |

### Scale assumptions (planning figures)

- 50,000 cases/day across tenants, peak 3× the mean for four hours → ~5 cases/s peak.
- 2 KB of case text on average; 20 KB p99.
- Triage: one model call (~1,500 input tokens, ~250 output).
- Fix jobs: 200/day, each minutes long, bursty.

Two workloads with very different shapes: **short, high-volume, latency-sensitive** (triage) and
**long, low-volume, resource-hungry** (fix shifts). They get separate queues and separate worker
pools, because one runaway fix must not delay triage.

---

## 3. Architecture

```
Dynamics 365 / Dataverse ──(webhook or Power Automate)──► Ingest API ──► case.events queue
                                                                              │
                                        ┌─────────────────────────────────────┘
                                        ▼
                               Triage workers (stateless, autoscaled)
                                        │   ├─ retrieval: similar cases (vector + keyword)
                                        │   ├─ model call via the brain router
                                        │   └─ deterministic validation of the output
                                        ▼
                                 Postgres / Azure SQL  ◄──► Dashboard API ──► React dashboard
                                        │
                                        ├─ writeback worker ──► Dataverse (suggestion on the case)
                                        │
                                        └─ cluster detector (scheduled) ──► fix.jobs queue
                                                                                 │
                                                                                 ▼
                                                                   Shift runners (isolated,
                                                                   one container per job:
                                                                   git worktree + sandbox +
                                                                   verification + spec check)
                                                                                 │
                                                                                 ▼
                                                                    branch + PR description,
                                                                    or a recorded refusal
```

### Components

| Component | Responsibility | Why it is separate |
|---|---|---|
| **Ingest API** | Accept Dataverse webhooks, authenticate, deduplicate, enqueue | Must stay up and fast even when models are down |
| **case.events queue** | Durable buffer between the support system and us | Absorbs bursts; lets us redeploy workers without dropping cases |
| **Triage workers** | Retrieval, one model call, deterministic validation, persist | Scales on queue depth; stateless so any instance can die |
| **Writeback worker** | Push suggestions to Dataverse, honouring its throttling | Isolates the one component that writes to the customer's system |
| **Cluster detector** | Group cases by signature, open fix jobs above a threshold | Batch job; different cadence from per-case work |
| **Shift runners** | The existing Journeyman shift, one container per job | Needs a writable worktree, a sandbox and minutes of CPU; must not share a process with triage |
| **Brain router** | Pick a model per task, enforce budgets, fall back | One place for cost, quota and provider outages |
| **Dashboard API + UI** | Queue health, agreement rate, cost, refusals, audit trail | The product surface for leads; read-mostly |

### What is reused from Journeyman today

`scout`, `shift`, the verification gate, the blind spec check, `review`, `inventory`, the budget
hook and the sandbox already exist and are tested (measured: 358 tests). The service wraps them;
it does not reimplement them. The AgentCore deployment already proves the review can run as a
remote service.

---

## 4. Request flows

### 4.1 Triage (synchronous path, target p95 30 s)

1. Dataverse fires a webhook on case create/update. Ingest validates the signature, extracts
   `{tenant, case_id, title, body, product, created_at}`, and writes a row in `cases` with
   `state = 'queued'`. The insert is keyed on `(tenant_id, case_id, revision)`, so a redelivered
   webhook is a no-op.
2. Ingest enqueues `case.events`. It returns 202 as soon as the row and the message are durable.
3. A triage worker leases the message and:
   - **Retrieval:** top-5 similar resolved cases for that tenant (vector search on embeddings plus
     a keyword filter on product and error code).
   - **Prompt:** a versioned template; customer text goes in a clearly delimited user block, never
     in the system prompt (this is what Journeyman's own AIE004 check exists to catch).
   - **Model call:** through the brain router with `max_tokens` set, a timeout, and adaptive retry.
   - **Validate deterministically:** the output must parse as JSON against a schema, the queue must
     be one of the tenant's real queues, severity must be in range, and the summary must not exceed
     N words. A failure here is a *rejected suggestion*, recorded, not posted.
   - **Persist** the suggestion with model id, prompt version, latency, token counts and cost.
4. The writeback worker posts the suggestion to the case (a note or custom field) with a link back
   to the evidence.

**Degradation:** if the model is unavailable or the tenant's budget is exhausted, triage still
returns retrieval-only output ("3 similar cases, no AI summary") and the dashboard shows why. The
case is never blocked on the model.

### 4.2 Fix job (asynchronous, minutes)

1. The cluster detector runs every 15 minutes: group open cases by `(product, error signature)`;
   above a threshold (**assumed** ≥ 5 cases in 24 h), open a fix job for the owning repository.
2. A shift runner claims the job, clones the repo at a pinned commit into a throwaway worktree,
   and runs the existing shift: fix, verify, blind spec check.
3. Outcome:
   - **Delivered:** branch pushed to a service-owned fork (never the mainline), PR description
     generated, a link attached to every case in the cluster.
   - **Withheld:** the refusal and its reason are recorded and shown to the owning team. This is a
     first-class outcome, not an error (measured on the hard benchmark: refusals prevented 6 of 10
     wrong fixes from being delivered).
4. A human reviews the diff and opens the pull request. The service never merges.

---

## 5. Data model

Postgres or Azure SQL. Sketch (Postgres syntax; see `docs/design/schema.sql`):

- `tenants(tenant_id, name, budget_daily_cents, model_policy, created_at)`
- `cases(tenant_id, case_id, revision, product, title, body_ref, state, created_at, updated_at)` —
  primary key `(tenant_id, case_id, revision)`; `body_ref` points at blob storage for large text.
- `suggestions(id, tenant_id, case_id, kind, payload jsonb, model_id, prompt_version, confidence,
  cost_cents, latency_ms, created_at, posted_at, human_decision, decided_by, decided_at)`
- `clusters(id, tenant_id, signature, product, first_seen, last_seen, case_count, fix_job_id)`
- `fix_jobs(id, tenant_id, repo, commit_sha, state, outcome, branch, refusal_reason, started_at,
  finished_at, cost_cents)`
- `evals(id, tenant_id, prompt_version, split, case_id, expected jsonb, graded_at, passed)`
- `audit(id, tenant_id, actor, action, subject, detail jsonb, at)`

Indexes that matter: `cases(tenant_id, state, created_at)` for the work queue view;
`suggestions(tenant_id, created_at desc)` for the dashboard; `clusters(tenant_id, signature)` for
the detector; a vector index on case embeddings per tenant.

**Retention:** case text 90 days (**assumed**, tenant-configurable), suggestions and audit 2 years,
embeddings deleted with the case. Deletion is a tenant-scoped job, and there is a test for it,
because "we forgot the embeddings" is the classic GDPR miss.

---

## 6. API

Small and boring on purpose (`docs/design/openapi.yaml`):

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/ingest/case` | Dataverse webhook target. Signature-verified, idempotent |
| `GET` | `/v1/cases/{case_id}/suggestion` | Latest suggestion plus its evidence |
| `POST` | `/v1/suggestions/{id}/decision` | accept / edit / reject; becomes eval data |
| `GET` | `/v1/clusters` | Open clusters and their fix jobs |
| `POST` | `/v1/fix-jobs` | Open a fix job by hand (lead or engineer) |
| `GET` | `/v1/fix-jobs/{id}` | State, outcome, branch, refusal reason |
| `GET` | `/v1/health` | Liveness; `/v1/ready` checks DB, queue and one model probe |

Auth: Entra ID (Azure AD) app tokens for service-to-service, on-behalf-of for dashboard users.
Every endpoint is tenant-scoped by the token, never by a path parameter the caller chooses.

---

## 7. Cross-cutting design decisions

### 7.1 Deterministic first, model second

The rule Journeyman already follows: **facts from code, words from the model.** Retrieval,
validation, severity bounds and the queue list are deterministic. The model writes the summary and
proposes a label. If the model disagrees with a deterministic rule, the rule wins and the
disagreement is logged — that log is the eval set for the next prompt version.

### 7.2 The brain router

One component owns model choice: cheap model first (Haiku-class or a local open model for
self-hosted deployments), escalate to a larger model only when the task is marked hard or the
cheap model's output fails validation. It enforces per-tenant daily budgets, per-request token
limits, timeouts and adaptive retry, and it emits cost per suggestion. Journeyman's existing
`pick()` and budget hook are the seed of this.

Provider outage: the router fails over between deployments and regions; if all fail, the service
degrades to retrieval-only (see 4.1) rather than queueing an ever-growing backlog.

### 7.3 Quality gates in the pipeline

A prompt or model change is a code change and goes through the same CI Journeyman already ships:
- the eval set for that prompt must not regress on the **held-out** split;
- `journeyman review` must find no new AI-engineering finding that the change introduced;
- the blind spec check runs on fix jobs, off by default for routine ones (measured: it halves
  wrong deliveries on hard bugs but costs time, so it is a per-repo setting).

### 7.4 Tenant isolation

- Separate schemas (or separate databases for large tenants) and row-level security keyed to the
  token's tenant claim.
- Prompts are assembled per request from that tenant's data only; there is no shared few-shot pool
  across tenants.
- Shift runners get a per-job container, a per-job credential with access to exactly one
  repository, and no network beyond the package proxy and the model endpoint.
- Journeyman's existing sandbox rules (no network, no credential reads, writes confined to the
  worktree) apply inside the runner.

### 7.5 Idempotency and exactly-once-enough

Queues are at-least-once. Everything downstream is idempotent: the `cases` primary key includes the
revision; `suggestions` are upserted on `(case_id, prompt_version, model_id, input_hash)`; writeback
uses a deterministic external id so a redelivered message updates rather than duplicates the note.

### 7.6 Failure modes

| Failure | Detection | Response |
|---|---|---|
| Dataverse webhook storm | Ingest rate metric | Queue absorbs; workers autoscale; backpressure via 429 with retry-after |
| Model provider 5xx / throttle | Router error rate | Adaptive retry, failover, then retrieval-only degradation |
| Model returns unparseable output | Validation failure counter | Suggestion rejected, case untouched, sample stored for the eval set |
| Poison message | Delivery count | After 5 attempts to dead-letter with the full context; alert on DLQ depth > 0 |
| Shift runner hangs | Job heartbeat | Budget watchdog cancels (already in Journeyman), container killed, job marked timed out |
| Runaway cost | Cost per tenant per hour | Hard stop at the budget; degrade; page if a single tenant crosses 3× its mean |
| Bad prompt deployed | Held-out eval in CI; agreement rate in prod | Block at CI; if it lands, flip the prompt version flag back (no redeploy) |
| Database failover | Connection errors | Retries with jitter; workers hold leases; queue keeps the work |

### 7.7 Observability and live site

Every request carries a correlation id from the webhook through to the model call and the writeback.

- **Metrics:** ingest rate, queue depth and age, triage latency (p50/p95/p99), validation failure
  rate, model error rate, tokens and cost per tenant, agreement rate (human accepted vs edited vs
  rejected), fix-job outcomes (delivered / withheld / timed out).
- **Traces:** one span per stage; the model call records model id, prompt version, token counts.
- **Logs:** structured, with case ids but **never** raw case text outside the tenant's store.
- **Alerts that page:** queue age > 10 min, triage p95 > 60 s for 15 min, DLQ non-empty,
  writeback failure rate > 5%, cost per tenant over budget, agreement rate down more than 10 points
  week over week.
- **Runbook per alert**, linked from the alert itself. Journeyman's `doctor` becomes the service's
  readiness probe: it already checks for the silent failures that cost the most time (a model
  context that silently truncates, a test runner that reports green because it never ran, a
  scheduler registered but never firing).

### 7.8 Rollout

1. **Shadow mode.** Triage runs and records suggestions; nothing is written back. Measure agreement
   against what humans actually did.
2. **Suggest mode** for one tenant, one queue: suggestions are posted but clearly marked.
3. **Widen** by queue and tenant with a feature flag per tenant, never a global switch.
4. **Fix jobs** last, starting with repositories that have a strong test suite; branch-only, and a
   human opens every PR.

Each stage has an exit criterion measured on production data, not a date.

---

## 8. Technology choices, and the honest trade-off

| Concern | Choice | Why, and what it costs |
|---|---|---|
| Ingest & APIs | ASP.NET Core (C#) or FastAPI (Python) | C# matches a Microsoft support stack and Dataverse SDKs; Python keeps one language with the agent core. Pick per team; the contract is HTTP + queue, so either works |
| Queue | Azure Service Bus (or SQS) | Sessions for per-case ordering, DLQ, scheduled delivery |
| Store | Azure SQL / Postgres + blob for case bodies | Relational is right for audit and joins; blob keeps rows small |
| Retrieval | pgvector or Azure AI Search | Start with pgvector: one fewer system until scale demands otherwise |
| Compute | Container Apps / AKS (triage), Container Apps Jobs (shift runners) | Runners need per-job isolation and minutes of CPU |
| Models | Azure OpenAI / Azure AI Foundry, Amazon Bedrock, or a local open model | The router keeps the core portable; today's Journeyman already runs on Ollama and Bedrock |
| Front end | React + TypeScript | Journeyman's TypeScript review already covers this code |
| Ticketing integration | Dataverse Web API + Power Automate | Power Automate lets support ops change the trigger without an engineer |

**The trade-off to say out loud:** this design is portable at the cost of not using the deepest
platform features. A pure Dataverse/Power Platform implementation (plug-ins, custom API, Copilot
Studio) would be faster to ship inside one tenant and harder to run anywhere else. The queue and
the runner isolation are the two places I would not compromise, because they are what keep triage
fast and untrusted code contained.

---

## 9. Cost model (planning)

Per triage (**assumed** 1,500 in / 250 out tokens on a cheap model): fractions of a cent, dominated
by the model. At 50,000 cases/day that is the largest single line, so: cheap model first, cache
retrieval, and never re-triage an unchanged case (the revision key in `cases` makes that automatic).

Fix jobs are minutes of CPU plus a larger model; 200/day is small against engineer time, but the
per-job budget is hard-capped (**measured** on the laptop: a full shift on Claude in Bedrock,
including the blind checkers, was 12 model calls and finished in 0.7 minutes).

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Agreement rate too low to be useful | Shadow mode first; ship only above an agreed threshold |
| Customer text leaking into another tenant's prompt | Per-tenant assembly, tests that assert isolation, no shared few-shot pool |
| Over-trust: humans rubber-stamp suggestions | Show confidence and evidence; sample audits; track edit rate, not just accept rate |
| The agent's fixes are wrong on hard bugs | Keep the blind spec check on for those repos; branch-only; human opens the PR (measured: hard-bug wrong deliveries 10 → 4 with the check on) |
| Dataverse throttling | Writeback worker with its own rate limiter and backoff |

---

## 11. What exists today vs what this design adds

| Piece | Status |
|---|---|
| Review, inventory, eval, pair, shift, verification, blind spec check, sandbox, budgets | **Built and tested** (358 tests) |
| Remote reviewer as a service | **Built and deployed** on Amazon Bedrock AgentCore |
| Case ingestion, triage worker, clustering, writeback, dashboard, SQL store | **This design**; a working slice lives in `journeyman/support/` |
| Azure deployment, Dataverse integration, vector retrieval | **Designed, not built** |

The slice in `journeyman/support/` implements the triage path end to end against an in-process
queue and SQLite, with the deterministic validation, the budget guard, the idempotency key and the
degradation path, so the design's riskiest claims are executable rather than asserted.
