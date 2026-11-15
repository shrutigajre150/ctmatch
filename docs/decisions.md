# Design decisions (v0.1)

Short records of each choice: what, why, what else was considered. These double as interview answers.

## 1. Bronze/silver layering: `raw_studies` → `trials`
**Decision.** Store the (contact-scrubbed) API payload as JSONB, then a typed, cleaned table.
**Why.** Parsing logic will change (new fields for v0.2 features). With raw payloads stored I can
re-parse 50k+ trials in seconds instead of re-hitting a rate-limited API for an hour.
**Alternatives.** Only typed table (simpler, but every parser change means re-downloading);
files in S3/Parquet (sensible at larger scale; unnecessary here).

## 2. Idempotent upserts guarded by `last_update_date`
**Decision.** `INSERT … ON CONFLICT DO UPDATE … WHERE excluded.last_update_date >= trials.last_update_date`.
**Why.** Retries (Prefect task retries, re-running a failed flow) must never duplicate rows, and
an older replayed page must never overwrite fresher data.

## 3. Incremental loads with a watermark + 1-day overlap
**Decision.** Each successful run stores the max `last_update_date` it loaded. The next run fetches
`LastUpdatePostDate >= watermark − 1 day`.
**Why.** The overlap costs a few duplicate fetches but protects against records posted late on the
watermark day; idempotent upserts make the overlap free of side effects. Failed runs never advance
the watermark.

## 4. Data quality: row vs dataset checks, error vs warning
**Decision.** Row errors → quarantine table; row warnings → load + flag; dataset errors → stop the run.
**Why.** Real registry data is messy (e.g. completion before start happens), so dropping every
odd row would bias analyses. But a *systematic* problem (an API field renamed → 100% nulls) must
stop the pipeline rather than silently load junk. Every check result is stored per run, so DQ
history can be charted.
**Alternatives.** Great Expectations / pandera — capable, but ~10 rules over typed objects are
clearer as plain tested functions.

## 5. PII: minimize first, redact second
**Decision.** Delete contact blocks (names, emails, phones of investigators) before storage; regex
redaction of free text; a DQ backstop that quarantines any record still containing an email/phone.
**Why.** ClinicalTrials.gov is public and contains no patient data, but it does name real people,
and we never need them. The same `redact_text` guard will sit in front of every LLM call (v0.4).
**Limitation.** Regex catches structured PII and titled names ("Dr. X") only. Bare names are
missed; NER (e.g. Presidio) is the upgrade path for patient-style text.

## 6. Fetch TREC-judged trials by ID
**Decision.** Load TREC qrels first, then fetch exactly those NCT IDs via `filter.ids`.
**Why.** TREC 2021/2022 judgments refer to an April 2021 snapshot. Evaluation is only meaningful if
every judged trial is in our index. Today's records may differ from the 2021 versions (or be gone);
the flow reports coverage, and this is listed as a benchmark limitation.

## 7. Plain SQL + idempotent schema file, no ORM/Alembic yet
**Why.** Small schema, bulk upserts. Alembic gets added when the first non-additive schema change
appears.

## 8. Pure core, thin Prefect layer
**Decision.** `pipeline.process_page` (scrub → parse → redact → check) has no Prefect/DB imports.
**Why.** Unit-testable in milliseconds; Prefect only adds retries, logging, scheduling.
**Alternatives.** Airflow (heavier to run locally), cron + scripts (no retries/observability),
Dagster (good, asset-oriented; Prefect is lighter for one pipeline).
