"""Typed domain models shared across the pipeline."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DatePrecision(StrEnum):
    """How precise a ClinicalTrials.gov date is. Many dates are month-only ("2019-03")."""

    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class TrialRecord(BaseModel):
    """One clinical trial, flattened from the API's nested JSON into analysis-ready fields.

    Contact details (names, emails, phones) are intentionally absent: we never parse them.
    """

    model_config = ConfigDict(frozen=True)

    nct_id: str
    brief_title: str | None = None
    official_title: str | None = None
    brief_summary: str | None = None
    overall_status: str | None = None
    why_stopped: str | None = None
    study_type: str | None = None
    phases: tuple[str, ...] = ()
    lead_sponsor_name: str | None = None
    lead_sponsor_class: str | None = None
    enrollment_count: int | None = None
    enrollment_type: str | None = None
    start_date: date | None = None
    start_date_precision: DatePrecision | None = None
    primary_completion_date: date | None = None
    primary_completion_precision: DatePrecision | None = None
    completion_date: date | None = None
    last_update_date: date | None = None
    conditions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    eligibility_criteria: str | None = None
    sex: str | None = None
    minimum_age_years: float | None = None
    maximum_age_years: float | None = None
    healthy_volunteers: bool | None = None
    n_locations: int = 0
    countries: tuple[str, ...] = ()
    redaction_counts: dict[str, int] = Field(default_factory=dict)
