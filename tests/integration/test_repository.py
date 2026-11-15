"""Integration tests against a real Postgres (pgvector image). Run with ``pytest -m integration``.

Requires ``CTMATCH_DATABASE_URL`` pointing at a disposable database: tables are truncated.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg")

from ctmatch.benchmarks.trec import Qrel, Topic  # noqa: E402
from ctmatch.db import repository as repo  # noqa: E402
from ctmatch.pipeline import process_page  # noqa: E402

pytestmark = pytest.mark.integration

DSN = os.environ.get("CTMATCH_DATABASE_URL")
TABLES = "quarantine, dq_results, trials, raw_studies, trec_qrels, trec_topics, pipeline_runs"


@pytest.fixture
def conn() -> Iterator[Any]:
    if not DSN:
        pytest.skip("CTMATCH_DATABASE_URL not set")
    with repo.connect(DSN) as c:
        repo.apply_schema(c)
        repo.apply_schema(c)  # idempotent: second run must not fail
        c.execute(f"TRUNCATE {TABLES} CASCADE")
        c.commit()
        yield c


def _load(conn: Any, raws: list[dict[str, Any]]) -> Any:
    run_id = repo.start_run(conn, "test", {})
    result = process_page(raws)
    repo.upsert_raw(conn, result.scrubbed_raw, run_id)
    repo.upsert_trials(conn, result.report.passed, result.report.warnings, run_id)
    repo.insert_quarantine(conn, result.report.quarantined, run_id)
    repo.insert_dq_results(conn, result.report.results, run_id)
    return run_id


def test_load_is_idempotent(conn: Any, raw_study: dict[str, Any], make_raw: Any) -> None:
    raws = [raw_study, make_raw("NCT00000002")]
    _load(conn, raws)
    _load(conn, raws)
    assert conn.execute("SELECT count(*) FROM trials").fetchone()[0] == 2
    row = conn.execute(
        "SELECT phases, countries, redaction_counts, start_date_precision "
        "FROM trials WHERE nct_id = 'NCT01234567'"
    ).fetchone()
    assert row[0] == ["PHASE2"]
    assert row[1] == ["Canada", "United States"]
    assert row[2] == {"name": 1, "email": 1, "phone": 1}
    assert row[3] == "month"


def test_older_update_does_not_overwrite_newer(conn: Any, make_raw: Any) -> None:
    newer = make_raw("NCT00000003", lastUpdatePostDateStruct={"date": "2024-05-01"})
    newer["protocolSection"]["identificationModule"]["briefTitle"] = "New title"
    older = make_raw("NCT00000003", lastUpdatePostDateStruct={"date": "2023-01-01"})
    older["protocolSection"]["identificationModule"]["briefTitle"] = "Old title"
    _load(conn, [newer])
    _load(conn, [older])
    title = conn.execute("SELECT brief_title FROM trials WHERE nct_id='NCT00000003'").fetchone()
    assert title[0] == "New title"


def test_quarantine_and_dq_results_written(conn: Any, make_raw: Any) -> None:
    bad = make_raw("NCT00000004")
    bad["protocolSection"]["identificationModule"]["briefTitle"] = ""
    run_id = _load(conn, [bad, make_raw("NCT00000005")])
    q = conn.execute("SELECT nct_id, reasons FROM quarantine WHERE run_id=%s", (run_id,)).fetchall()
    assert q == [("NCT00000004", ["has_title"])]
    n_checks = conn.execute("SELECT count(*) FROM dq_results WHERE run_id=%s", (run_id,)).fetchone()
    assert n_checks[0] > 5


def test_watermark_only_from_successful_runs(conn: Any) -> None:
    from datetime import date

    ok = repo.start_run(conn, "ingest_trials", {})
    repo.finish_run(conn, ok, status="success", watermark=date(2026, 11, 1))
    bad = repo.start_run(conn, "ingest_trials", {})
    repo.finish_run(conn, bad, status="failed", watermark=date(2026, 11, 10))
    assert repo.get_watermark(conn, "ingest_trials") == date(2026, 11, 1)


def test_trec_roundtrip(conn: Any) -> None:
    topics = [Topic(2021, "1", "a patient"), Topic(2021, "2", "another")]
    qrels = [Qrel(2021, "1", "NCT00000001", 2), Qrel(2021, "2", "NCT00000001", 0)]
    repo.upsert_trec(conn, topics, qrels)
    repo.upsert_trec(conn, topics, [replace(qrels[0], relevance=1)])
    assert repo.trec_judged_nct_ids(conn) == ["NCT00000001"]
    rel = conn.execute("SELECT relevance FROM trec_qrels WHERE topic_id='1'").fetchone()
    assert rel[0] == 1
