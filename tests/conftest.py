from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def raw_study() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "study_terminated.json").read_text())
    return data


def _make_raw(nct_id: str = "NCT00000001", **status: Any) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": f"Trial {nct_id}"},
            "statusModule": {
                "overallStatus": "COMPLETED",
                "startDateStruct": {"date": "2020-01-01"},
                "lastUpdatePostDateStruct": {"date": "2023-01-01"},
                **status,
            },
            "eligibilityModule": {"eligibilityCriteria": "Adults 18+"},
        }
    }


@pytest.fixture
def make_raw() -> Any:
    """Factory for a minimal valid raw study: ``make_raw("NCT00000002", overallStatus=...)``."""
    return _make_raw
