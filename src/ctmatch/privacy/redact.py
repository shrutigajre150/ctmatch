"""PII handling, in two layers.

1. **Data minimization** (:func:`scrub_raw_study`): ClinicalTrials.gov records name real people
   (investigators, study contacts) with emails and phone numbers. We have no use for them, so
   they are deleted from the raw payload *before* it is stored.
2. **Free-text redaction** (:func:`redact_text`): emails, phone numbers and "Dr./Prof. Name"
   mentions can still appear inside eligibility criteria and summaries. These are replaced with
   typed placeholders (``[EMAIL]``, ``[PHONE]``, ``[NAME]``) and counted.

The same :func:`redact_text` guard is reused before any text is sent to an LLM (v0.4).

Limitation (be honest about it): regexes catch structured PII (emails, phones) well but only
catch names with a title prefix. A bare "contact Jane Smith" is missed. For patient text an
NER-based tool (e.g. Microsoft Presidio) would be the next step.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Candidate phone numbers: digit groups joined by space/dot/dash, optional +country and (area).
# Separators are required, so plain numbers ("100000") never match. Candidates with fewer
# than 10 digits are rejected in code, which keeps dates ("2019-03-01") and ranges intact.
PHONE_CANDIDATE_RE = re.compile(
    r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]\d{2,4}){1,4}(?![\w-])"
)
MIN_PHONE_DIGITS = 10

NAME_RE = re.compile(r"\b(?:Dr|Prof|Professor)\.?\s+[A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)?")

# Free-text fields of TrialRecord that are redacted.
REDACTED_FIELDS = ("eligibility_criteria", "brief_summary", "why_stopped")


@dataclass(frozen=True)
class RedactionResult:
    """Redacted text plus how many of each PII type were replaced."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _is_iso_date(candidate: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate.strip()))


def redact_text(text: str) -> RedactionResult:
    """Replace emails, phone numbers and titled names with placeholders.

    Order matters: emails first (they contain dots/digits the phone pattern could touch).
    """
    counts: Counter[str] = Counter()

    def _sub_email(_: re.Match[str]) -> str:
        counts["email"] += 1
        return "[EMAIL]"

    def _sub_phone(match: re.Match[str]) -> str:
        candidate = match.group(0)
        digits = sum(ch.isdigit() for ch in candidate)
        if digits < MIN_PHONE_DIGITS or _is_iso_date(candidate):
            return candidate
        counts["phone"] += 1
        return "[PHONE]"

    def _sub_name(_: re.Match[str]) -> str:
        counts["name"] += 1
        return "[NAME]"

    out = EMAIL_RE.sub(_sub_email, text)
    out = PHONE_CANDIDATE_RE.sub(_sub_phone, out)
    out = NAME_RE.sub(_sub_name, out)
    return RedactionResult(text=out, counts=dict(counts))


def contains_pii(text: str | None) -> bool:
    """True if ``text`` still contains an email or phone number. Used as a DQ backstop."""
    if not text:
        return False
    counts = redact_text(text).counts
    return counts.get("email", 0) + counts.get("phone", 0) > 0


def scrub_raw_study(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a raw study with all personal contact data removed.

    Removes ``centralContacts``, ``overallOfficials`` and per-site ``contacts``; keeps site
    facility/city/country (organisations, not people). The input is not mutated.
    """
    clean = copy.deepcopy(raw)
    module = clean.get("protocolSection", {}).get("contactsLocationsModule")
    if isinstance(module, dict):
        module.pop("centralContacts", None)
        module.pop("overallOfficials", None)
        for loc in module.get("locations", []) or []:
            if isinstance(loc, dict):
                loc.pop("contacts", None)
    # Responsible party can name an individual investigator.
    sponsor = clean.get("protocolSection", {}).get("sponsorCollaboratorsModule")
    if isinstance(sponsor, dict):
        sponsor.pop("responsibleParty", None)
    return clean
