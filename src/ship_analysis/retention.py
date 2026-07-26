from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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


class StagingRetention:
    """Remove rebuildable source/staging data after daily compaction succeeds."""

    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.settings = config.retention
        self.storage = storage
        self.raw_root = config.raw_dir.resolve()

    def prune(self, *, dry_run: bool = False) -> RetentionOutcome:
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

        self.storage.initialize()
        with self.storage.connect() as connection:
            completed_days = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT operational_date
                    FROM daily_compaction_runs
                    WHERE status = 'completed'
                    """
                )
            }
            rows = connection.execute(
                """
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
            ).fetchall()

        eligible_rows = []
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

        run_ids = [row["run_id"] for row in eligible_rows]
        links_deleted, observations_deleted = self._staging_counts(run_ids)
        raw_deleted = 0
        raw_missing = 0
        skipped_unsafe = 0
        bytes_deleted = 0

        for row in eligible_rows:
            if row["snapshot_path"] is None or row["raw_deleted_at_utc"] is not None:
                continue
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

            reason = "daily_compaction_source_cleanup"
            if exists:
                snapshot.unlink()
                raw_deleted += 1
                bytes_deleted += file_size
                self._remove_empty_parents(snapshot.parent)
            else:
                raw_missing += 1
                reason = "daily_compaction_file_already_missing"

            with self.storage.connect() as connection:
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET raw_deleted_at_utc = ?, raw_retention_reason = ?
                    WHERE run_id = ?
                    """,
                    (utc_now_iso(), reason, row["run_id"]),
                )

        if run_ids and not dry_run:
            links_deleted, observations_deleted = self._delete_staging(run_ids)

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
        )

    def _staging_counts(self, run_ids: list[str]) -> tuple[int, int]:
        if not run_ids:
            return 0, 0
        with self.storage.connect() as connection:
            self._prepare_temp_tables(connection, run_ids)
            links = connection.execute(
                """
                SELECT COUNT(*)
                FROM collection_observations co
                JOIN retention_runs rr ON rr.run_id = co.run_id
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

    def _delete_staging(self, run_ids: list[str]) -> tuple[int, int]:
        with self.storage.connect() as connection:
            self._prepare_temp_tables(connection, run_ids)
            connection.execute(
                """
                DELETE FROM collection_observations
                WHERE run_id IN (SELECT run_id FROM retention_runs)
                """
            )
            links_deleted = connection.execute("SELECT changes()").fetchone()[0]
            connection.execute(
                """
                DELETE FROM observations
                WHERE observation_id IN (
                    SELECT observation_id FROM retention_observations
                )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM collection_observations co
                      WHERE co.observation_id = observations.observation_id
                  )
                """
            )
            observations_deleted = connection.execute(
                "SELECT changes()"
            ).fetchone()[0]
            now = utc_now_iso()
            connection.execute(
                """
                UPDATE collection_runs
                SET details_deleted_at_utc = ?,
                    details_retention_reason = 'daily_compaction_source_cleanup'
                WHERE run_id IN (SELECT run_id FROM retention_runs)
                """,
                (now,),
            )
        return int(links_deleted), int(observations_deleted)

    @staticmethod
    def _prepare_temp_tables(connection, run_ids: list[str]) -> None:
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
            JOIN retention_runs rr ON rr.run_id = co.run_id
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

