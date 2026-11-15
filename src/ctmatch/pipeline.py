"""Pure transformation steps for one page of studies: scrub → parse → redact → check.

Kept free of Prefect and the database so it can be unit-tested directly. The Prefect flow
(:mod:`ctmatch.flows.ingest_trials`) is a thin orchestration layer on top.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ctmatch.ingest.parse import parse_study
from ctmatch.models import TrialRecord
from ctmatch.privacy.redact import REDACTED_FIELDS, redact_text, scrub_raw_study
from ctmatch.quality.checks import DQReport, run_checks

logger = logging.getLogger(__name__)


@dataclass
class PageResult:
    """Output of processing one API page."""

    scrubbed_raw: list[dict[str, Any]]
    report: DQReport
    parse_failures: list[str] = field(default_factory=list)
    redaction_totals: Counter[str] = field(default_factory=Counter)


def redact_record(record: TrialRecord) -> TrialRecord:
    """Return a copy of ``record`` with free-text fields redacted and counts attached."""
    updates: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    for name in REDACTED_FIELDS:
        value = getattr(record, name)
        if value:
            result = redact_text(value)
            if result.total:
                updates[name] = result.text
                counts.update(result.counts)
    updates["redaction_counts"] = dict(counts)
    return record.model_copy(update=updates)


def process_page(raw_studies: list[dict[str, Any]]) -> PageResult:
    """Scrub contacts, parse, redact and quality-check one page of raw studies."""
    scrubbed: list[dict[str, Any]] = []
    records: list[TrialRecord] = []
    failures: list[str] = []
    totals: Counter[str] = Counter()

    for raw in raw_studies:
        clean = scrub_raw_study(raw)
        try:
            record = redact_record(parse_study(clean))
        except (ValueError, TypeError) as exc:
            # Parsing failure = malformed payload. Count it, keep going.
            ident = str(clean.get("protocolSection", {}).get("identificationModule", {}))
            failures.append(f"{exc} :: {ident[:120]}")
            logger.warning("Failed to parse study: %s", exc)
            continue
        scrubbed.append(clean)
        records.append(record)
        totals.update(record.redaction_counts)

    report = run_checks(records)
    return PageResult(
        scrubbed_raw=scrubbed, report=report, parse_failures=failures, redaction_totals=totals
    )
