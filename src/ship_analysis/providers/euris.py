from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from ..config import BBox, ProviderConfig


class FetchError(RuntimeError):
    """Raised when a complete, validated pagination run cannot be fetched."""


@dataclass(frozen=True)
class FetchResult:
    items: tuple[dict[str, Any], ...]
    pages: int
    reported_count: int | None
    reported_count_delta: int | None
    fetched_at_utc: str
    source_url: str
    elapsed_seconds: float


class EurisClient:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def fetch_bbox(self, bbox: BBox) -> FetchResult:
        source_url = f"{self.config.base_url}?{urlencode(bbox.query_parameters())}"
        next_url: str | None = source_url
        visited: set[str] = set()
        items: list[dict[str, Any]] = []
        reported_count: int | None = None
        page = 0
        started = time.monotonic()

        while next_url:
            if next_url in visited:
                raise FetchError(f"Pagination cycle detected at page {page + 1}")
            if page >= self.config.max_pages:
                raise FetchError(
                    f"Pagination exceeded max_pages={self.config.max_pages}; "
                    "increase the guard only after checking the bbox"
                )

            visited.add(next_url)
            page += 1
            payload = self._request_json(next_url)
            if not isinstance(payload, dict):
                raise FetchError(
                    "Expected the EuRIS v2 page object; provider contract may have changed"
                )

            page_items = payload.get("items") or []
            if not isinstance(page_items, list):
                raise FetchError(f"Page {page} has a non-list items field")
            items.extend(item for item in page_items if isinstance(item, dict))

            if reported_count is None and payload.get("count") is not None:
                try:
                    reported_count = int(payload["count"])
                except (TypeError, ValueError):
                    reported_count = None

            raw_next = payload.get("nextPageLink")
            next_url = urljoin(next_url, str(raw_next)) if raw_next else None
            if next_url and self.config.request_gap_seconds:
                time.sleep(self.config.request_gap_seconds)

        return FetchResult(
            items=tuple(items),
            pages=page,
            reported_count=reported_count,
            reported_count_delta=(
                len(items) - reported_count if reported_count is not None else None
            ),
            fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            source_url=source_url,
            elapsed_seconds=time.monotonic() - started,
        )

    def _request_json(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            headers = {
                "Accept": "application/json",
                "User-Agent": self.config.user_agent,
            }
            if self.config.token:
                headers["Authorization"] = f"Bearer {self.config.token}"

            request = Request(url, headers=headers, method="GET")
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                last_error = error
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    raise FetchError(f"EuRIS HTTP {error.code}: {error.reason}") from error
                delay = self._retry_delay(attempt, error.headers.get("Retry-After"))
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                delay = self._retry_delay(attempt, None)

            if attempt < self.config.max_retries:
                time.sleep(delay)

        raise FetchError(
            f"EuRIS request failed after {self.config.max_retries + 1} attempts: "
            f"{last_error}"
        ) from last_error

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    now = datetime.now(retry_at.tzinfo or timezone.utc)
                    return max(0.0, (retry_at - now).total_seconds())
                except (TypeError, ValueError):
                    pass
        return min(30.0, (2**attempt) + random.random())
