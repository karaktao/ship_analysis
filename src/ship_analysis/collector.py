from __future__ import annotations

from dataclasses import dataclass
import heapq
import logging
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

        LOGGER.info("scheduler-started targets=%d", len(queue))
        while True:
            due_at, negative_priority, target_id, target = heapq.heappop(queue)
            wait = due_at - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, self.config.idle_sleep_seconds))
                heapq.heappush(
                    queue, (due_at, negative_priority, target_id, target)
                )
                continue

            scheduler_lag = max(0.0, time.monotonic() - due_at)
            try:
                self.collect(target, scheduler_lag_seconds=scheduler_lag)
            except Exception:
                # The failed run is already persisted; keep other areas alive.
                pass
            finally:
                next_due = max(
                    due_at + target.interval_seconds,
                    time.monotonic() + 0.01,
                )
                heapq.heappush(
                    queue,
                    (next_due, negative_priority, target_id, target),
                )
