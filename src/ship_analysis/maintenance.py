from __future__ import annotations

from datetime import date
import logging
import time

from .compaction import DailyCompactor
from .config import AppConfig
from .reporting import CollectionReporter
from .retention import StagingRetention
from .storage import Storage


LOGGER = logging.getLogger("ship_analysis.maintenance")


class MaintenanceWorker:
    """Run reporting and compaction outside the collector.

    Retention is deliberately opt-in.  Large staging deletes can hold a
    SQLite write lock for a long time, so they must not run from the
    always-on maintenance loop that shares the collector database.
    """

    def __init__(self, config: AppConfig, *, run_retention: bool = False) -> None:
        self.config = config
        self.storage = Storage(config.database, config.raw_dir)
        self.storage.initialize()
        self.compactor = DailyCompactor(config, self.storage)
        self.reporter = CollectionReporter(config, self.storage)
        self.retention = StagingRetention(config, self.storage)
        self.run_retention = run_retention
        self._next_retention_check = 0.0

    def run_once(self) -> None:
        recovered = self.storage.recover_stale_runs()
        if recovered:
            LOGGER.warning("recovered-stale-runs count=%d", recovered)

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
        if self.run_retention and (
            outcomes or time.monotonic() >= self._next_retention_check
        ):
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
            self._next_retention_check = time.monotonic() + (
                self.config.retention.cleanup_interval_hours * 3600
            )

    def run_forever(self, interval_seconds: int = 60) -> None:
        if interval_seconds < 10:
            raise ValueError("maintenance interval must be at least 10 seconds")
        LOGGER.info("maintenance-started interval_seconds=%d", interval_seconds)
        while True:
            started = time.monotonic()
            self.run_once()
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval_seconds - elapsed))
