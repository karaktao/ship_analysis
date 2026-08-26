from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time

from .compaction import _parse_datetime, operational_date_for
from .config import AppConfig
from .models import utc_now_iso
from .storage import Storage


@dataclass(frozen=True)
class RetentionOutcome:
    completed_operational_days: int
    eligible_through_operational_date: str | None
    candidate_runs: int
    raw_deleted: int
    raw_missing: int
    provenance_links_deleted: int
    observations_deleted: int
    skipped_uncompacted: int
    skipped_unsafe_path: int
    bytes_deleted: int
    dry_run: bool
    skipped_raw_too_new: int = 0
    run_history_deleted: int = 0
    wal_checkpoint_busy: int = 0


class StagingRetention:
    """Remove rebuildable staging data after verified daily compaction."""

    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.settings = config.retention
        self.storage = storage
        self.raw_root = config.raw_dir.resolve()

    def prune(
        self,
        *,
        dry_run: bool = False,
        now_utc: datetime | None = None,
        max_runs: int | None = None,
        batch_pause_seconds: float = 0.0,
    ) -> RetentionOutcome:
        if max_runs is not None and max_runs < 1:
            raise ValueError("max_runs must be at least 1")
        if batch_pause_seconds < 0:
            raise ValueError("batch_pause_seconds cannot be negative")
        now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not self.settings.enabled:
            return RetentionOutcome(
                completed_operational_days=0,
                eligible_through_operational_date=None,
                candidate_runs=0,
                raw_deleted=0,
                raw_missing=0,
                provenance_links_deleted=0,
                observations_deleted=0,
                skipped_uncompacted=0,
                skipped_unsafe_path=0,
                bytes_deleted=0,
                dry_run=dry_run,
            )

        # The collector/maintenance startup owns schema initialization. A
        # frequent bounded retention pass must never run migrations or index
        # creation against the live database before doing its small batch.
        with self.storage.connect() as connection:
            completed_days = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT operational_date, output_path
                    FROM daily_compaction_runs
                    WHERE status = 'completed'
                    """
                )
                if self._valid_compacted_output(row[1])
            }
            run_query = """
                SELECT run_id, started_at_utc, snapshot_path,
                       raw_deleted_at_utc, details_deleted_at_utc
                FROM collection_runs
                WHERE status = 'completed'
                  AND (
                      details_deleted_at_utc IS NULL
                      OR (
                          snapshot_path IS NOT NULL
                          AND raw_deleted_at_utc IS NULL
                      )
                  )
                ORDER BY started_at_utc
            """
            parameters: tuple[int, ...] = ()
            if max_runs is not None:
                run_query += " LIMIT ?"
                parameters = (max_runs,)
            rows = connection.execute(run_query, parameters).fetchall()

        eligible_rows: list[sqlite3.Row] = []
        skipped_uncompacted = 0
        for row in rows:
            day = operational_date_for(
                _parse_datetime(row["started_at_utc"]),
                self.config.compaction,
            ).isoformat()
            if (
                self.settings.require_completed_compaction
                and day not in completed_days
            ):
                skipped_uncompacted += 1
                continue
            if day in completed_days:
                eligible_rows.append(row)

        detail_run_ids = [
            str(row["run_id"])
            for row in eligible_rows
            if row["details_deleted_at_utc"] is None
        ]
        raw_cutoff = now - timedelta(hours=self.settings.raw_min_age_hours)
        raw_rows: list[sqlite3.Row] = []
        skipped_raw_too_new = 0
        for row in eligible_rows:
            if row["snapshot_path"] is None or row["raw_deleted_at_utc"] is not None:
                continue
            if _parse_datetime(row["started_at_utc"]) > raw_cutoff:
                skipped_raw_too_new += 1
                continue
            raw_rows.append(row)

        if dry_run:
            links_deleted, observations_deleted = self._staging_counts(
                detail_run_ids
            )
        else:
            links_deleted = 0
            observations_deleted = 0

        raw_deleted = 0
        raw_missing = 0
        skipped_unsafe = 0
        bytes_deleted = 0
        raw_updates: list[tuple[str, str, str]] = []

        for row in raw_rows:
            snapshot = Path(row["snapshot_path"]).resolve()
            if (
                not snapshot.is_relative_to(self.raw_root)
                or not snapshot.name.endswith(".json.gz")
            ):
                skipped_unsafe += 1
                continue

            exists = snapshot.is_file()
            file_size = snapshot.stat().st_size if exists else 0
            if dry_run:
                if exists:
                    raw_deleted += 1
                    bytes_deleted += file_size
                else:
                    raw_missing += 1
                continue

            reason = "daily_compaction_24h_source_cleanup"
            if exists:
                snapshot.unlink()
                raw_deleted += 1
                bytes_deleted += file_size
                self._remove_empty_parents(snapshot.parent)
            else:
                raw_missing += 1
                reason = "daily_compaction_source_already_missing"
            raw_updates.append((utc_now_iso(), reason, str(row["run_id"])))
            if len(raw_updates) >= 1_000:
                self._record_raw_updates(raw_updates)
                raw_updates.clear()
                self._pause(batch_pause_seconds)

        if raw_updates:
            self._record_raw_updates(raw_updates)

        if detail_run_ids and not dry_run:
            links_deleted, observations_deleted = self._delete_staging(
                detail_run_ids,
                batch_pause_seconds=batch_pause_seconds,
            )

        run_history_deleted = 0
        checkpoint_busy = 0
        if not dry_run:
            history_cutoff = now - timedelta(days=self.settings.run_detail_days)
            run_history_deleted = self._purge_run_history(
                history_cutoff,
                max_rows=max_runs,
                batch_pause_seconds=batch_pause_seconds,
            )
            # PASSIVE never waits for live collector writers. The collector's
            # WAL autocheckpoint handles truncation when it can do so safely.
            checkpoint_busy, _, _ = self.storage.checkpoint(truncate=False)

        return RetentionOutcome(
            completed_operational_days=len(completed_days),
            eligible_through_operational_date=(
                max(completed_days) if completed_days else None
            ),
            candidate_runs=len(eligible_rows),
            raw_deleted=raw_deleted,
            raw_missing=raw_missing,
            provenance_links_deleted=links_deleted,
            observations_deleted=observations_deleted,
            skipped_uncompacted=skipped_uncompacted,
            skipped_unsafe_path=skipped_unsafe,
            bytes_deleted=bytes_deleted,
            dry_run=dry_run,
            skipped_raw_too_new=skipped_raw_too_new,
            run_history_deleted=run_history_deleted,
            wal_checkpoint_busy=checkpoint_busy,
        )

    def _valid_compacted_output(self, output_path: object) -> bool:
        if not output_path:
            return False
        compacted_root = (self.config.data_dir / "compacted").resolve()
        path = Path(str(output_path)).resolve()
        try:
            return (
                path.is_relative_to(compacted_root)
                and path.name.endswith(".json.gz")
                and path.is_file()
                and path.stat().st_size > 0
            )
        except OSError:
            return False

    @staticmethod
    def _pause(seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def _record_raw_updates(
        self, values: list[tuple[str, str, str]]
    ) -> None:
        with self.storage.connect() as connection:
            connection.executemany(
                """
                UPDATE collection_runs
                SET raw_deleted_at_utc = ?, raw_retention_reason = ?
                WHERE run_id = ?
                """,
                values,
            )

    def _staging_counts(self, run_ids: list[str]) -> tuple[int, int]:
        if not run_ids:
            return 0, 0
        with self.storage.connect() as connection:
            self._prepare_temp_tables(connection, run_ids)
            links = connection.execute(
                """
                SELECT COUNT(*)
                FROM collection_observations
                WHERE run_id IN (SELECT run_id FROM retention_runs)
                """
            ).fetchone()[0]
            observations = connection.execute(
                """
                SELECT COUNT(*)
                FROM retention_observations ro
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM collection_observations co
                    LEFT JOIN retention_runs rr ON rr.run_id = co.run_id
                    WHERE co.observation_id = ro.observation_id
                      AND rr.run_id IS NULL
                )
                """
            ).fetchone()[0]
        return int(links), int(observations)

    def _delete_staging(
        self,
        run_ids: list[str],
        *,
        batch_pause_seconds: float = 0.0,
    ) -> tuple[int, int]:
        # Keep transactions small even when the configured cleanup batch is
        # large; this limits the time the collector can wait on SQLite writes.
        batch_size = min(self.settings.delete_batch_size, 1_000)
        links_deleted = 0
        observations_deleted = 0
        with self.storage.connect() as connection:
            self._prepare_temp_tables(connection, run_ids)
            connection.commit()

            while True:
                connection.execute(
                    """
                    DELETE FROM collection_observations
                    WHERE rowid IN (
                        SELECT co.rowid
                        FROM retention_runs rr
                        CROSS JOIN collection_observations co
                            INDEXED BY sqlite_autoindex_collection_observations_1
                        WHERE co.run_id = rr.run_id
                        LIMIT ?
                    )
                    """,
                    (batch_size,),
                )
                changed = int(
                    connection.execute("SELECT changes()").fetchone()[0]
                )
                links_deleted += changed
                connection.commit()
                if changed < batch_size:
                    break
                self._pause(batch_pause_seconds)

            while True:
                connection.execute(
                    """
                    DELETE FROM observations
                    WHERE observation_id IN (
                        SELECT ro.observation_id
                        FROM retention_observations ro
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM collection_observations co
                            WHERE co.observation_id = ro.observation_id
                        )
                        LIMIT ?
                    )
                    """,
                    (batch_size,),
                )
                changed = int(
                    connection.execute("SELECT changes()").fetchone()[0]
                )
                observations_deleted += changed
                connection.commit()
                if changed < batch_size:
                    break
                self._pause(batch_pause_seconds)

            connection.execute(
                """
                UPDATE collection_runs
                SET details_deleted_at_utc = ?,
                    details_retention_reason = 'daily_compaction_source_cleanup'
                WHERE run_id IN (SELECT run_id FROM retention_runs)
                """,
                (utc_now_iso(),),
            )
        return links_deleted, observations_deleted

    def _purge_run_history(
        self,
        cutoff: datetime,
        *,
        max_rows: int | None = None,
        batch_pause_seconds: float = 0.0,
    ) -> int:
        deleted = 0
        cutoff_text = cutoff.isoformat(timespec="milliseconds")
        batch_size = min(self.settings.delete_batch_size, 1_000)
        with self.storage.connect() as connection:
            while True:
                if max_rows is not None and deleted >= max_rows:
                    break
                current_batch_size = batch_size
                if max_rows is not None:
                    current_batch_size = min(
                        current_batch_size, max_rows - deleted
                    )
                connection.execute(
                    """
                    DELETE FROM collection_runs
                    WHERE rowid IN (
                        SELECT rowid
                        FROM collection_runs
                        WHERE started_at_utc < ?
                          AND (
                              status = 'failed'
                              OR (
                                  status = 'completed'
                                  AND details_deleted_at_utc IS NOT NULL
                                  AND (
                                      snapshot_path IS NULL
                                      OR raw_deleted_at_utc IS NOT NULL
                                  )
                              )
                          )
                        LIMIT ?
                    )
                    """,
                    (cutoff_text, current_batch_size),
                )
                changed = int(
                    connection.execute("SELECT changes()").fetchone()[0]
                )
                deleted += changed
                connection.commit()
                if changed < current_batch_size:
                    break
                self._pause(batch_pause_seconds)
        return deleted

    @staticmethod
    def _prepare_temp_tables(
        connection: sqlite3.Connection, run_ids: list[str]
    ) -> None:
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS retention_runs (run_id TEXT PRIMARY KEY)"
        )
        connection.execute("DELETE FROM retention_runs")
        connection.executemany(
            "INSERT INTO retention_runs (run_id) VALUES (?)",
            ((run_id,) for run_id in run_ids),
        )
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS retention_observations (
                observation_id INTEGER PRIMARY KEY
            )
            """
        )
        connection.execute("DELETE FROM retention_observations")
        connection.execute(
            """
            INSERT INTO retention_observations (observation_id)
            SELECT DISTINCT co.observation_id
            FROM collection_observations co
            WHERE co.run_id IN (SELECT run_id FROM retention_runs)
            """
        )

    def _remove_empty_parents(self, directory: Path) -> None:
        current = directory.resolve()
        while current != self.raw_root and current.is_relative_to(self.raw_root):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


# Backwards-compatible import for code written before staging cleanup included
# normalized observations and provenance links.
RawRetention = StagingRetention
