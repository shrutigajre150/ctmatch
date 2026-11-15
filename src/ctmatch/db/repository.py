"""Postgres reads and writes (psycopg 3, plain SQL).

Design choices:

* **Idempotent upserts.** Re-running a load never duplicates rows. A trial is only overwritten
  when the incoming ``last_update_date`` is the same or newer, so an old page replayed out of
  order cannot clobber fresher data.
* **Plain SQL, no ORM.** The schema is small and the queries are bulk upserts; SQL is clearer
  and easier to tune. ``schema.sql`` is idempotent and applied at flow start, which is
  enough until the schema needs real migrations (then: Alembic).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ctmatch.benchmarks.trec import Qrel, Topic
from ctmatch.models import TrialRecord
from ctmatch.quality.checks import CheckResult

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

TRIAL_COLUMNS = (
    "nct_id",
    "brief_title",
    "official_title",
    "brief_summary",
    "overall_status",
    "why_stopped",
    "study_type",
    "phases",
    "lead_sponsor_name",
    "lead_sponsor_class",
    "enrollment_count",
    "enrollment_type",
    "start_date",
    "start_date_precision",
    "primary_completion_date",
    "primary_completion_precision",
    "completion_date",
    "last_update_date",
    "conditions",
    "keywords",
    "eligibility_criteria",
    "sex",
    "minimum_age_years",
    "maximum_age_years",
    "healthy_volunteers",
    "n_locations",
    "countries",
    "redaction_counts",
    "dq_warnings",
    "run_id",
)


def connect(dsn: str) -> psycopg.Connection[Any]:
    """Open a connection. Callers use it as a context manager (commits on success)."""
    return psycopg.connect(dsn)


def apply_schema(conn: psycopg.Connection[Any], path: Path = SCHEMA_PATH) -> None:
    """Create tables/extensions if missing (the SQL file is idempotent)."""
    conn.execute(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- pipeline runs


def start_run(conn: psycopg.Connection[Any], flow_name: str, params: dict[str, Any]) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, flow_name, status, params) "
        "VALUES (%s, %s, 'running', %s)",
        (run_id, flow_name, Jsonb(json.loads(json.dumps(params, default=str)))),
    )
    return run_id


def finish_run(
    conn: psycopg.Connection[Any],
    run_id: uuid.UUID,
    *,
    status: str,
    n_fetched: int = 0,
    n_loaded: int = 0,
    n_quarantined: int = 0,
    watermark: date | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE pipeline_runs
           SET status = %s, finished_at = now(), n_fetched = %s, n_loaded = %s,
               n_quarantined = %s, watermark = %s, error = %s
         WHERE run_id = %s
        """,
        (status, n_fetched, n_loaded, n_quarantined, watermark, error, run_id),
    )


def get_watermark(conn: psycopg.Connection[Any], flow_name: str) -> date | None:
    """Latest watermark from a *successful* run of ``flow_name`` (``None`` on first run)."""
    row = conn.execute(
        "SELECT max(watermark) FROM pipeline_runs WHERE flow_name = %s AND status = 'success'",
        (flow_name,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- trials


def upsert_raw(
    conn: psycopg.Connection[Any], studies: Sequence[dict[str, Any]], run_id: uuid.UUID
) -> None:
    rows = []
    for s in studies:
        status = s.get("protocolSection", {}).get("statusModule", {})
        last = status.get("lastUpdatePostDateStruct", {}).get("date")
        nct = s["protocolSection"]["identificationModule"]["nctId"]
        rows.append((nct, Jsonb(s), last if last and len(last) == 10 else None, run_id))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO raw_studies (nct_id, payload, last_update_date, run_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (nct_id) DO UPDATE
               SET payload = EXCLUDED.payload, last_update_date = EXCLUDED.last_update_date,
                   fetched_at = now(), run_id = EXCLUDED.run_id
            """,
            rows,
        )


def _trial_row(r: TrialRecord, warnings: Sequence[str], run_id: uuid.UUID) -> tuple[Any, ...]:
    data = r.model_dump()
    data["phases"] = list(r.phases)
    data["conditions"] = list(r.conditions)
    data["keywords"] = list(r.keywords)
    data["countries"] = list(r.countries)
    data["redaction_counts"] = Jsonb(r.redaction_counts)
    data["dq_warnings"] = list(warnings)
    data["run_id"] = run_id
    for key in ("start_date_precision", "primary_completion_precision"):
        data[key] = data[key].value if data[key] is not None else None
    return tuple(data[c] for c in TRIAL_COLUMNS)


def upsert_trials(
    conn: psycopg.Connection[Any],
    records: Sequence[TrialRecord],
    warnings: dict[str, list[str]],
    run_id: uuid.UUID,
) -> int:
    """Insert or update trials; returns the number of input records."""
    cols = ", ".join(TRIAL_COLUMNS)
    placeholders = ", ".join(["%s"] * len(TRIAL_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in TRIAL_COLUMNS if c != "nct_id")
    sql = f"""
        INSERT INTO trials ({cols}) VALUES ({placeholders})
        ON CONFLICT (nct_id) DO UPDATE SET {updates}, loaded_at = now()
        WHERE trials.last_update_date IS NULL
           OR EXCLUDED.last_update_date IS NULL
           OR EXCLUDED.last_update_date >= trials.last_update_date
    """
    rows = [_trial_row(r, warnings.get(r.nct_id, []), run_id) for r in records]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def insert_quarantine(
    conn: psycopg.Connection[Any], quarantined: dict[str, list[str]], run_id: uuid.UUID
) -> None:
    if not quarantined:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO quarantine (run_id, nct_id, reasons) VALUES (%s, %s, %s)",
            [(run_id, nct, reasons) for nct, reasons in quarantined.items()],
        )


def insert_dq_results(
    conn: psycopg.Connection[Any], results: Iterable[CheckResult], run_id: uuid.UUID
) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO dq_results
                (run_id, check_name, severity, scope, passed, n_failed, sample_ids, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    run_id,
                    r.name,
                    r.severity.value,
                    r.scope.value,
                    r.passed,
                    r.n_failed,
                    list(r.failed_ids[:10]),
                    r.detail,
                )
                for r in results
            ],
        )


def existing_nct_ids(conn: psycopg.Connection[Any], nct_ids: Sequence[str]) -> set[str]:
    rows = conn.execute(
        "SELECT nct_id FROM trials WHERE nct_id = ANY(%s)", (list(nct_ids),)
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------- TREC


def upsert_trec(
    conn: psycopg.Connection[Any], topics: Sequence[Topic], qrels: Sequence[Qrel]
) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO trec_topics (year, topic_id, text) VALUES (%(year)s, %(topic_id)s, "
            "%(text)s) ON CONFLICT (year, topic_id) DO UPDATE SET text = EXCLUDED.text",
            [asdict(t) for t in topics],
        )
        cur.executemany(
            "INSERT INTO trec_qrels (year, topic_id, nct_id, relevance) VALUES (%(year)s, "
            "%(topic_id)s, %(nct_id)s, %(relevance)s) ON CONFLICT (year, topic_id, nct_id) "
            "DO UPDATE SET relevance = EXCLUDED.relevance",
            [asdict(q) for q in qrels],
        )


def trec_judged_nct_ids(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute("SELECT DISTINCT nct_id FROM trec_qrels ORDER BY nct_id").fetchall()
    return [row[0] for row in rows]
