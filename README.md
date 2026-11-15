# Clinical Trial Matching & Intelligence Platform

Matches patients to clinical trials, explains eligibility criterion by criterion, and analyzes the
trial landscape (early termination, enrollment speed). Evaluated on the public TREC Clinical Trials
benchmark.

> **Status: v0.1 (Foundation) in progress.** Data platform: ingestion, data quality, PII handling,
> tests, CI. Results tables will be filled in with measured numbers as each version lands.

## Architecture (v0.1)

```
ClinicalTrials.gov API v2 ──► Prefect flow ──────────────────────────────────────► Postgres + pgvector
  (paginated, rate-limited,    scrub contacts → parse → redact free text → DQ checks   raw_studies (JSONB)
   retried; by query, by        │                                                   trials (typed)
   NCT ID list, or incremental) └─ error rows → quarantine; batch-level errors stop  quarantine, dq_results
                                                                                     pipeline_runs (audit, watermark)
TREC CT 2021/2022 topics + qrels ──► load_trec flow ──────────────────────────────► trec_topics, trec_qrels
```

## Quickstart

```bash
make install                 # pip install -e ".[dev]"
cp .env.example .env
make up                      # Postgres 16 + pgvector in Docker
make ingest-sample           # 2 pages of type 2 diabetes trials (smoke test)

make trec-download           # topics + qrels from NIST into data/trec/
make trec-load               # load benchmark into Postgres
make ingest-trec             # fetch every TREC-judged trial by NCT ID, reports coverage

make ci                      # ruff + mypy + unit tests
make test-integration        # needs Postgres running
```

## Repository layout

```
src/ctmatch/
  ingest/client.py     CT.gov API client: pagination, retries/backoff, rate limit, ID batches
  ingest/parse.py      nested JSON → typed TrialRecord (dates with precision, ages in years)
  privacy/redact.py    contact scrubbing + free-text PII redaction
  quality/checks.py    row/dataset DQ checks, quarantine, circuit breaker
  pipeline.py          pure page transform (no Prefect/DB) — unit tested
  db/schema.sql        idempotent schema
  db/repository.py     idempotent upserts, run tracking, watermark
  benchmarks/trec.py   TREC topics/qrels parsers
  flows/               Prefect flows (thin orchestration)
tests/unit             fast tests, no network or DB
tests/integration      real Postgres (CI service container)
docs/decisions.md      design decisions and tradeoffs
```

## Data and privacy

- **ClinicalTrials.gov** (public domain registry). No patient data. Investigator/contact names,
  emails and phones are removed before storage; free text is redacted (`[EMAIL]`, `[PHONE]`, `[NAME]`).
- **TREC Clinical Trials 2021/2022**: synthetic patient cases written by people with medical
  training, plus expert relevance judgments (0 not relevant, 1 excluded, 2 eligible).
- No real patient data or employer data is used anywhere in this project.

## Known limitations (v0.1)

- PII redaction is regex-based: catches emails, phone numbers, and titled names ("Dr. X"), not bare names.
- TREC judgments refer to an April 2021 snapshot; trials are fetched from today's API, so some
  records have changed or been removed since. Coverage is reported by `ingest-trec`.
- Month-only dates are imputed to the 1st of the month (precision is stored per date).
- TREC 2023 (different topic format) is not loaded yet.

## Roadmap

| Version | Scope |
|---|---|
| v0.1 | Data platform (this) |
| v0.2 | Termination prediction (SHAP, calibration), survival analysis of enrollment, causal analysis, Streamlit |
| v0.3 | Hybrid retrieval (BM25 + embeddings) + LLM reranking, TREC NDCG@10 / P@k |
| v0.4 | Eligibility extraction to JSON, coordinator agent, MCP server |
| v1.0 | FastAPI, AWS, Langfuse, drift monitoring, version comparison, demo |
