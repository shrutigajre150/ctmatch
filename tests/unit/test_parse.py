from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from ctmatch.ingest.parse import get_path, parse_age_years, parse_ct_date, parse_study
from ctmatch.models import DatePrecision


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2020-11-15", (date(2020, 11, 15), DatePrecision.DAY)),
        ("2019-03", (date(2019, 3, 1), DatePrecision.MONTH)),
        ("2018", (date(2018, 1, 1), DatePrecision.YEAR)),
        ("2019-13", (None, None)),
        ("March 2019", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_ct_date(raw: str | None, expected: tuple[Any, Any]) -> None:
    assert parse_ct_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "years"),
    [
        ("18 Years", 18.0),
        ("6 Months", 0.5),
        ("1 Year", 1.0),
        ("30 Days", round(30 / 365.25, 4)),
        ("N/A", None),
        (None, None),
    ],
)
def test_parse_age_years(raw: str | None, years: float | None) -> None:
    assert parse_age_years(raw) == years


def test_get_path_handles_missing_and_non_dict() -> None:
    data = {"a": {"b": {"c": 1}}, "x": [1, 2]}
    assert get_path(data, "a.b.c") == 1
    assert get_path(data, "a.z.c") is None
    assert get_path(data, "x.y", default="d") == "d"


def test_parse_study_full_fixture(raw_study: dict[str, Any]) -> None:
    rec = parse_study(raw_study)
    assert rec.nct_id == "NCT01234567"
    assert rec.overall_status == "TERMINATED"
    assert rec.phases == ("PHASE2",)
    assert rec.lead_sponsor_class == "INDUSTRY"
    assert rec.enrollment_count == 120
    assert rec.start_date == date(2019, 3, 1)
    assert rec.start_date_precision is DatePrecision.MONTH
    assert rec.primary_completion_date == date(2020, 11, 15)
    assert rec.minimum_age_years == 18.0 and rec.maximum_age_years == 75.0
    assert rec.healthy_volunteers is False
    # whitespace stripped, empty strings dropped
    assert rec.conditions == ("Type 2 Diabetes", "Obesity")
    assert rec.n_locations == 3
    assert rec.countries == ("Canada", "United States")


def test_parse_study_minimal_record_has_none_not_errors() -> None:
    rec = parse_study({"protocolSection": {"identificationModule": {"nctId": "NCT00000009"}}})
    assert rec.brief_title is None
    assert rec.phases == ()
    assert rec.n_locations == 0


def test_parse_study_without_nct_id_raises() -> None:
    with pytest.raises(ValueError, match="nctId"):
        parse_study({"protocolSection": {}})
