from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterator
from uuid import uuid4

from .config import BBox, CollectionTarget
from .models import NormalizedObservation, normalize_observation, utc_now_iso
from .providers import FetchResult


WAL_JOURNAL_LIMIT_BYTES = 256 * 1024 * 1024


SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    area_id TEXT NOT NULL,
    tile_id TEXT NOT NULL,
    bbox TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    pages INTEGER,
    reported_count INTEGER,
    reported_count_delta INTEGER,
    item_count INTEGER,
    unique_item_count INTEGER,
    inserted_count INTEGER,
    duplicate_count INTEGER,
    within_run_duplicate_count INTEGER,
    outside_bbox_count INTEGER,
    elapsed_seconds REAL,
    snapshot_path TEXT,
    raw_deleted_at_utc TEXT,
    raw_retention_reason TEXT,
    details_deleted_at_utc TEXT,
    details_retention_reason TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    track_id TEXT,
    position_time_utc TEXT,
    reception_time_utc TEXT,
    first_fetched_at_utc TEXT NOT NULL,
    last_fetched_at_utc TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    lat REAL,
    lon REAL,
    speed_ground REAL,
    course_ground REAL,
    is_moving INTEGER,
    vessel_name TEXT,
    call_sign TEXT,
    mmsi INTEGER,
    eni TEXT,
    imo INTEGER,
    length_m REAL,
    beam_m REAL,
    ais_ship_type INTEGER,
    eri_ship_type INTEGER,
    isrs_code TEXT,
    isrs_name TEXT,
    direction INTEGER,
    privacy_class INTEGER,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_observations (
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
    observation_id INTEGER NOT NULL REFERENCES observations(observation_id),
    inside_requested_bbox INTEGER,
    PRIMARY KEY (run_id, observation_id)
);

CREATE TABLE IF NOT EXISTS daily_compaction_runs (
    operational_date TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    day_start_utc TEXT NOT NULL,
    day_end_utc TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    source_sample_count INTEGER,
    track_count INTEGER,
    output_record_count INTEGER,
    position_record_count INTEGER,
    stationary_record_count INTEGER,
    stationary_source_sample_count INTEGER,
    output_path TEXT,
    settings_json TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS daily_track_records (
    operational_date TEXT NOT NULL
        REFERENCES daily_compaction_runs(operational_date) ON DELETE CASCADE,
    track_key TEXT NOT NULL,
    track_id TEXT,
    sequence_no INTEGER NOT NULL,
    record_type TEXT NOT NULL CHECK (record_type IN ('position', 'stationary')),
    started_at_utc TEXT NOT NULL,
    ended_at_utc TEXT NOT NULL,
    first_position_time_utc TEXT,
    last_position_time_utc TEXT,
    representative_lat REAL,
    representative_lon REAL,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    duration_seconds REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    unique_observation_count INTEGER NOT NULL,
    max_radius_m REAL,
    source_observation_id INTEGER,
    speed_ground REAL,
    course_ground REAL,
    source_is_moving INTEGER,
    vessel_name TEXT,
    call_sign TEXT,
    mmsi INTEGER,
    eni TEXT,
    imo INTEGER,
    length_m REAL,
    beam_m REAL,
    ais_ship_type INTEGER,
    eri_ship_type INTEGER,
    isrs_code TEXT,
    isrs_name TEXT,
    direction INTEGER,
    privacy_class INTEGER,
    PRIMARY KEY (operational_date, track_key, sequence_no)
);

CREATE TABLE IF NOT EXISTS collection_period_stats (
    granularity TEXT NOT NULL
        CHECK (granularity IN ('minute', 'hour', 'day')),
    period_start_utc TEXT NOT NULL,
    period_end_utc TEXT NOT NULL,
    period_label_local TEXT NOT NULL,
    timezone TEXT NOT NULL,
    operational_date TEXT,
    computed_at_utc TEXT NOT NULL,
    is_final INTEGER NOT NULL,
    details_complete INTEGER NOT NULL,
    expected_target_count INTEGER NOT NULL,
    expected_run_count INTEGER NOT NULL,
    observed_run_count INTEGER NOT NULL,
    completed_run_count INTEGER NOT NULL,
    failed_run_count INTEGER NOT NULL,
    running_run_count INTEGER NOT NULL,
    tiles_seen INTEGER NOT NULL,
    received_item_count INTEGER NOT NULL,
    unique_item_count INTEGER NOT NULL,
    distinct_observation_count INTEGER,
    distinct_track_count INTEGER,
    new_observation_count INTEGER NOT NULL,
    existing_observation_count INTEGER NOT NULL,
    within_run_duplicate_count INTEGER NOT NULL,
    outside_bbox_count INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    pagination_anomaly_run_count INTEGER NOT NULL,
    total_elapsed_seconds REAL NOT NULL,
    average_elapsed_seconds REAL,
    p95_elapsed_seconds REAL,
    max_elapsed_seconds REAL,
    PRIMARY KEY (granularity, period_start_utc)
);

CREATE TABLE IF NOT EXISTS daily_collection_summaries (
    operational_date TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    day_start_utc TEXT NOT NULL,
    day_end_utc TEXT NOT NULL,
    generated_at_utc TEXT NOT NULL,
    health_status TEXT NOT NULL
        CHECK (
            health_status IN (
                'healthy', 'warning', 'critical', 'partial', 'no_data'
            )
        ),
    summary_text TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    output_path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_track_time
    ON observations (track_id, position_time_utc);
CREATE INDEX IF NOT EXISTS idx_observations_position_time
    ON observations (position_time_utc);
CREATE INDEX IF NOT EXISTS idx_observations_lat_lon
    ON observations (lat, lon);
CREATE INDEX IF NOT EXISTS idx_collection_observations_observation_id
    ON collection_observations (observation_id);
CREATE INDEX IF NOT EXISTS idx_runs_area_started
    ON collection_runs (area_id, started_at_utc);
CREATE INDEX IF NOT EXISTS idx_daily_records_track_time
    ON daily_track_records (track_id, started_at_utc);
CREATE INDEX IF NOT EXISTS idx_daily_records_type_date
    ON daily_track_records (record_type, operational_date);
CREATE INDEX IF NOT EXISTS idx_period_stats_granularity_start
    ON collection_period_stats (granularity, period_start_utc);
CREATE INDEX IF NOT EXISTS idx_runs_started
    ON collection_runs (started_at_utc);
CREATE INDEX IF NOT EXISTS idx_runs_raw_deleted
    ON collection_runs (raw_deleted_at_utc);
"""


class Storage:
    def __init__(self, database: Path, raw_dir: Path) -> None:
        self.database = database
        self.raw_dir = raw_dir

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
        connection.execute(f"PRAGMA journal_size_limit={WAL_JOURNAL_LIMIT_BYTES}")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, *, backfill_bbox_flags: bool = True) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_columns(connection)
            self._ensure_daily_summary_health_values(connection)
            if backfill_bbox_flags:
                self._backfill_bbox_flags(connection)

    @staticmethod
    def _ensure_daily_summary_health_values(
        connection: sqlite3.Connection,
    ) -> None:
        row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'daily_collection_summaries'
            """
        ).fetchone()
        if row is None or "'partial'" in str(row["sql"]):
            return
        connection.execute(
            "ALTER TABLE daily_collection_summaries RENAME TO daily_collection_summaries_old"
        )
        connection.execute(
            """
            CREATE TABLE daily_collection_summaries (
                operational_date TEXT PRIMARY KEY,
                timezone TEXT NOT NULL,
                day_start_utc TEXT NOT NULL,
                day_end_utc TEXT NOT NULL,
                generated_at_utc TEXT NOT NULL,
                health_status TEXT NOT NULL
                    CHECK (
                        health_status IN (
                            'healthy', 'warning', 'critical', 'partial', 'no_data'
                        )
                    ),
                summary_text TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                output_path TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO daily_collection_summaries
            SELECT * FROM daily_collection_summaries_old
            """
        )
        connection.execute("DROP TABLE daily_collection_summaries_old")

    def checkpoint(self, *, truncate: bool = False) -> tuple[int, int, int]:
        mode = "TRUNCATE" if truncate else "PASSIVE"
        with self.connect() as connection:
            row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        run_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(collection_runs)")
        }
        run_additions = {
            "unique_item_count": "INTEGER",
            "within_run_duplicate_count": "INTEGER",
            "reported_count_delta": "INTEGER",
            "outside_bbox_count": "INTEGER",
            "raw_deleted_at_utc": "TEXT",
            "raw_retention_reason": "TEXT",
            "details_deleted_at_utc": "TEXT",
            "details_retention_reason": "TEXT",
        }
        for name, declaration in run_additions.items():
            if name not in run_columns:
                connection.execute(
                    f"ALTER TABLE collection_runs ADD COLUMN {name} {declaration}"
                )

        link_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(collection_observations)"
            )
        }
        if "inside_requested_bbox" not in link_columns:
            connection.execute(
                """
                ALTER TABLE collection_observations
                ADD COLUMN inside_requested_bbox INTEGER
                """
            )

    @staticmethod
    def _backfill_bbox_flags(connection: sqlite3.Connection) -> None:
        runs = connection.execute(
            """
            SELECT DISTINCT r.run_id, r.bbox
            FROM collection_runs r
            JOIN collection_observations co ON co.run_id = r.run_id
            WHERE co.inside_requested_bbox IS NULL
            """
        ).fetchall()
        for run in runs:
            try:
                min_lon, min_lat, max_lon, max_lat = (
                    float(value) for value in run["bbox"].split(",")
                )
            except (AttributeError, TypeError, ValueError):
                continue
            connection.execute(
                """
                UPDATE collection_observations
                SET inside_requested_bbox = CASE
                    WHEN (
                        SELECT o.lon BETWEEN ? AND ?
                           AND o.lat BETWEEN ? AND ?
                        FROM observations o
                        WHERE o.observation_id =
                              collection_observations.observation_id
                    ) THEN 1 ELSE 0 END
                WHERE run_id = ?
                """,
                (min_lon, max_lon, min_lat, max_lat, run["run_id"]),
            )
            connection.execute(
                """
                UPDATE collection_runs
                SET outside_bbox_count = (
                    SELECT COUNT(*)
                    FROM collection_observations
                    WHERE run_id = ? AND inside_requested_bbox = 0
                )
                WHERE run_id = ?
                """,
                (run["run_id"], run["run_id"]),
            )

    def start_run(self, provider: str, target: CollectionTarget) -> str:
        run_id = str(uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_runs (
                    run_id, provider, area_id, tile_id, bbox,
                    started_at_utc, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    run_id,
                    provider,
                    target.area_id,
                    target.tile_id,
                    target.bbox.compact(),
                    utc_now_iso(),
                ),
            )
        return run_id

    def fail_run(self, run_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE collection_runs
                SET completed_at_utc = ?, status = 'failed', error = ?
                WHERE run_id = ?
                """,
                (utc_now_iso(), error[:4000], run_id),
            )

    def recover_stale_runs(self, max_age_seconds: int = 900) -> int:
        """Mark abandoned in-flight runs as failed after a safe grace period."""
        if max_age_seconds < 1:
            raise ValueError("max_age_seconds must be positive")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat(timespec="milliseconds")
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE collection_runs
                SET completed_at_utc = ?,
                    status = 'failed',
                    error = CASE
                        WHEN error IS NULL OR error = ''
                        THEN 'Recovered stale running record after process restart'
                        ELSE error
                    END
                WHERE status = 'running' AND started_at_utc < ?
                """,
                (now, cutoff),
            )
            return int(cursor.rowcount)

    def write_snapshot(
        self,
        provider: str,
        target: CollectionTarget,
        run_id: str,
        result: FetchResult,
    ) -> Path:
        timestamp = result.fetched_at_utc.replace("-", "").replace(":", "")
        timestamp = timestamp.replace("+0000", "Z").replace("+00:00", "Z")
        timestamp = timestamp.replace(".", "_")
        directory = (
            self.raw_dir
            / provider
            / result.fetched_at_utc[0:4]
            / result.fetched_at_utc[5:7]
            / result.fetched_at_utc[8:10]
            / target.area_id
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{timestamp}_{target.tile_id}_{run_id[:8]}.json.gz"
        payload: dict[str, Any] = {
            "metadata": {
                "run_id": run_id,
                "provider": provider,
                "area_id": target.area_id,
                "tile_id": target.tile_id,
                "bbox": asdict(target.bbox),
                "fetched_at_utc": result.fetched_at_utc,
                "pages": result.pages,
                "reported_count": result.reported_count,
                "reported_count_delta": result.reported_count_delta,
                "item_count": len(result.items),
                "elapsed_seconds": result.elapsed_seconds,
                "source_url": result.source_url,
                "attribution": (
                    "API/Service Tracks incorporated from EuRIS (eurisportal.eu)"
                ),
            },
            "items": result.items,
        }

        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=".snapshot_", suffix=".tmp"
        )
        try:
            with os.fdopen(file_descriptor, "wb") as raw_file:
                with gzip.GzipFile(fileobj=raw_file, mode="wb", compresslevel=6) as gz:
                    gz.write(
                        json.dumps(
                            payload, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return path

    def ingest(
        self,
        run_id: str,
        provider: str,
        target: CollectionTarget,
        result: FetchResult,
        snapshot_path: Path,
    ) -> tuple[int, int, int]:
        observations = [
            normalize_observation(item, provider, result.fetched_at_utc)
            for item in result.items
        ]
        unique_observations = {
            observation.observation_key: observation for observation in observations
        }
        within_run_duplicates = len(observations) - len(unique_observations)
        outside_bbox_count = sum(
            1
            for observation in unique_observations.values()
            if not self._inside_bbox(observation, target)
        )
        inserted = 0
        existing = 0

        with self.connect() as connection:
            for observation in unique_observations.values():
                was_inserted, observation_id = self._upsert_observation(
                    connection, observation
                )
                if was_inserted:
                    inserted += 1
                else:
                    existing += 1
                connection.execute(
                    """
                    INSERT OR IGNORE INTO collection_observations (
                        run_id, observation_id, inside_requested_bbox
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        run_id,
                        observation_id,
                        int(self._inside_bbox(observation, target)),
                    ),
                )

            connection.execute(
                """
                UPDATE collection_runs
                SET completed_at_utc = ?, status = 'completed', pages = ?,
                    reported_count = ?, reported_count_delta = ?,
                    item_count = ?, unique_item_count = ?, inserted_count = ?,
                    duplicate_count = ?, within_run_duplicate_count = ?,
                    outside_bbox_count = ?, elapsed_seconds = ?,
                    snapshot_path = ?
                WHERE run_id = ?
                """,
                (
                    utc_now_iso(),
                    result.pages,
                    result.reported_count,
                    result.reported_count_delta,
                    len(result.items),
                    len(unique_observations),
                    inserted,
                    existing,
                    within_run_duplicates,
                    outside_bbox_count,
                    result.elapsed_seconds,
                    str(snapshot_path),
                    run_id,
                ),
            )
        return inserted, existing, within_run_duplicates

    @staticmethod
    def _inside_bbox(
        observation: NormalizedObservation, target: CollectionTarget
    ) -> bool:
        if observation.lon is None or observation.lat is None:
            return False
        return (
            target.bbox.min_lon <= observation.lon <= target.bbox.max_lon
            and target.bbox.min_lat <= observation.lat <= target.bbox.max_lat
        )

    @staticmethod
    def _upsert_observation(
        connection: sqlite3.Connection, observation: NormalizedObservation
    ) -> tuple[bool, int]:
        values = asdict(observation)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO observations (
                observation_key, provider, track_id, position_time_utc,
                reception_time_utc, first_fetched_at_utc, last_fetched_at_utc,
                lat, lon, speed_ground, course_ground, is_moving, vessel_name,
                call_sign, mmsi, eni, imo, length_m, beam_m, ais_ship_type,
                eri_ship_type, isrs_code, isrs_name, direction, privacy_class,
                raw_json
            ) VALUES (
                :observation_key, :provider, :track_id, :position_time_utc,
                :reception_time_utc, :fetched_at_utc, :fetched_at_utc,
                :lat, :lon, :speed_ground, :course_ground, :is_moving,
                :vessel_name, :call_sign, :mmsi, :eni, :imo, :length_m,
                :beam_m, :ais_ship_type, :eri_ship_type, :isrs_code,
                :isrs_name, :direction, :privacy_class, :raw_json
            )
            """,
            values,
        )
        was_inserted = cursor.rowcount == 1
        if not was_inserted:
            connection.execute(
                """
                UPDATE observations
                SET last_fetched_at_utc = ?, seen_count = seen_count + 1
                WHERE observation_key = ?
                """,
                (observation.fetched_at_utc, observation.observation_key),
            )

        row = connection.execute(
            "SELECT observation_id FROM observations WHERE observation_key = ?",
            (observation.observation_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Observation upsert did not return an id")
        return was_inserted, int(row["observation_id"])

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT area_id, tile_id, status, started_at_utc, item_count,
                           unique_item_count, inserted_count, duplicate_count,
                           within_run_duplicate_count, reported_count_delta,
                           outside_bbox_count, pages, error
                    FROM collection_runs
                    ORDER BY started_at_utc DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def counts(self) -> dict[str, int]:
        self.initialize()
        with self.connect() as connection:
            return {
                "runs": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM collection_runs"
                    ).fetchone()[0]
                ),
                "observations": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM observations"
                    ).fetchone()[0]
                ),
                "tracks": int(
                    connection.execute(
                        "SELECT COUNT(DISTINCT track_id) FROM observations"
                    ).fetchone()[0]
                ),
                "compacted_days": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM daily_compaction_runs
                        WHERE status = 'completed'
                        """
                    ).fetchone()[0]
                ),
                "compacted_records": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM daily_track_records"
                    ).fetchone()[0]
                ),
                "raw_deleted": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM collection_runs
                        WHERE raw_deleted_at_utc IS NOT NULL
                        """
                    ).fetchone()[0]
                ),
                "details_deleted_runs": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM collection_runs
                        WHERE details_deleted_at_utc IS NOT NULL
                        """
                    ).fetchone()[0]
                ),
            }

    def recent_compactions(self, limit: int = 10) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT operational_date, status, source_sample_count,
                           output_record_count, position_record_count,
                           stationary_record_count,
                           stationary_source_sample_count, output_path, error
                    FROM daily_compaction_runs
                    ORDER BY operational_date DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def aggregate_runs(self, run_ids: list[str], area_bbox: BBox) -> dict[str, int]:
        if not run_ids:
            return {"distinct_observations": 0, "inside_area_bbox": 0}
        placeholders = ",".join("?" for _ in run_ids)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT co.observation_id) AS distinct_observations,
                    COUNT(DISTINCT CASE
                        WHEN o.lon BETWEEN ? AND ?
                         AND o.lat BETWEEN ? AND ?
                        THEN co.observation_id
                    END) AS inside_area_bbox
                FROM collection_observations co
                JOIN observations o
                  ON o.observation_id = co.observation_id
                WHERE co.run_id IN ({placeholders})
                """,
                (
                    area_bbox.min_lon,
                    area_bbox.max_lon,
                    area_bbox.min_lat,
                    area_bbox.max_lat,
                    *run_ids,
                ),
            ).fetchone()
        return {
            "distinct_observations": int(row["distinct_observations"]),
            "inside_area_bbox": int(row["inside_area_bbox"]),
        }
