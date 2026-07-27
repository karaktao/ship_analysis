from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import heapq
import logging
import threading
import time

from .compaction import DailyCompactor
from .config import AppConfig, CollectionTarget
from .providers import EurisClient
from .reporting import CollectionReporter
from .retention import StagingRetention
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
        self.storage.initialize()
        self.compactor = DailyCompactor(config, self.storage)
        self.reporter = CollectionReporter(config, self.storage)
        self.retention = StagingRetention(config, self.storage)
        self._compaction_stop = threading.Event()

    def _run_compaction_worker(self) -> None:
        next_retention_check = 0.0
        while not self._compaction_stop.is_set():
            outcomes = []
            try:
                self.reporter.refresh()
            except Exception:
                LOGGER.exception("collection-reporting-refresh-failed")
            try:
                outcomes = self.compactor.compact_pending()
                for outcome in outcomes:
                    LOGGER.info(
                        "daily-compaction-ok day=%s samples=%d records=%d "
                        "stationary=%d collapsed_samples=%d output=%s",
                        outcome.operational_date,
                        outcome.source_samples,
                        outcome.output_records,
                        outcome.stationary_records,
                        outcome.stationary_source_samples,
                        outcome.output_path,
                    )
            except Exception:
                LOGGER.exception("daily-compaction-failed")
            try:
                if outcomes:
                    for outcome in outcomes:
                        self.reporter.generate_daily_summary(
                            date.fromisoformat(outcome.operational_date),
                            force=True,
                        )
                else:
                    self.reporter.generate_latest_ready_summary()
            except Exception:
                LOGGER.exception("daily-collection-summary-failed")
            if outcomes or time.monotonic() >= next_retention_check:
                try:
                    retention = self.retention.prune()
                    if retention.candidate_runs or retention.skipped_uncompacted:
                        LOGGER.info(
                            "staging-retention-ok through=%s runs=%d raw=%d "
                            "raw-too-new=%d links=%d observations=%d "
                            "run-history=%d uncompacted=%d unsafe=%d "
                            "bytes=%d checkpoint-busy=%d",
                            retention.eligible_through_operational_date,
                            retention.candidate_runs,
                            retention.raw_deleted,
                            retention.skipped_raw_too_new,
                            retention.provenance_links_deleted,
                            retention.observations_deleted,
                            retention.run_history_deleted,
                            retention.skipped_uncompacted,
                            retention.skipped_unsafe_path,
                            retention.bytes_deleted,
                            retention.wal_checkpoint_busy,
                        )
                except Exception:
                    LOGGER.exception("staging-retention-failed")
                next_retention_check = time.monotonic() + (
                    self.config.retention.cleanup_interval_hours * 3600
                )
            self._compaction_stop.wait(60)

    def collect(self, target: CollectionTarget) -> CollectionOutcome:
        run_id = self.storage.start_run(self.config.provider.name, target)
        LOGGER.info(
            "fetch-start run=%s target=%s bbox=%s",
            run_id,
            target.id,
            target.bbox.compact(),
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
            "existing=%d page_duplicates=%d count_delta=%s pages=%d elapsed=%.2fs",
            run_id,
            target.id,
            outcome.items,
            outcome.unique_items,
            outcome.inserted,
            outcome.existing,
            outcome.within_run_duplicates,
            outcome.reported_count_delta,
            outcome.pages,
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

        compaction_thread = threading.Thread(
            target=self._run_compaction_worker,
            name="collection-maintenance",
            daemon=True,
        )
        compaction_thread.start()

        LOGGER.info("scheduler-started targets=%d", len(queue))
        try:
            while True:
                due_at, negative_priority, target_id, target = heapq.heappop(queue)
                wait = due_at - time.monotonic()
                if wait > 0:
                    time.sleep(min(wait, self.config.idle_sleep_seconds))
                    heapq.heappush(
                        queue, (due_at, negative_priority, target_id, target)
                    )
                    continue

                try:
                    self.collect(target)
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
        finally:
            self._compaction_stop.set()
            compaction_thread.join(timeout=1)
