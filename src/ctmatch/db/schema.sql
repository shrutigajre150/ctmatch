-- ctmatch schema v0.1 (src/ctmatch/db/schema.sql). Idempotent: safe to run on every pipeline start.
-- Layering: raw_studies (bronze: API payload, contacts scrubbed) -> trials (silver: typed, redacted, checked).

CREATE EXTENSION IF NOT EXISTS vector;  -- used from v0.3 (embeddings)

-- One row per pipeline run: the audit trail and the incremental-load watermark.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id         UUID PRIMARY KEY,
    flow_name      TEXT        NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    status         TEXT        NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    params         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    watermark      DATE,       -- max last_update_date successfully loaded in this run
    n_fetched      INTEGER     NOT NULL DEFAULT 0,
    n_loaded       INTEGER     NOT NULL DEFAULT 0,
    n_quarantined  INTEGER     NOT NULL DEFAULT 0,
    error          TEXT
);

-- Raw API payload (after contact scrubbing). Lets us re-parse without re-calling the API.
CREATE TABLE IF NOT EXISTS raw_studies (
    nct_id            TEXT PRIMARY KEY,
    payload           JSONB       NOT NULL,
    last_update_date  DATE,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id            UUID REFERENCES pipeline_runs (run_id)
);

-- Cleaned, typed trial records: what analytics and search read from.
CREATE TABLE IF NOT EXISTS trials (
    nct_id                     TEXT PRIMARY KEY,
    brief_title                TEXT NOT NULL,
    official_title             TEXT,
    brief_summary              TEXT,
    overall_status             TEXT,
    why_stopped                TEXT,
    study_type                 TEXT,
    phases                     TEXT[] NOT NULL DEFAULT '{}',
    lead_sponsor_name          TEXT,
    lead_sponsor_class         TEXT,
    enrollment_count           INTEGER,
    enrollment_type            TEXT,
    start_date                 DATE,
    start_date_precision       TEXT,
    primary_completion_date    DATE,
    primary_completion_precision TEXT,
    completion_date            DATE,
    last_update_date           DATE,
    conditions                 TEXT[] NOT NULL DEFAULT '{}',
    keywords                   TEXT[] NOT NULL DEFAULT '{}',
    eligibility_criteria       TEXT,
    sex                        TEXT,
    minimum_age_years          DOUBLE PRECISION,
    maximum_age_years          DOUBLE PRECISION,
    healthy_volunteers         BOOLEAN,
    n_locations                INTEGER NOT NULL DEFAULT 0,
    countries                  TEXT[] NOT NULL DEFAULT '{}',
    redaction_counts           JSONB NOT NULL DEFAULT '{}'::jsonb,
    dq_warnings                TEXT[] NOT NULL DEFAULT '{}',
    loaded_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id                     UUID REFERENCES pipeline_runs (run_id)
);
CREATE INDEX IF NOT EXISTS idx_trials_status      ON trials (overall_status);
CREATE INDEX IF NOT EXISTS idx_trials_last_update ON trials (last_update_date);
CREATE INDEX IF NOT EXISTS idx_trials_conditions  ON trials USING GIN (conditions);

-- Results of every data quality check in every run (pass or fail): a DQ history you can chart.
CREATE TABLE IF NOT EXISTS dq_results (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID REFERENCES pipeline_runs (run_id),
    check_name  TEXT    NOT NULL,
    severity    TEXT    NOT NULL CHECK (severity IN ('error', 'warning')),
    scope       TEXT    NOT NULL CHECK (scope IN ('row', 'dataset')),
    passed      BOOLEAN NOT NULL,
    n_failed    INTEGER NOT NULL DEFAULT 0,
    sample_ids  TEXT[]  NOT NULL DEFAULT '{}',
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Records that failed an error-level check. Kept for inspection, never loaded into trials.
CREATE TABLE IF NOT EXISTS quarantine (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID REFERENCES pipeline_runs (run_id),
    nct_id      TEXT   NOT NULL,
    reasons     TEXT[] NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TREC Clinical Trials benchmark (2021, 2022): synthetic patient topics + expert judgments.
CREATE TABLE IF NOT EXISTS trec_topics (
    year      SMALLINT NOT NULL,
    topic_id  TEXT     NOT NULL,
    text      TEXT     NOT NULL,
    PRIMARY KEY (year, topic_id)
);

CREATE TABLE IF NOT EXISTS trec_qrels (
    year       SMALLINT NOT NULL,
    topic_id   TEXT     NOT NULL,
    nct_id     TEXT     NOT NULL,
    relevance  SMALLINT NOT NULL CHECK (relevance IN (0, 1, 2)),  -- 0 not relevant, 1 excluded, 2 eligible
    PRIMARY KEY (year, topic_id, nct_id)
);
CREATE INDEX IF NOT EXISTS idx_qrels_nct ON trec_qrels (nct_id);
