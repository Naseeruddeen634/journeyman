# Capacity, cost and the arithmetic behind the design

Every number here is either **assumed** (a planning figure, stated so it can be argued with and
replaced by production data) or **measured** (from a real Journeyman run). The point of writing the
arithmetic down is that it decides the architecture: the queue, the worker count and the separate
pool for fix jobs all fall out of these numbers, not out of taste.

## 1. Arrival rate

**Assumed:** 50,000 cases per day, arriving mostly in business hours.

```
50,000 cases/day ÷ 86,400 s          =  0.58 cases/s   averaged over the whole day
50,000 cases/day ÷ 28,800 s (8 h)    =  1.74 cases/s   averaged over business hours
peak = 3 × business-hours mean       =  5.2 cases/s    the number to design for
```

The gap between 0.58 and 5.2 is the reason for a queue. Sizing on the daily mean would leave the
service 9× under-provisioned at 11am on a Monday; sizing on the peak would leave most of the fleet
idle all night. A queue plus autoscaling on queue depth lets the fleet track the curve, and it
turns a traffic spike into latency instead of errors.

## 2. Triage workers

**Assumed:** per case, retrieval 50 ms, one model call p50 2 s and p95 6 s, validation and
persistence 50 ms. Design against p95, so service time ≈ **6.1 s**, of which 98% is waiting on the
model.

Little's law gives the concurrency needed:

```
in-flight = arrival rate × service time = 5.2/s × 6.1 s ≈ 32 concurrent cases
```

Because the work is almost entirely waiting on a network call, workers should be async: one
instance handling 10 concurrent cases is not CPU-bound.

```
32 concurrent ÷ 10 per instance  =  3.2 instances
+ 50% headroom                   =  5 instances at peak, scaling to 1 overnight
```

**The p95 target of 30 s (N1)** then has room: 6 s of model time, a second of our own work, and
about 23 s of slack for queue wait and retries. That slack is the budget for a degraded provider,
which is exactly when the queue gets deep.

## 3. What happens during a model outage

A ten-minute provider outage at peak, with the service degrading to retrieval-only after a failed
call (so nothing is lost, but the backlog of *AI* suggestions builds if we chose to retry instead):

```
backlog after 10 min = 5.2/s × 600 s            = 3,120 cases
drain rate = 10 concurrent ÷ 6.1 s per case
           × 5 instances                        ≈ 8.2 cases/s
net drain  = 8.2 − 5.2 (new arrivals)           = 3.0 cases/s
time to clear = 3,120 ÷ 3.0                     ≈ 17 minutes
```

Seventeen minutes of elevated latency, no lost cases, and the alert that fires is **queue age**,
not queue depth: depth alone cannot tell "deep and draining" from "shallow and stuck". If the
backlog is instead allowed to degrade immediately (the default), there is no backlog at all and the
cost is quality, which the dashboard shows as `degraded_share`.

## 4. Model cost

**Assumed:** 1,500 input tokens (case plus three retrieved neighbours plus instructions) and 250
output tokens per triage, on a cheap tier at $0.25 per million input and $1.25 per million output
(replace with the provider's real price list).

```
per day:  50,000 × 1,500 = 75,000,000 input tokens   → 75.0 M × $0.25/M  = $18.75
          50,000 ×   250 = 12,500,000 output tokens  → 12.5 M × $1.25/M  = $15.63
                                                       daily total       ≈ $34
                                                       monthly           ≈ $1,030
```

Three levers, in the order I would pull them:

1. **Do not re-triage unchanged text.** The case key includes the revision and the body hash, so an
   edit that does not change the text costs nothing. **Assumed** 15% of updates are cosmetic → ~$150/month.
2. **Cheap model first, escalate on failure.** Only cases whose first answer fails validation, or
   which are marked hard, go to a larger model. **Assumed** 10% escalation at 8× the price ≈ +80%
   of the cheap-tier cost, which is still far below sending everything to the large model.
3. **Shorter retrieval.** Three neighbours at ~200 tokens each is 40% of the input. Two neighbours
   saves ~13% of input cost; whether that hurts quality is an eval question, not an opinion.

A **per-tenant daily budget** is enforced in the ledger (implemented in `journeyman/support/store.py`),
so a runaway loop or an abusive tenant degrades that tenant to retrieval-only instead of producing a
surprise invoice.

## 5. Storage

**Assumed** 2 KB of case text on average, a ~500-byte case row, a ~1 KB suggestion row, 90-day
retention for text.

```
blob (case bodies):  50,000 × 2 KB   = 100 MB/day  → 9 GB at 90 days
relational rows:     50,000 × 1.5 KB =  75 MB/day  → 6.8 GB at 90 days
audit:               ~3 rows/case at 300 B ≈ 45 MB/day → 4 GB at 90 days
```

Under 20 GB after 90 days: a single managed Postgres or Azure SQL instance, with a read replica
added when the dashboard starts competing with the workers, not before.

**Embeddings deserve their own line.** At 1,024 dimensions and 4 bytes a dimension, one vector is
4 KB — twice the case text.

```
every case embedded:        50,000 × 4 KB = 200 MB/day → 18 GB at 90 days
only resolved cases (20%):  10,000 × 4 KB =  40 MB/day → 3.6 GB at 90 days
```

Only resolved cases are retrievable evidence, so only those are embedded. That is a 5× saving from
one sentence of design, and it is the kind of thing a diagram never shows.

## 6. Fix jobs

**Measured** on a laptop: a full shift on Claude in Bedrock, including the two blind checkers,
took 0.7 minutes and 12 model calls. **Assumed** a heavier repository takes 5 minutes.

```
200 jobs/day × 5 min = 1,000 job-minutes/day
1,000 ÷ 480 min (8 h) ≈ 2 concurrent jobs on average, bursting to ~10
```

Two to ten containers that scale to zero: Container Apps Jobs or equivalent, not a standing fleet.
They are on a **separate queue and pool** because their service time is 50× triage's; sharing a
pool would let a burst of fix jobs push triage past its p95 target.

## 7. Database load

```
writes: 2–3 per case (case row, suggestion, audit) → ~15 writes/s at peak
reads:  retrieval (1 vector query + 1 keyword query per case) → ~10 reads/s at peak
dashboard: a handful of aggregate queries per minute
```

Small. The database is not the bottleneck at this scale; the model is. What *would* hurt is a
dashboard query scanning every suggestion for an agreement rate on every page load, so that number
is computed from an index (`suggestions(tenant_id, prompt_version, human_decision)`) and cached for
a minute.

## 8. Availability arithmetic

99.9% monthly (N2) allows **43 minutes** of downtime a month. The ingest API is the only part that
has to meet it, because everything after the queue can be late without being unavailable. That is
why ingest does nothing but validate, write and enqueue: fewer dependencies, less to be down.

An error-budget rule worth agreeing up front: if half the monthly budget is spent, the next change
that ships is a reliability fix, not a feature.

## 9. What breaks at 10× and at 100×

| Scale | What breaks first | The change |
|---|---|---|
| 10× (500k cases/day, 52/s peak) | Model quota and cost; single Postgres write path | Provisioned throughput per region, batch embedding, partition by tenant, read replicas |
| 100× (5M cases/day) | One queue and one database per region | Shard by tenant, regional deployments with data residency, a real vector service instead of pgvector, tiered storage for old cases |

Neither is built now, and saying so is part of the design. The two decisions that keep those doors
open are that workers are stateless and that every key starts with the tenant id.
