-- Journeyman for Support: relational schema (PostgreSQL dialect).
-- Azure SQL differences are noted where they matter.
--
-- Design rules visible here:
--   * every table is tenant-scoped and every index leads with tenant_id
--   * case text lives in blob storage; rows stay small and cheap to scan
--   * a case revision is part of the key, so a redelivered webhook cannot duplicate work
--   * suggestions are immutable facts: a new model or prompt version writes a new row

CREATE TABLE tenants (
    tenant_id           TEXT PRIMARY KEY,
    name                TEXT        NOT NULL,
    budget_daily_cents  INTEGER     NOT NULL DEFAULT 2000,
    model_policy        TEXT        NOT NULL DEFAULT 'cheap_first',  -- cheap_first | quality_first | local_only
    spec_check_repos    TEXT[]      NOT NULL DEFAULT '{}',           -- repos where the blind check stays on
    retention_days      INTEGER     NOT NULL DEFAULT 90,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cases (
    tenant_id    TEXT        NOT NULL REFERENCES tenants(tenant_id),
    case_id      TEXT        NOT NULL,          -- Dataverse incident id
    revision     INTEGER     NOT NULL,          -- versionnumber from Dataverse
    product      TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    body_ref     TEXT        NOT NULL,          -- blob URI; never the raw text in this row
    body_sha256  TEXT        NOT NULL,          -- lets us skip re-triage of unchanged text
    language     TEXT,
    state        TEXT        NOT NULL DEFAULT 'queued',  -- queued|triaged|rejected|degraded|closed
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, case_id, revision)
);

-- The work-queue view: "what is still queued, oldest first, for this tenant".
CREATE INDEX cases_worklist ON cases (tenant_id, state, created_at);
-- Clustering and retention both scan by product and age.
CREATE INDEX cases_by_product ON cases (tenant_id, product, created_at DESC);

CREATE TABLE suggestions (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    case_id         TEXT        NOT NULL,
    revision        INTEGER     NOT NULL,
    kind            TEXT        NOT NULL,       -- triage | reply_draft | kb_gap
    payload         JSONB       NOT NULL,       -- validated against the kind's schema before insert
    evidence        JSONB       NOT NULL,       -- retrieved case ids, rule hits, prompt inputs hash
    confidence      REAL,
    model_id        TEXT        NOT NULL,       -- 'retrieval-only' when the model was skipped
    prompt_version  TEXT        NOT NULL,
    input_hash      TEXT        NOT NULL,       -- idempotency: same inputs, same row
    latency_ms      INTEGER     NOT NULL,
    input_tokens    INTEGER     NOT NULL DEFAULT 0,
    output_tokens   INTEGER     NOT NULL DEFAULT 0,
    cost_cents      NUMERIC(10, 4) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at       TIMESTAMPTZ,                -- when writeback reached Dataverse
    human_decision  TEXT,                       -- accepted | edited | rejected | untouched
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    FOREIGN KEY (tenant_id, case_id, revision) REFERENCES cases (tenant_id, case_id, revision)
);

-- One suggestion per (case revision, prompt version, model, inputs): a retry writes nothing new.
CREATE UNIQUE INDEX suggestions_idempotent
    ON suggestions (tenant_id, case_id, revision, kind, prompt_version, model_id, input_hash);
CREATE INDEX suggestions_recent ON suggestions (tenant_id, created_at DESC);
-- Agreement rate by prompt version is the number that decides whether a change shipped well.
CREATE INDEX suggestions_agreement ON suggestions (tenant_id, prompt_version, human_decision);

CREATE TABLE clusters (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT        NOT NULL,
    signature    TEXT        NOT NULL,          -- normalised error signature or symptom key
    product      TEXT        NOT NULL,
    case_count   INTEGER     NOT NULL DEFAULT 0,
    first_seen   TIMESTAMPTZ NOT NULL,
    last_seen    TIMESTAMPTZ NOT NULL,
    fix_job_id   BIGINT,
    UNIQUE (tenant_id, signature)
);

CREATE TABLE cluster_cases (
    cluster_id BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    tenant_id  TEXT   NOT NULL,
    case_id    TEXT   NOT NULL,
    PRIMARY KEY (cluster_id, tenant_id, case_id)
);

CREATE TABLE fix_jobs (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    cluster_id      BIGINT REFERENCES clusters(id),
    repo            TEXT        NOT NULL,
    commit_sha      TEXT        NOT NULL,       -- pinned: the job is reproducible
    task            TEXT        NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'queued',   -- queued|running|done|timed_out|error
    outcome         TEXT,                        -- delivered | withheld | no_work | regressed
    branch          TEXT,
    pr_body_ref     TEXT,
    refusal_reason  TEXT,                        -- why it would not commit: a first-class result
    spec_check      TEXT,                        -- 'passed: N independent test(s)' or the failure
    attempts        INTEGER     NOT NULL DEFAULT 0,
    heartbeat_at    TIMESTAMPTZ,                 -- a stuck runner is detected by a stale heartbeat
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    cost_cents      NUMERIC(10, 4) NOT NULL DEFAULT 0
);

CREATE INDEX fix_jobs_queue ON fix_jobs (state, heartbeat_at);
CREATE INDEX fix_jobs_by_tenant ON fix_jobs (tenant_id, finished_at DESC);

-- Human decisions become the eval set for the next prompt version.
CREATE TABLE evals (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT        NOT NULL,
    prompt_version TEXT        NOT NULL,
    split          TEXT        NOT NULL,        -- train | holdout   (assigned once, never re-split)
    case_id        TEXT        NOT NULL,
    input_ref      TEXT        NOT NULL,
    expected       JSONB       NOT NULL,        -- what the human actually did
    passed         BOOLEAN,
    graded_at      TIMESTAMPTZ,
    UNIQUE (tenant_id, prompt_version, case_id)
);

CREATE TABLE budget_ledger (
    tenant_id  TEXT        NOT NULL,
    day        DATE        NOT NULL,
    spent_cents NUMERIC(12, 4) NOT NULL DEFAULT 0,
    calls      INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

CREATE TABLE audit (
    id        BIGSERIAL PRIMARY KEY,
    tenant_id TEXT        NOT NULL,
    actor     TEXT        NOT NULL,             -- user upn, or 'service:triage-worker'
    action    TEXT        NOT NULL,             -- suggestion.posted, decision.recorded, job.withheld
    subject   TEXT        NOT NULL,
    detail    JSONB       NOT NULL,
    at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_by_subject ON audit (tenant_id, subject, at DESC);

-- Row-level security: every query is scoped by the token's tenant claim, not by a path parameter.
ALTER TABLE cases       ENABLE ROW LEVEL SECURITY;
ALTER TABLE suggestions ENABLE ROW LEVEL SECURITY;
CREATE POLICY cases_tenant_isolation ON cases
    USING (tenant_id = current_setting('app.tenant_id', TRUE));
CREATE POLICY suggestions_tenant_isolation ON suggestions
    USING (tenant_id = current_setting('app.tenant_id', TRUE));

-- Retrieval. Start with pgvector in the same database; move to a search service only if it stops
-- being enough. One system fewer is worth a lot in the first year.
-- CREATE EXTENSION IF NOT EXISTS vector;
-- ALTER TABLE cases ADD COLUMN embedding vector(1024);
-- CREATE INDEX cases_embedding ON cases USING hnsw (embedding vector_cosine_ops);

-- Azure SQL notes:
--   BIGSERIAL      -> BIGINT IDENTITY(1,1)
--   TIMESTAMPTZ    -> DATETIMEOFFSET
--   JSONB          -> NVARCHAR(MAX) with ISJSON() constraints, or the native JSON type
--   TEXT[]         -> a child table; arrays are not portable
--   Row-level security exists with the same shape (SESSION_CONTEXT instead of current_setting).
