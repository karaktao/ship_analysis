from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import random
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from ..config import BBox, ProviderConfig


LOGGER = logging.getLogger("ship_analysis.providers.euris")


class FetchError(RuntimeError):
    """Raised when a complete, validated pagination run cannot be fetched."""


class ProviderUnavailable(FetchError):
    """Raised while the provider circuit breaker is cooling down."""


class _RetryableFetchError(FetchError):
    """Internal marker for transient provider/network failures."""


@dataclass(frozen=True)
class FetchResult:
    items: tuple[dict[str, Any], ...]
    pages: int
    reported_count: int | None
    reported_count_delta: int | None
    fetched_at_utc: str
    source_url: str
    elapsed_seconds: float
    retry_count: int = 0


class EurisClient:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_lock = threading.Lock()

    def fetch_bbox(self, bbox: BBox) -> FetchResult:
        self._ensure_circuit_available()
        source_url = f"{self.config.base_url}?{urlencode(bbox.query_parameters())}"
        next_url: str | None = source_url
        visited: set[str] = set()
        items: list[dict[str, Any]] = []
        reported_count: int | None = None
        page = 0
        retry_count = 0
        started = time.monotonic()
        deadline = started + self.config.request_budget_seconds

        try:
            while next_url:
                if next_url in visited:
                    raise FetchError(
                        f"Pagination cycle detected at page {page + 1}"
                    )
                if page >= self.config.max_pages:
                    raise FetchError(
                        f"Pagination exceeded max_pages={self.config.max_pages}; "
                        "increase the guard only after checking the bbox"
                    )

                visited.add(next_url)
                page += 1
                payload, page_retries = self._request_json(next_url, deadline)
                retry_count += page_retries
                if not isinstance(payload, dict):
                    raise FetchError(
                        "Expected the EuRIS v2 page object; provider contract "
                        "may have changed"
                    )

                page_items = payload.get("items") or []
                if not isinstance(page_items, list):
                    raise FetchError(f"Page {page} has a non-list items field")
                items.extend(
                    item for item in page_items if isinstance(item, dict)
                )

                if reported_count is None and payload.get("count") is not None:
                    try:
                        reported_count = int(payload["count"])
                    except (TypeError, ValueError):
                        reported_count = None

                raw_next = payload.get("nextPageLink")
                next_url = urljoin(next_url, str(raw_next)) if raw_next else None
                if next_url and self.config.request_gap_seconds:
                    remaining = deadline - time.monotonic()
                    if remaining <= self.config.request_gap_seconds:
                        raise _RetryableFetchError(
                            "EuRIS bbox request exceeded its "
                            f"{self.config.request_budget_seconds:g}s budget "
                            "during pagination"
                        )
                    time.sleep(self.config.request_gap_seconds)
        except _RetryableFetchError as error:
            self._record_transient_failure(error)
            raise FetchError(str(error)) from error

        self._record_success()

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
            retry_count=retry_count,
        )

    def _request_json(self, url: str, deadline: float) -> tuple[Any, int]:
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(self.config.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempts += 1
            headers = {
                "Accept": "application/json",
                "User-Agent": self.config.user_agent,
            }
            if self.config.token:
                headers["Authorization"] = f"Bearer {self.config.token}"

            request = Request(url, headers=headers, method="GET")
            try:
                timeout = min(self.config.timeout_seconds, remaining)
                with urlopen(request, timeout=timeout) as response:
                    return (
                        json.loads(response.read().decode("utf-8")),
                        attempt,
                    )
            except HTTPError as error:
                last_error = error
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    raise FetchError(f"EuRIS HTTP {error.code}: {error.reason}") from error
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                delay = self._retry_delay(attempt, retry_after)
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                delay = self._retry_delay(attempt, None)

            if attempt < self.config.max_retries:
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    break
                LOGGER.warning(
                    "euris-retry attempt=%d/%d delay=%.2fs remaining=%.2fs "
                    "error=%s",
                    attempts,
                    self.config.max_retries + 1,
                    delay,
                    remaining,
                    last_error,
                )
                time.sleep(delay)

        raise _RetryableFetchError(
            f"EuRIS request failed after {attempts} attempt(s) within "
            f"the {self.config.request_budget_seconds:g}s bbox budget: "
            f"{last_error or 'request budget exhausted'}"
        ) from last_error

    def _ensure_circuit_available(self) -> None:
        with self._circuit_lock:
            now = time.monotonic()
            if now < self._circuit_open_until:
                raise ProviderUnavailable(
                    "EuRIS circuit open; retry in "
                    f"{self._circuit_open_until - now:.1f}s"
                )
            if self._circuit_open_until:
                LOGGER.warning(
                    "euris-circuit-half-open prior_failures=%d",
                    self._consecutive_failures,
                )

    def _record_transient_failure(self, error: Exception) -> None:
        with self._circuit_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures < self.config.circuit_failure_threshold:
                return
            self._circuit_open_until = (
                time.monotonic() + self.config.circuit_cooldown_seconds
            )
            LOGGER.error(
                "euris-circuit-open failures=%d cooldown=%.1fs error=%s",
                self._consecutive_failures,
                self.config.circuit_cooldown_seconds,
                error,
            )

    def _record_success(self) -> None:
        with self._circuit_lock:
            if self._consecutive_failures or self._circuit_open_until:
                LOGGER.info(
                    "euris-circuit-recovered prior_failures=%d",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

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
