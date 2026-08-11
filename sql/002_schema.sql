-- Unsay :: agent memory schema
--
--   cockroach sql --insecure --host=localhost:26257 -d unsay -f sql/002_schema.sql
--
-- The design in one sentence: every safety claim is versioned in two time
-- dimensions, every agent decision records exactly which claim versions it
-- read, and those two facts together make it possible to go back and repair
-- answers that were correct when given and are wrong now.

USE unsay;

-- ===========================================================================
-- 1. FACTS :: bitemporal store of drug-safety claims
-- ===========================================================================
--
-- Two independent time axes, which is what "bitemporal" means:
--
--   valid_from / valid_to      when the claim is true IN THE WORLD.
--                              An FDA recall is valid from its initiation
--                              date until its termination date.
--
--   asserted_at / retracted_at when THIS SYSTEM believed the claim.
--                              Set by ingestion, never by the source.
--
-- Keeping them separate is what distinguishes "the drug became dangerous on
-- March 3rd" from "we found out on July 2nd". Agent memory that collapses
-- these into one timestamp cannot tell you whether an answer was negligent
-- or merely unlucky.

CREATE TABLE IF NOT EXISTS fact (
    fact_key       STRING       NOT NULL,
    version        INT4         NOT NULL,
    fact_id        UUID         NOT NULL DEFAULT gen_random_uuid(),

    subject_kind   STRING       NOT NULL,
    subject_id     STRING       NOT NULL,
    predicate      STRING       NOT NULL,

    claim          STRING       NOT NULL,
    severity       STRING       NOT NULL,
    payload        JSONB        NOT NULL DEFAULT '{}'::JSONB,

    -- valid time
    valid_from     TIMESTAMPTZ  NOT NULL,
    valid_to       TIMESTAMPTZ  NULL,

    -- transaction time
    asserted_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retracted_at   TIMESTAMPTZ  NULL,

    source         STRING       NOT NULL,
    source_ref     STRING       NOT NULL,
    content_hash   STRING       NOT NULL,

    embedding      VECTOR(1024) NULL,

    -- Stored so it can serve as a vector-index prefix column: retrieval then
    -- spends its entire top-K budget on claims we currently believe, instead
    -- of filtering retracted ones out afterwards and silently losing recall.
    believed       BOOL         NOT NULL AS (retracted_at IS NULL) STORED,

    CONSTRAINT pk_fact PRIMARY KEY (fact_key, version DESC),
    CONSTRAINT uq_fact_id UNIQUE (fact_id),
    CONSTRAINT ck_severity CHECK (severity IN
        ('class_i','class_ii','class_iii','boxed_warning','warning','info')),
    CONSTRAINT ck_predicate CHECK (predicate IN
        ('recall','boxed_warning','contraindication','interaction','dosage')),
    CONSTRAINT ck_valid_order CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- Current-belief lookups by drug. Partial index: retracted versions are the
-- majority of rows over time and never appear in live retrieval.
CREATE INDEX IF NOT EXISTS fact_subject_live
    ON fact (subject_id, predicate, severity)
    STORING (claim, valid_from, valid_to)
    WHERE retracted_at IS NULL;

-- Change-detection probe used by ingestion to decide whether an incoming
-- record is genuinely new or a byte-identical repeat of what we already hold.
CREATE INDEX IF NOT EXISTS fact_hash ON fact (content_hash);

-- ===========================================================================
-- 2. PEOPLE :: patients and what was actually dispensed to them
-- ===========================================================================
--
-- REGIONAL BY ROW pins each patient's row to their home region. EU patient
-- memory is stored in eu-west-1 and enforced by the storage layer rather than
-- by application code that can be bypassed.

CREATE TABLE IF NOT EXISTS patient (
    patient_id   UUID        NOT NULL DEFAULT gen_random_uuid(),
    mrn          STRING      NOT NULL,
    display_name STRING      NOT NULL,
    contact      STRING      NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_patient PRIMARY KEY (patient_id),
    CONSTRAINT uq_patient_mrn UNIQUE (mrn)
);

-- The lot number is the point. The peer-reviewed attempt at automated recall
-- notification (Automating Individualized Notification of Drug Recalls to
-- Patients, 2024) failed precisely here: "it was not possible to trace a
-- medication prescription from the EHR to specific lot numbers dispensed to
-- that patient by a community pharmacy." Unsay carries the lot end to end,
-- so a lot-scoped recall resolves to named people instead of a whole drug.
CREATE TABLE IF NOT EXISTS dispense (
    dispense_id  UUID        NOT NULL DEFAULT gen_random_uuid(),
    patient_id   UUID        NOT NULL,
    drug_name    STRING      NOT NULL,
    subject_id   STRING      NOT NULL,
    lot_number   STRING      NULL,
    ndc          STRING      NULL,
    quantity     INT4        NOT NULL DEFAULT 0,
    dispensed_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT pk_dispense PRIMARY KEY (dispense_id),
    CONSTRAINT fk_dispense_patient FOREIGN KEY (patient_id)
        REFERENCES patient (patient_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS dispense_subject ON dispense (subject_id, lot_number);
CREATE INDEX IF NOT EXISTS dispense_patient ON dispense (patient_id, dispensed_at DESC);

-- ===========================================================================
-- 3. DECISIONS :: what the agent said, and what it read to say it
-- ===========================================================================

CREATE TABLE IF NOT EXISTS decision (
    decision_id  UUID         NOT NULL DEFAULT gen_random_uuid(),
    patient_id   UUID         NULL,
    question     STRING       NOT NULL,
    answer       STRING       NOT NULL,
    verdict      STRING       NOT NULL,
    confidence   FLOAT8       NOT NULL DEFAULT 0,

    model_id     STRING       NOT NULL,
    decided_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- The cluster's HLC at the instant memory was read. Inside the GC window
    -- this replays the exact read set via AS OF SYSTEM TIME; outside it, the
    -- bitemporal columns above reconstruct the same state exactly.
    read_hlc     DECIMAL      NOT NULL DEFAULT cluster_logical_timestamp(),

    status       STRING       NOT NULL DEFAULT 'standing',
    embedding    VECTOR(1024) NULL,

    CONSTRAINT pk_decision PRIMARY KEY (decision_id),
    CONSTRAINT fk_decision_patient FOREIGN KEY (patient_id)
        REFERENCES patient (patient_id) ON DELETE CASCADE,
    CONSTRAINT ck_verdict CHECK (verdict IN ('safe','caution','stop','unknown')),
    CONSTRAINT ck_status  CHECK (status  IN ('standing','reaffirmed','reversed'))
);

CREATE INDEX IF NOT EXISTS decision_standing
    ON decision (decided_at DESC)
    WHERE status = 'standing';

-- Provenance. Written inside the SAME transaction as the decision, so an
-- answer can never exist without the record of what produced it. This table
-- is the entire reason retroactive repair is possible.
CREATE TABLE IF NOT EXISTS decision_read (
    decision_id  UUID    NOT NULL,
    fact_key     STRING  NOT NULL,
    fact_version INT4    NOT NULL,
    rank         INT4    NOT NULL,
    similarity   FLOAT8  NOT NULL,

    -- Whether the model actually leaned on this claim, as opposed to merely
    -- being shown it. Only load-bearing reads can invalidate an answer, which
    -- keeps the sweep's false-positive rate down.
    load_bearing BOOL    NOT NULL DEFAULT false,

    CONSTRAINT pk_decision_read PRIMARY KEY (decision_id, fact_key, fact_version),
    CONSTRAINT fk_read_decision FOREIGN KEY (decision_id)
        REFERENCES decision (decision_id) ON DELETE CASCADE,
    CONSTRAINT fk_read_fact FOREIGN KEY (fact_key, fact_version)
        REFERENCES fact (fact_key, version)
);

-- Reverse edge: given a fact version that just got retracted, find every
-- decision that stood on it. This is the sweep's driving access path.
CREATE INDEX IF NOT EXISTS decision_read_by_fact
    ON decision_read (fact_key, fact_version)
    STORING (decision_id, load_bearing);

-- ===========================================================================
-- 4. SWEEPS :: retroactive invalidation, the flagship
-- ===========================================================================

CREATE TABLE IF NOT EXISTS sweep (
    sweep_id     UUID        NOT NULL DEFAULT gen_random_uuid(),
    trigger_kind STRING      NOT NULL,
    trigger_ref  STRING      NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ NULL,
    candidates   INT8        NOT NULL DEFAULT 0,
    reevaluated  INT8        NOT NULL DEFAULT 0,
    reversed     INT8        NOT NULL DEFAULT 0,
    state        STRING      NOT NULL DEFAULT 'running',

    CONSTRAINT pk_sweep PRIMARY KEY (sweep_id),
    CONSTRAINT ck_sweep_state CHECK (state IN ('running','done','failed'))
);

CREATE TABLE IF NOT EXISTS correction (
    correction_id UUID        NOT NULL DEFAULT gen_random_uuid(),
    sweep_id      UUID        NOT NULL,
    decision_id   UUID        NOT NULL,
    prior_verdict STRING      NOT NULL,
    new_verdict   STRING      NOT NULL,
    prior_answer  STRING      NOT NULL,
    new_answer    STRING      NOT NULL,
    changed       JSONB       NOT NULL DEFAULT '[]'::JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_correction PRIMARY KEY (correction_id),
    -- A sweep may be retried after a node or region loss. This makes the
    -- retry a no-op rather than a second correction.
    CONSTRAINT uq_correction_once UNIQUE (sweep_id, decision_id),
    CONSTRAINT fk_correction_sweep FOREIGN KEY (sweep_id)
        REFERENCES sweep (sweep_id) ON DELETE CASCADE,
    CONSTRAINT fk_correction_decision FOREIGN KEY (decision_id)
        REFERENCES decision (decision_id) ON DELETE CASCADE
);

-- ===========================================================================
-- 5. OUTBOX :: exactly-once patient notification
-- ===========================================================================
--
-- The notification row is written in the same transaction as the correction
-- that justifies it. Either both commit or neither does, so the system can
-- never notify a patient about a correction it did not durably record, and
-- can never record a correction it forgot to act on.
--
-- dedupe_key is a deterministic hash of (decision, new verdict, triggering
-- fact version). A sweep killed halfway through and restarted recomputes the
-- identical key, and the unique constraint turns the second write into a
-- no-op. That is what makes "zero duplicate notifications across a region
-- failure" a property of the schema rather than a hope about the code.

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id   UUID        NOT NULL DEFAULT gen_random_uuid(),
    dedupe_key  STRING      NOT NULL,
    channel     STRING      NOT NULL,
    recipient   STRING      NOT NULL,
    payload     JSONB       NOT NULL,
    state       STRING      NOT NULL DEFAULT 'pending',
    attempts    INT4        NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ NULL,
    last_error  STRING      NULL,

    CONSTRAINT pk_outbox PRIMARY KEY (outbox_id),
    CONSTRAINT uq_outbox_dedupe UNIQUE (dedupe_key),
    CONSTRAINT ck_outbox_state CHECK (state IN ('pending','sent','failed'))
);

CREATE INDEX IF NOT EXISTS outbox_pending
    ON outbox (created_at)
    WHERE state = 'pending';

-- ===========================================================================
-- 6. AUDIT :: append-only record of every memory mutation
-- ===========================================================================

CREATE TABLE IF NOT EXISTS memory_audit (
    audit_id   UUID        NOT NULL DEFAULT gen_random_uuid(),
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    hlc        DECIMAL     NOT NULL DEFAULT cluster_logical_timestamp(),
    actor      STRING      NOT NULL,
    action     STRING      NOT NULL,
    target     STRING      NOT NULL,
    detail     JSONB       NOT NULL DEFAULT '{}'::JSONB,

    CONSTRAINT pk_memory_audit PRIMARY KEY (audit_id)
);

CREATE INDEX IF NOT EXISTS memory_audit_time ON memory_audit (at DESC);
