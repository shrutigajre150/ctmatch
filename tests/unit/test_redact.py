from __future__ import annotations

import json
from typing import Any

import pytest

from ctmatch.privacy.redact import contains_pii, redact_text, scrub_raw_study


def test_redacts_email_phone_and_titled_name() -> None:
    text = "Contact Dr. Alan Grant at agrant@example.org or +1 (415) 555-0134."
    result = redact_text(text)
    assert result.text == "Contact [NAME] at [EMAIL] or [PHONE]."
    assert result.counts == {"email": 1, "phone": 1, "name": 1}
    assert result.total == 3


@pytest.mark.parametrize(
    "text",
    [
        "HbA1c 7.0-10.5%",
        "Diagnosed 2015-01-01 or later",
        "Platelets >= 100000 /uL",
        "Study period 2019-2021",
        "eGFR < 30 mL/min/1.73 m2",
        "NCT01234567 is a related trial",
        "ages 18-65 years",
    ],
)
def test_clinical_text_is_not_redacted(text: str) -> None:
    """False positives would corrupt eligibility criteria, which later stages depend on."""
    assert redact_text(text).text == text


@pytest.mark.parametrize("phone", ["415-555-0134", "415.555.0134", "+44 20 7946 0958"])
def test_phone_formats(phone: str) -> None:
    assert redact_text(f"call {phone} now").text == "call [PHONE] now"


def test_contains_pii() -> None:
    assert contains_pii("mail me: a.b@c.io")
    assert not contains_pii("no contact info here")
    assert not contains_pii(None)


def test_scrub_raw_study_removes_people_keeps_sites(raw_study: dict[str, Any]) -> None:
    before = json.dumps(raw_study)
    clean = scrub_raw_study(raw_study)
    module = clean["protocolSection"]["contactsLocationsModule"]
    assert "centralContacts" not in module
    assert "overallOfficials" not in module
    assert all("contacts" not in loc for loc in module["locations"])
    assert module["locations"][0]["facility"] == "City Hospital"
    assert "responsibleParty" not in clean["protocolSection"]["sponsorCollaboratorsModule"]
    assert json.dumps(raw_study) == before, "input must not be mutated"
    serialized = json.dumps(clean)
    for leaked in ("jdoe@example.org", "Jane Doe", "Ian Malcolm", "Ellie Sattler", "jroe@"):
        assert leaked not in serialized
