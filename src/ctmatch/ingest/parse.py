"""Flatten a raw ClinicalTrials.gov v2 study into a typed :class:`TrialRecord`.

The API nests everything under ``protocolSection.<module>``. Fields are frequently missing,
so every lookup is defensive: a missing field becomes ``None`` here and is judged later by the
data quality checks (parsing never decides whether a record is "good").
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ctmatch.models import DatePrecision, TrialRecord

_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(year|month|week|day|hour|minute)s?\s*$", re.I)
_UNIT_TO_YEARS = {
    "year": 1.0,
    "month": 1.0 / 12.0,
    "week": 7.0 / 365.25,
    "day": 1.0 / 365.25,
    "hour": 1.0 / (365.25 * 24),
    "minute": 1.0 / (365.25 * 24 * 60),
}


def get_path(obj: Any, path: str, default: Any = None) -> Any:
    """Safely read a dotted path (``"a.b.c"``) from nested dicts; return ``default`` if absent."""
    cur = obj
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def parse_ct_date(value: str | None) -> tuple[date | None, DatePrecision | None]:
    """Parse ``YYYY-MM-DD``, ``YYYY-MM`` or ``YYYY`` into a date plus its precision.

    Month-only dates map to the 1st of the month, year-only dates to Jan 1. The precision is
    kept so later analyses (e.g. survival time in days) can account for the imputation.
    Unparseable strings return ``(None, None)``.
    """
    if not value or not isinstance(value, str):
        return None, None
    parts = value.strip().split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2])), DatePrecision.DAY
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1), DatePrecision.MONTH
        if len(parts) == 1 and len(parts[0]) == 4:
            return date(int(parts[0]), 1, 1), DatePrecision.YEAR
    except ValueError:
        return None, None
    return None, None


def parse_age_years(value: str | None) -> float | None:
    """Convert API age strings like ``"18 Years"`` or ``"6 Months"`` to years (``None`` if N/A)."""
    if not value or not isinstance(value, str):
        return None
    match = _AGE_RE.match(value)
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2).lower()
    return round(amount * _UNIT_TO_YEARS[unit], 4)


def _str_tuple(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(v.strip() for v in values if isinstance(v, str) and v.strip())


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def parse_study(raw: dict[str, Any]) -> TrialRecord:
    """Map one raw study (as returned by ``GET /studies``) to a :class:`TrialRecord`.

    Raises:
        ValueError: if the study has no NCT ID (the one field we cannot key a record without).
    """
    ps = raw.get("protocolSection", {})
    nct_id = get_path(ps, "identificationModule.nctId")
    if not isinstance(nct_id, str) or not nct_id.strip():
        raise ValueError("Study is missing protocolSection.identificationModule.nctId")

    status = ps.get("statusModule", {})
    start, start_prec = parse_ct_date(get_path(status, "startDateStruct.date"))
    pcd, pcd_prec = parse_ct_date(get_path(status, "primaryCompletionDateStruct.date"))
    completion, _ = parse_ct_date(get_path(status, "completionDateStruct.date"))
    last_update, _ = parse_ct_date(get_path(status, "lastUpdatePostDateStruct.date"))

    design = ps.get("designModule", {})
    elig = ps.get("eligibilityModule", {})
    locations = get_path(ps, "contactsLocationsModule.locations", []) or []
    countries = sorted(
        {loc["country"] for loc in locations if isinstance(loc, dict) and loc.get("country")}
    )
    healthy = elig.get("healthyVolunteers")

    return TrialRecord(
        nct_id=nct_id.strip(),
        brief_title=_clean_text(get_path(ps, "identificationModule.briefTitle")),
        official_title=_clean_text(get_path(ps, "identificationModule.officialTitle")),
        brief_summary=_clean_text(get_path(ps, "descriptionModule.briefSummary")),
        overall_status=_clean_text(status.get("overallStatus")),
        why_stopped=_clean_text(status.get("whyStopped")),
        study_type=_clean_text(design.get("studyType")),
        phases=_str_tuple(design.get("phases")),
        lead_sponsor_name=_clean_text(get_path(ps, "sponsorCollaboratorsModule.leadSponsor.name")),
        lead_sponsor_class=_clean_text(
            get_path(ps, "sponsorCollaboratorsModule.leadSponsor.class")
        ),
        enrollment_count=_to_int(get_path(design, "enrollmentInfo.count")),
        enrollment_type=_clean_text(get_path(design, "enrollmentInfo.type")),
        start_date=start,
        start_date_precision=start_prec,
        primary_completion_date=pcd,
        primary_completion_precision=pcd_prec,
        completion_date=completion,
        last_update_date=last_update,
        conditions=_str_tuple(get_path(ps, "conditionsModule.conditions")),
        keywords=_str_tuple(get_path(ps, "conditionsModule.keywords")),
        eligibility_criteria=_clean_text(elig.get("eligibilityCriteria")),
        sex=_clean_text(elig.get("sex")),
        minimum_age_years=parse_age_years(elig.get("minimumAge")),
        maximum_age_years=parse_age_years(elig.get("maximumAge")),
        healthy_volunteers=healthy if isinstance(healthy, bool) else None,
        n_locations=len(locations) if isinstance(locations, list) else 0,
        countries=tuple(countries),
    )
