from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ctmatch.benchmarks.trec import (
    judged_nct_ids,
    parse_qrels,
    parse_qrels_file,
    parse_topics,
    summarize_qrels,
)
from ctmatch.config import Settings
from ctmatch.pipeline import process_page

# ----------------------------------------------------------------- TREC


def test_parse_topics(fixtures_dir: Path) -> None:
    topics = parse_topics(fixtures_dir / "topics_sample.xml", 2021)
    assert [t.topic_id for t in topics] == ["1", "2"]
    assert topics[0].text.startswith("Patient is a 45-year-old man")
    assert "\n" not in topics[0].text  # whitespace normalised


def test_parse_qrels_and_summary(fixtures_dir: Path) -> None:
    qrels = parse_qrels_file(fixtures_dir / "qrels_sample.txt", 2021)
    assert len(qrels) == 5
    assert judged_nct_ids(qrels) == ["NCT00000102", "NCT00000104", "NCT00000108", "NCT01234567"]
    assert summarize_qrels(qrels) == {
        "n_judgments": 5,
        "n_topics": 2,
        "n_trials": 4,
        "n_not_relevant": 2,
        "n_excluded": 1,
        "n_eligible": 2,
    }


@pytest.mark.parametrize("line", ["1 0 NCT1", "1 0 NCT1 3"])
def test_parse_qrels_rejects_bad_lines(line: str) -> None:
    with pytest.raises(ValueError):
        parse_qrels([line], 2021)


def test_parse_topics_rejects_empty_topic(tmp_path: Path) -> None:
    path = tmp_path / "t.xml"
    path.write_text('<topics><topic number="1">   </topic></topics>')
    with pytest.raises(ValueError, match="no text"):
        parse_topics(path, 2021)


# ----------------------------------------------------------------- pipeline


def test_process_page_end_to_end(raw_study: dict[str, Any], make_raw: Any) -> None:
    raws = [raw_study] + [make_raw(f"NCT0000000{i}") for i in range(1, 5)]
    result = process_page(raws)

    assert result.parse_failures == []
    assert len(result.report.passed) == 5
    # Contacts never reach storage, and free text was redacted.
    stored = json.dumps(result.scrubbed_raw)
    assert "jdoe@example.org" not in stored and "Jane Doe" not in stored
    terminated = next(r for r in result.report.passed if r.nct_id == "NCT01234567")
    assert terminated.why_stopped == "Sponsor decision; questions to [NAME] at [EMAIL]"
    assert terminated.brief_summary is not None and "[PHONE]" in terminated.brief_summary
    assert terminated.redaction_counts == {"name": 1, "email": 1, "phone": 1}
    assert result.redaction_totals == {"name": 1, "email": 1, "phone": 1}
    # Clinical text untouched.
    assert terminated.eligibility_criteria is not None
    assert "HbA1c 7.0-10.5%" in terminated.eligibility_criteria


def test_process_page_counts_parse_failures(make_raw: Any) -> None:
    result = process_page([make_raw("NCT00000001"), {"protocolSection": {}}])
    assert len(result.parse_failures) == 1
    assert len(result.report.passed) == 1


def test_settings_from_env() -> None:
    s = Settings.from_env(
        {"CTMATCH_CTGOV_PAGE_SIZE": "50", "CTMATCH_DATABASE_URL": "postgresql://x"}
    )
    assert s.ctgov_page_size == 50
    assert s.database_url == "postgresql://x"
    assert Settings.from_env({}).ctgov_page_size == 1000
