from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import requests

from ctmatch.ingest.client import CTGovClient, CTGovError


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Returns queued responses (or raises queued exceptions) and records request params."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, params: dict[str, str], timeout: float) -> Any:
        self.calls.append(dict(params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def page(ids: list[str], token: str | None = None) -> FakeResponse:
    payload: dict[str, Any] = {
        "studies": [{"protocolSection": {"identificationModule": {"nctId": i}}} for i in ids]
    }
    if token:
        payload["nextPageToken"] = token
    return FakeResponse(200, payload)


def make_client(session: FakeSession, sleeps: list[float] | None = None) -> CTGovClient:
    log = sleeps if sleeps is not None else []
    return CTGovClient(
        session=session,  # type: ignore[arg-type]
        min_interval_s=0.0,
        max_retries=3,
        sleep=log.append,
    )


def test_follows_page_tokens_until_exhausted() -> None:
    session = FakeSession([page(["NCT1", "NCT2"], token="t2"), page(["NCT3"])])
    pages = list(make_client(session).iter_pages(query_cond="diabetes"))
    assert [len(p) for p in pages] == [2, 1]
    assert "pageToken" not in session.calls[0]
    assert session.calls[1]["pageToken"] == "t2"
    assert session.calls[0]["query.cond"] == "diabetes"


def test_max_pages_stops_early() -> None:
    session = FakeSession([page(["NCT1"], token="t2"), page(["NCT2"], token="t3")])
    assert len(list(make_client(session).iter_pages(max_pages=1))) == 1
    assert len(session.calls) == 1


def test_updated_since_builds_advanced_filter() -> None:
    session = FakeSession([page([])])
    list(make_client(session).iter_pages(updated_since=date(2026, 11, 1)))
    assert session.calls[0]["filter.advanced"] == "AREA[LastUpdatePostDate]RANGE[2026-11-01,MAX]"


def test_nct_ids_are_deduplicated_and_batched() -> None:
    ids = [f"NCT{i:08d}" for i in range(5)] + ["NCT00000000"]
    session = FakeSession([page(ids[:2]), page(ids[2:4]), page(ids[4:5])])
    studies = list(make_client(session).iter_studies(nct_ids=ids, id_batch_size=2))
    assert len(studies) == 5
    assert [c["filter.ids"].count(",") + 1 for c in session.calls] == [2, 2, 1]


def test_retries_429_honouring_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "7"}),
            requests.ConnectionError("reset"),
            page(["NCT1"]),
        ]
    )
    client = make_client(session, sleeps)
    assert len(list(client.iter_studies())) == 1
    assert client.requests_made == 3
    assert sleeps[0] == 7.0  # Retry-After respected
    assert 2.0 <= sleeps[1] <= 3.0  # exponential backoff (2**1) + jitter


def test_gives_up_after_max_retries() -> None:
    session = FakeSession([FakeResponse(503)] * 4)
    with pytest.raises(CTGovError, match="Giving up after 4 attempts"):
        list(make_client(session).iter_pages())


def test_non_retryable_error_fails_fast() -> None:
    session = FakeSession([FakeResponse(400, payload="bad param")])
    with pytest.raises(CTGovError, match="HTTP 400"):
        list(make_client(session).iter_pages())
    assert len(session.calls) == 1


def test_throttle_spaces_requests() -> None:
    sleeps: list[float] = []
    t = iter([0.0, 0.5, 0.5])  # clock reads: stamp #1, check #2, stamp #2
    client = CTGovClient(
        session=FakeSession([page(["A"], token="x"), page(["B"])]),  # type: ignore[arg-type]
        min_interval_s=1.3,
        sleep=sleeps.append,
        clock=lambda: next(t),
    )
    list(client.iter_pages())
    assert sleeps == [pytest.approx(0.8)]
