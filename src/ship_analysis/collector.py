from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import heapq
import logging
import threading
import time

from .config import AppConfig, CollectionTarget
from .providers import EurisClient, ProviderUnavailable
from .storage import Storage


LOGGER = logging.getLogger("ship_analysis.collector")


@dataclass(frozen=True)
class CollectionOutcome:
    run_id: str
    target_id: str
    items: int
    unique_items: int
    inserted: int
    existing: int
    within_run_duplicates: int
    reported_count_delta: int | None
    pages: int
    elapsed_seconds: float
    snapshot_path: str


class Collector:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.provider = EurisClient(config.provider)
        self.storage = Storage(config.database, config.raw_dir)
        # SQLite permits one writer at a time. Fetches may run concurrently,
        # while the larger observation-ingest transaction stays serialized
        # inside this collector process to avoid lock contention and retries.
        self._ingest_lock = threading.Lock()
        # Historical bbox-flag backfill can scan the full provenance table.
        # The live collector only needs schema checks; maintenance owns heavy
        # historical work so restarts return to polling promptly.
        self.storage.initialize(backfill_bbox_flags=False)
        # A restart after an OOM or host maintenance can leave a run in the
        # audit table as ``running``. These rows are not active requests and
        # should not pollute schedule-completion metrics.
        recovered = self.storage.recover_stale_runs()
        if recovered:
            LOGGER.warning("recovered-stale-runs count=%d", recovered)

    def collect(
        self,
        target: CollectionTarget,
        *,
        scheduler_lag_seconds: float = 0.0,
    ) -> CollectionOutcome:
        run_id = self.storage.start_run(self.config.provider.name, target)
        LOGGER.info(
            "fetch-start run=%s target=%s bbox=%s scheduler_lag=%.2fs",
            run_id,
            target.id,
            target.bbox.compact(),
            scheduler_lag_seconds,
        )
        try:
            result = self.provider.fetch_bbox(target.bbox)
            snapshot_path = self.storage.write_snapshot(
                self.config.provider.name, target, run_id, result
            )
            with self._ingest_lock:
                inserted, existing, within_run_duplicates = self.storage.ingest(
                    run_id,
                    self.config.provider.name,
                    target,
                    result,
                    snapshot_path,
                )
        except ProviderUnavailable as error:
            self.storage.fail_run(run_id, f"{type(error).__name__}: {error}")
            LOGGER.warning(
                "fetch-skipped run=%s target=%s reason=%s",
                run_id,
                target.id,
                error,
            )
            raise
        except Exception as error:
            self.storage.fail_run(run_id, f"{type(error).__name__}: {error}")
            LOGGER.exception("fetch-failed run=%s target=%s", run_id, target.id)
            raise

        outcome = CollectionOutcome(
            run_id=run_id,
            target_id=target.id,
            items=len(result.items),
            unique_items=len(result.items) - within_run_duplicates,
            inserted=inserted,
            existing=existing,
            within_run_duplicates=within_run_duplicates,
            reported_count_delta=result.reported_count_delta,
            pages=result.pages,
            elapsed_seconds=result.elapsed_seconds,
            snapshot_path=str(snapshot_path),
        )
        LOGGER.info(
            "fetch-ok run=%s target=%s items=%d unique=%d inserted=%d "
            "existing=%d page_duplicates=%d count_delta=%s pages=%d retries=%d "
            "elapsed=%.2fs",
            run_id,
            target.id,
            outcome.items,
            outcome.unique_items,
            outcome.inserted,
            outcome.existing,
            outcome.within_run_duplicates,
            outcome.reported_count_delta,
            outcome.pages,
            result.retry_count,
            outcome.elapsed_seconds,
        )
        return outcome

    def _dispatch_target(
        self,
        executor: ThreadPoolExecutor,
        in_flight: dict[str, Future[CollectionOutcome]],
        target: CollectionTarget,
        *,
        scheduler_lag_seconds: float,
    ) -> str:
        if target.id in in_flight:
            return "target-still-running"
        if len(in_flight) >= self.config.collection_workers:
            return "worker-pool-full"
        in_flight[target.id] = executor.submit(
            self.collect,
            target,
            scheduler_lag_seconds=scheduler_lag_seconds,
        )
        return "submitted"

    @staticmethod
    def _reap_completed(
        in_flight: dict[str, Future[CollectionOutcome]],
    ) -> None:
        for target_id, future in tuple(in_flight.items()):
            if not future.done():
                continue
            try:
                future.result()
            except Exception:
                # collect() already records the failed run and full exception.
                pass
            del in_flight[target_id]

    def run_forever(self) -> None:
        targets = self.config.targets()
        if not targets:
            raise RuntimeError("No enabled collection targets in the configuration")

        now = time.monotonic()
        queue: list[tuple[float, int, str, CollectionTarget]] = []
        for target in targets:
            heapq.heappush(
                queue,
                (
                    now + target.initial_delay_seconds,
                    -target.priority,
                    target.id,
                    target,
                ),
            )

        in_flight: dict[str, Future[CollectionOutcome]] = {}
        LOGGER.info(
            "scheduler-started targets=%d workers=%d",
            len(queue),
            self.config.collection_workers,
        )
        executor = ThreadPoolExecutor(
            max_workers=self.config.collection_workers,
            thread_name_prefix="euris-collector",
        )
        try:
            while True:
                self._reap_completed(in_flight)
                due_at, negative_priority, target_id, target = heapq.heappop(queue)
                now = time.monotonic()
                wait = due_at - now
                if wait > 0:
                    time.sleep(min(wait, self.config.idle_sleep_seconds))
                    heapq.heappush(
                        queue, (due_at, negative_priority, target_id, target)
                    )
                    continue

                scheduler_lag = max(0.0, now - due_at)
                dispatch = self._dispatch_target(
                    executor,
                    in_flight,
                    target,
                    scheduler_lag_seconds=scheduler_lag,
                )
                if dispatch != "submitted":
                    LOGGER.warning(
                        "schedule-skipped target=%s reason=%s "
                        "in_flight=%d workers=%d scheduler_lag=%.2fs",
                        target.id,
                        dispatch,
                        len(in_flight),
                        self.config.collection_workers,
                        scheduler_lag,
                    )

                # Keep each tile aligned to its original cadence. If the
                # scheduler itself was paused, advance directly to the first
                # future slot instead of creating a catch-up request burst.
                intervals_elapsed = int(scheduler_lag // target.interval_seconds)
                next_due = due_at + (
                    intervals_elapsed + 1
                ) * target.interval_seconds
                heapq.heappush(
                    queue,
                    (next_due, negative_priority, target_id, target),
                )
        finally:
            LOGGER.info(
                "scheduler-stopping in_flight=%d", len(in_flight)
            )
            executor.shutdown(wait=False, cancel_futures=True)
