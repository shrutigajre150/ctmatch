"""Prefect flow: load TREC Clinical Trials topics and qrels into Postgres.

Expects files named as on the NIST site (see ``scripts/download_trec.sh``)::

    data/trec/topics2021.xml  data/trec/qrels2021.txt
    data/trec/topics2022.xml  data/trec/qrels2022.txt
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger

from ctmatch.benchmarks.trec import parse_qrels_file, parse_topics, summarize_qrels
from ctmatch.config import Settings
from ctmatch.db import repository as repo

YEARS = (2021, 2022)


@flow(name="load-trec")
def load_trec(data_dir: str = "data/trec", years: tuple[int, ...] = YEARS) -> dict[str, Any]:
    """Parse and upsert topics + qrels for each year. Fails if a judged topic has no text."""
    log = get_run_logger()
    settings = Settings.from_env()
    root = Path(data_dir)
    summary: dict[str, Any] = {}

    with repo.connect(settings.database_url) as conn:
        repo.apply_schema(conn)
        for year in years:
            topics = parse_topics(root / f"topics{year}.xml", year)
            qrels = parse_qrels_file(root / f"qrels{year}.txt", year)
            orphan = {q.topic_id for q in qrels} - {t.topic_id for t in topics}
            if orphan:
                raise ValueError(f"{year}: qrels reference unknown topics {sorted(orphan)[:5]}")
            repo.upsert_trec(conn, topics, qrels)
            summary[str(year)] = summarize_qrels(qrels)
            log.info("TREC %d: %d topics, %s", year, len(topics), summary[str(year)])
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", default="data/trec")
    args = parser.parse_args(argv)
    load_trec(data_dir=args.data_dir)


if __name__ == "__main__":
    main()
