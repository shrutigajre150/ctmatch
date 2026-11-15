"""Parsers for the TREC Clinical Trials track (2021 and 2022).

* **Topics**: XML, one ``<topic number="N">`` per synthetic patient case (admission-note style).
* **Qrels**: whitespace-separated ``topic_id  0  NCT_ID  relevance`` lines, where relevance is
  0 = not relevant, 1 = excluded (relevant but patient fails criteria), 2 = eligible.

Both years judge trials from the same April 27, 2021 ClinicalTrials.gov snapshot. We fetch those
trials from today's API, so a small share may have been edited or removed since; the ingestion
flow reports how many judged trials could not be fetched.

2023 used a different, structured topic format and is not handled yet.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

VALID_RELEVANCE = frozenset({0, 1, 2})


@dataclass(frozen=True)
class Topic:
    year: int
    topic_id: str
    text: str


@dataclass(frozen=True)
class Qrel:
    year: int
    topic_id: str
    nct_id: str
    relevance: int


def parse_topics(path: Path, year: int) -> list[Topic]:
    """Parse a TREC CT topics XML file. Topics with empty text are rejected.

    The files come from NIST (trusted source); for untrusted XML use ``defusedxml``.
    """
    root = ET.parse(path).getroot()
    topics: list[Topic] = []
    for el in root.iter("topic"):
        number = el.get("number")
        text = " ".join("".join(el.itertext()).split())
        if number is None:
            raise ValueError(f"{path}: <topic> without a number attribute")
        if not text:
            raise ValueError(f"{path}: topic {number} has no text")
        topics.append(Topic(year=year, topic_id=number, text=text))
    if not topics:
        raise ValueError(f"{path}: no <topic> elements found")
    return topics


def parse_qrels(lines: Iterable[str], year: int) -> list[Qrel]:
    """Parse qrels lines. Raises ``ValueError`` on malformed lines or unknown labels."""
    qrels: list[Qrel] = []
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise ValueError(f"qrels line {lineno}: expected 4 fields, got {len(parts)}")
        topic_id, _iteration, nct_id, rel = parts
        relevance = int(rel)
        if relevance not in VALID_RELEVANCE:
            raise ValueError(f"qrels line {lineno}: relevance {relevance} not in {{0,1,2}}")
        qrels.append(Qrel(year=year, topic_id=topic_id, nct_id=nct_id, relevance=relevance))
    return qrels


def parse_qrels_file(path: Path, year: int) -> list[Qrel]:
    with path.open(encoding="utf-8") as fh:
        return parse_qrels(fh, year)


def judged_nct_ids(qrels: Iterable[Qrel]) -> list[str]:
    """Sorted unique NCT IDs that appear in any judgment."""
    return sorted({q.nct_id for q in qrels})


def summarize_qrels(qrels: Iterable[Qrel]) -> dict[str, int]:
    """Counts useful for a README table: topics, judged trials, label distribution."""
    items = list(qrels)
    return {
        "n_judgments": len(items),
        "n_topics": len({(q.year, q.topic_id) for q in items}),
        "n_trials": len({q.nct_id for q in items}),
        "n_not_relevant": sum(q.relevance == 0 for q in items),
        "n_excluded": sum(q.relevance == 1 for q in items),
        "n_eligible": sum(q.relevance == 2 for q in items),
    }
