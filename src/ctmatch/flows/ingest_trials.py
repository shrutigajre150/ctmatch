"""Prefect flow: ClinicalTrials.gov → scrub/parse/redact/check → Postgres.

Three ways to choose what to fetch:

* ``--query "type 2 diabetes"``  condition search (sampling, smoke tests)
* ``--trec-judged``              exactly the trials judged in the TREC benchmark (load TREC first)
* ``--incremental``              everything updated since the last successful run's watermark

Run locally without a Prefect server: ``python -m ctmatch.flows.ingest_trials --help``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from prefect import flow, get_run_logger, task

from ctmatch.config import Settings
from ctmatch.db import repository as repo
from ctmatch.ingest.client import CTGovClient
from ctmatch.pipeline import process_page

FLOW_NAME = "ingest_trials"
# Re-fetch one day before the watermark: overlap is harmless (upserts are idempotent) and
# protects against records posted late on the watermark day.
WATERMARK_OVERLAP = timedelta(days=1)


class DataQualityError(RuntimeError):
    """A dataset-level DQ error: the batch looks systematically broken, so stop the run."""


@dataclass
class PageStats:
    n_fetched: int
    n_loaded: int
    n_quarantined: int
    n_parse_failures: int
    max_last_update: date | None
    redactions: dict[str, int]


@task(retries=2, retry_delay_seconds=30)
def process_and_load_page(dsn: str, raw_page: list[dict[str, Any]], run_id: UUID) -> PageStats:
    """Transform one page and write it in a single transaction (all-or-nothing per page)."""
    result = process_page(raw_page)
    report = result.report
    with repo.connect(dsn) as conn:
        repo.insert_dq_results(conn, report.results, run_id)
        if report.blocking_failures:
            conn.commit()  # keep the DQ evidence even though we stop
            names = ", ".join(f"{r.name} ({r.detail})" for r in report.blocking_failures)
            raise DataQualityError(f"Dataset-level DQ failure: {names}")
        repo.upsert_raw(conn, result.scrubbed_raw, run_id)
        loaded = repo.upsert_trials(conn, report.passed, report.warnings, run_id)
        repo.insert_quarantine(conn, report.quarantined, run_id)
    dates = [r.last_update_date for r in report.passed if r.last_update_date]
    return PageStats(
        n_fetched=len(raw_page),
        n_loaded=loaded,
        n_quarantined=len(report.quarantined),
        n_parse_failures=len(result.parse_failures),
        max_last_update=max(dates) if dates else None,
        redactions=dict(result.redaction_totals),
    )


@flow(name="ingest-trials", log_prints=True)
def ingest_trials(
    query: str | None = None,
    trec_judged: bool = False,
    incremental: bool = False,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Fetch trials from ClinicalTrials.gov and load them into Postgres."""
    if sum([bool(query), trec_judged, incremental]) != 1:
        raise ValueError("Choose exactly one of: query, trec_judged, incremental")

    log = get_run_logger()
    settings = Settings.from_env()
    client = CTGovClient.from_settings(settings)
    params = {
        "query": query,
        "trec_judged": trec_judged,
        "incremental": incremental,
        "max_pages": max_pages,
    }

    with repo.connect(settings.database_url) as conn:
        repo.apply_schema(conn)
        run_id = repo.start_run(conn, FLOW_NAME, params)
        nct_ids = repo.trec_judged_nct_ids(conn) if trec_judged else None
        watermark = repo.get_watermark(conn, FLOW_NAME) if incremental else None

    if trec_judged and not nct_ids:
        raise RuntimeError("No TREC judgments in the database. Run load_trec first.")
    updated_since = watermark - WATERMARK_OVERLAP if watermark else None
    log.info("Run %s: params=%s updated_since=%s", run_id, params, updated_since)

    totals = {"n_fetched": 0, "n_loaded": 0, "n_quarantined": 0, "n_parse_failures": 0}
    redactions: dict[str, int] = {}
    max_update: date | None = None
    try:
        pages = client.iter_pages(
            query_cond=query, nct_ids=nct_ids, updated_since=updated_since, max_pages=max_pages
        )
        for i, page in enumerate(pages, start=1):
            stats = process_and_load_page(settings.database_url, page, run_id)
            for key in totals:
                totals[key] += getattr(stats, key)
            for k, v in stats.redactions.items():
                redactions[k] = redactions.get(k, 0) + v
            if stats.max_last_update and (max_update is None or stats.max_last_update > max_update):
                max_update = stats.max_last_update
            log.info("Page %d: %s", i, stats)
    except Exception as exc:
        with repo.connect(settings.database_url) as conn:
            repo.finish_run(
                conn,
                run_id,
                status="failed",
                error=str(exc)[:2000],
                n_fetched=totals["n_fetched"],
                n_loaded=totals["n_loaded"],
                n_quarantined=totals["n_quarantined"],
            )
        raise

    summary: dict[str, Any] = {"run_id": str(run_id), **totals, "redactions": redactions}
    if nct_ids:
        with repo.connect(settings.database_url) as conn:
            found = repo.existing_nct_ids(conn, nct_ids)
        missing = sorted(set(nct_ids) - found)
        summary["trec_judged"] = len(nct_ids)
        summary["trec_missing"] = len(missing)
        summary["trec_coverage"] = round(len(found) / len(nct_ids), 4)
        log.info("TREC coverage: %d/%d judged trials loaded", len(found), len(nct_ids))

    with repo.connect(settings.database_url) as conn:
        repo.finish_run(
            conn,
            run_id,
            status="success",
            watermark=max_update or watermark,
            n_fetched=totals["n_fetched"],
            n_loaded=totals["n_loaded"],
            n_quarantined=totals["n_quarantined"],
        )
    log.info("Done: %s", json.dumps(summary))
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--query", help="Condition search, e.g. 'type 2 diabetes'")
    mode.add_argument("--trec-judged", action="store_true", help="Fetch TREC-judged trials")
    mode.add_argument("--incremental", action="store_true", help="Fetch updates since last run")
    parser.add_argument("--max-pages", type=int, default=None, help="Stop after N pages")
    args = parser.parse_args(argv)
    ingest_trials(
        query=args.query,
        trec_judged=args.trec_judged,
        incremental=args.incremental,
        max_pages=args.max_pages,
    )


if __name__ == "__main__":
    main()
