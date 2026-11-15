"""HTTP client for the ClinicalTrials.gov API v2.

Handles the three things that break naive ``requests.get`` loops:

* **Pagination** — the API returns at most 1000 studies per page plus a ``nextPageToken``.
* **Rate limits and transient errors** — retries 429/5xx/connection errors with exponential
  backoff and jitter, honouring ``Retry-After``; spaces requests out (~50 requests/min per IP).
* **Fetching specific trials** — TREC judgments reference fixed NCT IDs, so the client can
  fetch an explicit ID list in batches via ``filter.ids``.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from typing import Any

import requests

from ctmatch.config import Settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Keeps the request URL well under common length limits (~12 chars per NCT ID).
DEFAULT_ID_BATCH_SIZE = 200

Study = dict[str, Any]


class CTGovError(RuntimeError):
    """Raised when the API keeps failing after all retries, or returns a non-retryable error."""


class CTGovClient:
    """Minimal, testable client for ``GET /studies``.

    Args:
        base_url: API root, e.g. ``https://clinicaltrials.gov/api/v2``.
        session: Injected ``requests.Session`` (pass a fake in tests).
        page_size: Studies per page (API max is 1000).
        min_interval_s: Minimum seconds between requests (client-side rate limit).
        timeout_s: Per-request timeout.
        max_retries: Retries for retryable failures before raising ``CTGovError``.
        sleep: Injected sleep function so tests run instantly.
        clock: Injected monotonic clock.
    """

    def __init__(
        self,
        base_url: str = "https://clinicaltrials.gov/api/v2",
        *,
        session: requests.Session | None = None,
        page_size: int = 1000,
        min_interval_s: float = 1.3,
        timeout_s: float = 30.0,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.page_size = page_size
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self.requests_made = 0

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> CTGovClient:
        """Create a client from application settings."""
        return cls(
            settings.ctgov_base_url,
            page_size=settings.ctgov_page_size,
            min_interval_s=settings.ctgov_min_interval_s,
            timeout_s=settings.ctgov_timeout_s,
            max_retries=settings.ctgov_max_retries,
            **kwargs,
        )

    # ------------------------------------------------------------------ public API

    def iter_pages(
        self,
        *,
        query_cond: str | None = None,
        query_term: str | None = None,
        updated_since: date | None = None,
        nct_ids: Sequence[str] | None = None,
        max_pages: int | None = None,
        id_batch_size: int = DEFAULT_ID_BATCH_SIZE,
    ) -> Iterator[list[Study]]:
        """Yield pages (lists of raw study dicts) matching the filters.

        Args:
            query_cond: Condition search, e.g. ``"type 2 diabetes"``.
            query_term: Free-text search across all fields.
            updated_since: Only studies whose ``LastUpdatePostDate`` is on/after this date
                (used for incremental loads).
            nct_ids: Fetch exactly these trials (batched). Combined with other filters.
            max_pages: Stop after this many pages (smoke tests, sampling).
            id_batch_size: NCT IDs per request when ``nct_ids`` is given.
        """
        base = self._build_params(query_cond, query_term, updated_since)
        pages = 0
        if nct_ids:
            ids = list(dict.fromkeys(nct_ids))  # de-duplicate, keep order
            for start in range(0, len(ids), id_batch_size):
                params = {**base, "filter.ids": ",".join(ids[start : start + id_batch_size])}
                for page in self._paginate(params):
                    yield page
                    pages += 1
                    if max_pages is not None and pages >= max_pages:
                        return
        else:
            for page in self._paginate(base):
                yield page
                pages += 1
                if max_pages is not None and pages >= max_pages:
                    return

    def iter_studies(self, **kwargs: Any) -> Iterator[Study]:
        """Yield individual raw studies; same arguments as :meth:`iter_pages`."""
        for page in self.iter_pages(**kwargs):
            yield from page

    # ------------------------------------------------------------------ internals

    def _build_params(
        self, query_cond: str | None, query_term: str | None, updated_since: date | None
    ) -> dict[str, str]:
        params: dict[str, str] = {"format": "json", "pageSize": str(self.page_size)}
        if query_cond:
            params["query.cond"] = query_cond
        if query_term:
            params["query.term"] = query_term
        if updated_since:
            params["filter.advanced"] = (
                f"AREA[LastUpdatePostDate]RANGE[{updated_since.isoformat()},MAX]"
            )
        return params

    def _paginate(self, params: dict[str, str]) -> Iterator[list[Study]]:
        token: str | None = None
        while True:
            page_params = {**params, "pageToken": token} if token else params
            data = self._get_json("/studies", page_params)
            studies = data.get("studies", [])
            if not isinstance(studies, list):
                raise CTGovError(f"Unexpected response shape: 'studies' is {type(studies)}")
            if studies:
                yield studies
            token = data.get("nextPageToken")
            if not token:
                return

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self.min_interval_s - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _backoff_seconds(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass  # HTTP-date form; fall back to exponential backoff
        return float(min(60.0, 2.0**attempt) + random.uniform(0, 1))

    def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.requests_made += 1
            retry_after: str | None = None
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    payload: dict[str, Any] = resp.json()
                    return payload
                if resp.status_code not in RETRYABLE_STATUS:
                    raise CTGovError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")

            if attempt < self.max_retries:
                delay = self._backoff_seconds(attempt, retry_after)
                logger.warning(
                    "CT.gov request failed (%s); retry %d/%d in %.1fs",
                    last_error,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                self._sleep(delay)
        raise CTGovError(f"Giving up after {self.max_retries + 1} attempts: {last_error}")
