"""Data quality checks for parsed trial records.

Two scopes, two severities:

* **Row checks** look at one record. ``error`` → the record is quarantined (not loaded).
  ``warning`` → the record is loaded but flagged in ``trials.dq_warnings``.
* **Dataset checks** look at a whole batch. They catch problems every row passes individually,
  e.g. the API silently renaming a field makes ``eligibility_criteria`` null for *every*
  record. A failed dataset ``error`` stops the run (circuit breaker) instead of loading junk.

Why hand-written checks rather than Great Expectations / pandera: ~10 rules over typed objects
are clearer, faster and easier to test as plain functions. Revisit if the rule count explodes.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from ctmatch.models import TrialRecord
from ctmatch.privacy.redact import REDACTED_FIELDS, contains_pii

NCT_ID_RE = re.compile(r"^NCT\d{8}$")

# Values observed in the API's overallStatus enum.
KNOWN_STATUSES = frozenset(
    {
        "ACTIVE_NOT_RECRUITING",
        "COMPLETED",
        "ENROLLING_BY_INVITATION",
        "NOT_YET_RECRUITING",
        "RECRUITING",
        "SUSPENDED",
        "TERMINATED",
        "WITHDRAWN",
        "AVAILABLE",
        "NO_LONGER_AVAILABLE",
        "TEMPORARILY_NOT_AVAILABLE",
        "APPROVED_FOR_MARKETING",
        "WITHHELD",
        "UNKNOWN",
    }
)
STOPPED_STATUSES = frozenset({"TERMINATED", "WITHDRAWN", "SUSPENDED"})

# Max allowed share of nulls per field in a batch before the dataset check fails.
DEFAULT_NULL_RATE_LIMITS: dict[str, float] = {
    "brief_title": 0.01,
    "overall_status": 0.01,
    "eligibility_criteria": 0.25,
    "start_date": 0.25,
}
SAMPLE_SIZE = 10


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Scope(StrEnum):
    ROW = "row"
    DATASET = "dataset"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check over a batch."""

    name: str
    severity: Severity
    scope: Scope
    passed: bool
    failed_ids: tuple[str, ...] = ()
    detail: str = ""

    @property
    def n_failed(self) -> int:
        return len(self.failed_ids)


@dataclass
class DQReport:
    """Everything the loader needs: which records to load, quarantine, and what was checked."""

    results: list[CheckResult]
    passed: list[TrialRecord]
    quarantined: dict[str, list[str]]  # nct_id -> failed error-check names
    warnings: dict[str, list[str]] = field(default_factory=dict)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        """Failed dataset-level errors: the batch must not be loaded."""
        return [
            r
            for r in self.results
            if not r.passed and r.scope is Scope.DATASET and r.severity is Severity.ERROR
        ]


@dataclass(frozen=True)
class RowCheck:
    """A named predicate over one record. ``fails(record)`` returns True when the record is bad."""

    name: str
    severity: Severity
    fails: Callable[[TrialRecord], bool]
    description: str


def _before(a: date | None, b: date | None) -> bool:
    return a is not None and b is not None and b < a


def _actual_in_future(r: TrialRecord, today: date) -> bool:
    # A start date in the future is fine for NOT_YET_RECRUITING, suspicious for COMPLETED.
    return r.overall_status == "COMPLETED" and r.start_date is not None and r.start_date > today


def default_row_checks(today: date | None = None) -> list[RowCheck]:
    """The standard row-level rule set."""
    ref = today or date.today()
    return [
        RowCheck(
            "nct_id_format",
            Severity.ERROR,
            lambda r: not NCT_ID_RE.match(r.nct_id),
            "NCT ID must look like NCT + 8 digits",
        ),
        RowCheck(
            "has_title",
            Severity.ERROR,
            lambda r: not r.brief_title,
            "Brief title is required (used for search and display)",
        ),
        RowCheck(
            "enrollment_non_negative",
            Severity.ERROR,
            lambda r: r.enrollment_count is not None and r.enrollment_count < 0,
            "Enrollment count cannot be negative",
        ),
        RowCheck(
            "age_range_valid",
            Severity.ERROR,
            lambda r: (
                r.minimum_age_years is not None
                and r.maximum_age_years is not None
                and r.minimum_age_years > r.maximum_age_years
            ),
            "Minimum age must not exceed maximum age",
        ),
        RowCheck(
            "no_residual_pii",
            Severity.ERROR,
            lambda r: any(contains_pii(getattr(r, f)) for f in REDACTED_FIELDS),
            "Redacted text fields must not still contain emails/phones (backstop)",
        ),
        RowCheck(
            "status_known",
            Severity.WARNING,
            lambda r: r.overall_status not in KNOWN_STATUSES,
            "Overall status not in known enum (possible API change)",
        ),
        RowCheck(
            "start_before_primary_completion",
            Severity.WARNING,
            lambda r: _before(r.start_date, r.primary_completion_date),
            "Primary completion date precedes start date",
        ),
        RowCheck(
            "stopped_has_reason",
            Severity.WARNING,
            lambda r: r.overall_status in STOPPED_STATUSES and not r.why_stopped,
            "Terminated/withdrawn/suspended trial without a stop reason",
        ),
        RowCheck(
            "completed_not_in_future",
            Severity.WARNING,
            lambda r: _actual_in_future(r, ref),
            "COMPLETED trial with a start date in the future",
        ),
    ]


def check_duplicates(records: Sequence[TrialRecord]) -> CheckResult:
    """Dataset check: NCT IDs must be unique within a batch."""
    counts = Counter(r.nct_id for r in records)
    dupes = tuple(sorted(k for k, v in counts.items() if v > 1))
    return CheckResult(
        "unique_nct_id",
        Severity.ERROR,
        Scope.DATASET,
        passed=not dupes,
        failed_ids=dupes,
        detail=f"{len(dupes)} duplicated NCT IDs" if dupes else "",
    )


def check_null_rates(
    records: Sequence[TrialRecord], limits: dict[str, float] | None = None
) -> list[CheckResult]:
    """Dataset check: share of missing values per field must stay under its limit."""
    limits = DEFAULT_NULL_RATE_LIMITS if limits is None else limits
    results: list[CheckResult] = []
    n = len(records)
    for field_name, limit in limits.items():
        if n == 0:
            results.append(
                CheckResult(f"null_rate:{field_name}", Severity.ERROR, Scope.DATASET, True)
            )
            continue
        missing = [r.nct_id for r in records if getattr(r, field_name) in (None, "")]
        rate = len(missing) / n
        results.append(
            CheckResult(
                f"null_rate:{field_name}",
                Severity.ERROR,
                Scope.DATASET,
                passed=rate <= limit,
                failed_ids=tuple(missing[:SAMPLE_SIZE]) if rate > limit else (),
                detail=f"null rate {rate:.1%} (limit {limit:.0%}, n={n})",
            )
        )
    return results


def run_checks(
    records: Sequence[TrialRecord],
    *,
    row_checks: Sequence[RowCheck] | None = None,
    null_rate_limits: dict[str, float] | None = None,
) -> DQReport:
    """Run all row and dataset checks and split records into load / quarantine."""
    checks = list(row_checks) if row_checks is not None else default_row_checks()
    quarantined: dict[str, list[str]] = {}
    warnings: dict[str, list[str]] = {}
    results: list[CheckResult] = []

    for check in checks:
        failed = [r.nct_id for r in records if check.fails(r)]
        bucket = quarantined if check.severity is Severity.ERROR else warnings
        for nct_id in failed:
            bucket.setdefault(nct_id, []).append(check.name)
        results.append(
            CheckResult(
                check.name,
                check.severity,
                Scope.ROW,
                passed=not failed,
                failed_ids=tuple(failed),
                detail=check.description if failed else "",
            )
        )

    results.append(check_duplicates(records))
    results.extend(check_null_rates(records, null_rate_limits))

    passed = [r for r in records if r.nct_id not in quarantined]
    return DQReport(results=results, passed=passed, quarantined=quarantined, warnings=warnings)
