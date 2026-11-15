from __future__ import annotations

from datetime import date
from typing import Any

from ctmatch.models import TrialRecord
from ctmatch.quality.checks import (
    Scope,
    Severity,
    check_duplicates,
    check_null_rates,
    run_checks,
)


def rec(nct_id: str = "NCT00000001", **kw: Any) -> TrialRecord:
    base: dict[str, Any] = {
        "brief_title": "A trial",
        "overall_status": "COMPLETED",
        "eligibility_criteria": "Adults",
        "start_date": date(2020, 1, 1),
    }
    return TrialRecord(nct_id=nct_id, **{**base, **kw})


def result_named(report: Any, name: str) -> Any:
    return next(r for r in report.results if r.name == name)


def test_clean_batch_passes_everything() -> None:
    report = run_checks([rec("NCT00000001"), rec("NCT00000002")])
    assert all(r.passed for r in report.results)
    assert len(report.passed) == 2
    assert report.quarantined == {}
    assert report.blocking_failures == []


def test_error_checks_quarantine_rows() -> None:
    bad = [
        rec("NCT123"),  # bad id
        rec("NCT00000002", brief_title=None),
        rec("NCT00000003", enrollment_count=-5),
        rec("NCT00000004", minimum_age_years=70.0, maximum_age_years=18.0),
        rec("NCT00000005", eligibility_criteria="email x@y.com"),  # residual PII
    ]
    good = [rec(f"NCT1000000{i}") for i in range(5)]
    report = run_checks(bad + good)
    assert set(report.quarantined) == {r.nct_id for r in bad}
    assert report.quarantined["NCT00000005"] == ["no_residual_pii"]
    assert len(report.passed) == 5


def test_warnings_flag_but_keep_rows() -> None:
    records = [
        rec("NCT00000001", overall_status="TERMINATED", why_stopped=None),
        rec("NCT00000002", primary_completion_date=date(2019, 1, 1)),  # before start
        rec("NCT00000003", overall_status="SOMETHING_NEW"),
    ]
    report = run_checks(records)
    assert len(report.passed) == 3
    assert report.warnings["NCT00000001"] == ["stopped_has_reason"]
    assert report.warnings["NCT00000002"] == ["start_before_primary_completion"]
    assert report.warnings["NCT00000003"] == ["status_known"]
    assert result_named(report, "status_known").severity is Severity.WARNING


def test_duplicates_are_blocking() -> None:
    result = check_duplicates([rec("NCT00000001"), rec("NCT00000001")])
    assert not result.passed and result.failed_ids == ("NCT00000001",)
    report = run_checks([rec("NCT00000001"), rec("NCT00000001")])
    assert [r.name for r in report.blocking_failures] == ["unique_nct_id"]


def test_null_rate_spike_blocks_batch() -> None:
    """Simulates an API field rename: every row is individually fine, the batch is not."""
    records = [rec(f"NCT0000000{i}", eligibility_criteria=None) for i in range(10)]
    report = run_checks(records)
    blocking = {r.name for r in report.blocking_failures}
    assert "null_rate:eligibility_criteria" in blocking
    assert all(r.scope is Scope.DATASET for r in report.blocking_failures)


def test_null_rate_under_limit_passes() -> None:
    records = [rec(f"NCT0000000{i}") for i in range(9)] + [rec("NCT00000099", start_date=None)]
    (result,) = check_null_rates(records, {"start_date": 0.25})
    assert result.passed
    assert "10.0%" in result.detail


def test_empty_batch_does_not_crash() -> None:
    report = run_checks([])
    assert report.passed == [] and report.blocking_failures == []
