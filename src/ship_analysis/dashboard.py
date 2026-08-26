from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import time
from typing import Any

from .compaction import operational_date_for, operational_day_bounds
from .config import AppConfig
from .reporting import CollectionReporter
from .storage import Storage


UTC = timezone.utc
STORAGE_SCAN_TTL_SECONDS = 300


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _seconds_since(value: str | None, now: datetime) -> int | None:
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except FileNotFoundError:
                continue
    return total


def _aggregate_runs(
    connection: Any,
    start: datetime,
    end: datetime,
    expected_runs: int,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS observed,
            COALESCE(SUM(status = 'completed'), 0) AS completed,
            COALESCE(SUM(status = 'failed'), 0) AS failed,
            COALESCE(SUM(status = 'running'), 0) AS running,
            COALESCE(SUM(CASE WHEN status = 'completed'
                THEN item_count ELSE 0 END), 0) AS received,
            COALESCE(SUM(CASE WHEN status = 'completed'
                THEN inserted_count ELSE 0 END), 0) AS new_items,
            COALESCE(SUM(CASE WHEN status = 'completed'
                THEN duplicate_count ELSE 0 END), 0) AS existing,
            COALESCE(SUM(CASE WHEN status = 'completed'
                AND COALESCE(reported_count_delta, 0) <> 0
                THEN 1 ELSE 0 END), 0) AS pagination_changes,
            AVG(CASE WHEN status = 'completed'
                THEN elapsed_seconds END) AS average_seconds,
            MAX(CASE WHEN status = 'completed'
                THEN elapsed_seconds END) AS max_seconds
        FROM collection_runs
        WHERE started_at_utc >= ? AND started_at_utc < ?
        """,
        (_iso_utc(start), _iso_utc(end)),
    ).fetchone()
    completed = int(row["completed"])
    failed = int(row["failed"])
    attempted = completed + failed
    return {
        "periodStart": _iso_utc(start),
        "periodEnd": _iso_utc(end),
        "received": int(row["received"]),
        "new": int(row["new_items"]),
        "existing": int(row["existing"]),
        "observedRuns": int(row["observed"]),
        "completedRuns": completed,
        "failedRuns": failed,
        "runningRuns": int(row["running"]),
        "expectedRuns": expected_runs,
        "completionRate": (
            min(1.0, completed / expected_runs) if expected_runs else 0.0
        ),
        "requestSuccessRate": completed / attempted if attempted else 0.0,
        "paginationChanges": int(row["pagination_changes"]),
        "averageSeconds": (
            float(row["average_seconds"])
            if row["average_seconds"] is not None
            else None
        ),
        "maxSeconds": (
            float(row["max_seconds"])
            if row["max_seconds"] is not None
            else None
        ),
    }


class DashboardSnapshot:
    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self.reporter = CollectionReporter(config, storage)
        self.zone = self.reporter.zone
        self._storage_cache: tuple[float, dict[str, int | float]] | None = None

    def _storage_payload(
        self, logical_database_bytes: int
    ) -> dict[str, int | float]:
        now = time.monotonic()
        if (
            self._storage_cache is not None
            and now - self._storage_cache[0] < STORAGE_SCAN_TTL_SECONDS
        ):
            cached = dict(self._storage_cache[1])
            cached["logicalDatabaseBytes"] = logical_database_bytes
            return cached

        database_bytes = _file_size(self.config.database)
        wal_bytes = _file_size(Path(f"{self.config.database}-wal"))
        shm_bytes = _file_size(Path(f"{self.config.database}-shm"))
        raw_bytes = _directory_size(self.config.raw_dir)
        archive_bytes = _directory_size(self.config.data_dir / "compacted")
        summary_bytes = _directory_size(self.config.data_dir / "summaries")
        log_bytes = _directory_size(self.config.data_dir / "logs")
        data_bytes = (
            database_bytes
            + wal_bytes
            + shm_bytes
            + raw_bytes
            + archive_bytes
            + summary_bytes
            + log_bytes
        )
        disk_root = (
            self.config.data_dir
            if self.config.data_dir.exists()
            else self.config.project_root
        )
        disk = shutil.disk_usage(disk_root)
        payload: dict[str, int | float] = {
            "dataBytes": data_bytes,
            "databaseBytes": database_bytes,
            "logicalDatabaseBytes": logical_database_bytes,
            "walBytes": wal_bytes,
            "rawBytes": raw_bytes,
            "archiveBytes": archive_bytes,
            "logBytes": log_bytes,
            "diskTotalBytes": disk.total,
            "diskFreeBytes": disk.free,
            "diskUsedPercent": (
                (disk.total - disk.free) / disk.total if disk.total else 0.0
            ),
            "rawRetentionHours": self.config.retention.raw_min_age_hours,
            "runDetailDays": self.config.retention.run_detail_days,
        }
        self._storage_cache = (now, payload)
        return dict(payload)

    def build(self, now_utc: datetime | None = None) -> dict[str, Any]:
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        rolling_hour_start = now - timedelta(hours=1)
        operational_date = operational_date_for(now, self.config.compaction)
        day_start, day_end = operational_day_bounds(
            operational_date, self.config.compaction
        )

        with self.storage.connect() as connection:
            latest_run = connection.execute(
                """
                SELECT started_at_utc, completed_at_utc, status, tile_id,
                       item_count, error
                FROM collection_runs
                ORDER BY started_at_utc DESC
                LIMIT 1
                """
            ).fetchone()
            running_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM collection_runs WHERE status = 'running'"
                ).fetchone()[0]
            )
            latest_failed = connection.execute(
                """
                SELECT started_at_utc, tile_id, error
                FROM collection_runs
                WHERE status = 'failed'
                ORDER BY started_at_utc DESC
                LIMIT 1
                """
            ).fetchone()
            first_started = connection.execute(
                "SELECT MIN(started_at_utc) FROM collection_runs"
            ).fetchone()[0]
            last_hour = _aggregate_runs(
                connection,
                rolling_hour_start,
                now,
                self.reporter._expected_runs(rolling_hour_start, now),
            )
            operating_day = _aggregate_runs(
                connection,
                day_start,
                min(now, day_end),
                self.reporter._expected_runs(day_start, min(now, day_end)),
            )
            timeline = self._hourly_timeline(connection, now)
            final_summary = connection.execute(
                """
                SELECT operational_date, generated_at_utc, health_status,
                       summary_json
                FROM daily_collection_summaries
                ORDER BY operational_date DESC
                LIMIT 1
                """
            ).fetchone()
            summary_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM daily_collection_summaries"
                ).fetchone()[0]
            )
            compaction = connection.execute(
                """
                SELECT operational_date, status, source_sample_count,
                       output_record_count, completed_at_utc, error
                FROM daily_compaction_runs
                ORDER BY operational_date DESC
                LIMIT 1
                """
            ).fetchone()
            last_cleanup_at = connection.execute(
                """
                SELECT MAX(COALESCE(raw_deleted_at_utc, details_deleted_at_utc))
                FROM collection_runs
                """
            ).fetchone()[0]
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])

        last_started = latest_run["started_at_utc"] if latest_run else None
        freshness = _seconds_since(last_started, now)
        observation_window_seconds = _seconds_since(first_started, now) or 0
        if latest_run is None:
            collector_status = "no_data"
        elif freshness is not None and freshness > 180:
            collector_status = "stopped"
        elif running_count:
            collector_status = "collecting"
        else:
            collector_status = "online"

        storage_payload = self._storage_payload(page_size * page_count)
        summary_payload = self._summary_payload(final_summary, summary_count)
        maintenance = {
            "lastCleanupAt": last_cleanup_at,
            "compaction": dict(compaction) if compaction else None,
        }
        health = self._health_payload(
            collector_status=collector_status,
            freshness=freshness,
            observation_window_seconds=observation_window_seconds,
            last_hour=last_hour,
            storage=storage_payload,
            summary=summary_payload,
            compaction=maintenance["compaction"],
        )

        return {
            "schemaVersion": 2,
            "mode": "live-local",
            "generatedAt": _iso_utc(now),
            "timezone": self.config.compaction.timezone,
            "health": health,
            "collector": {
                "status": collector_status,
                "runningRequests": running_count,
                "lastRunAt": last_started,
                "freshnessSeconds": freshness,
                "observationWindowSeconds": observation_window_seconds,
                "latestTile": latest_run["tile_id"] if latest_run else None,
                "latestItems": int(latest_run["item_count"] or 0)
                if latest_run
                else 0,
                "latestError": latest_run["error"] if latest_run else None,
                "lastFailedAt": (
                    latest_failed["started_at_utc"] if latest_failed else None
                ),
                "lastFailedTile": (
                    latest_failed["tile_id"] if latest_failed else None
                ),
                "lastFailedError": (
                    latest_failed["error"] if latest_failed else None
                ),
                "targetCount": len(self.config.targets()),
                "intervalSeconds": min(
                    (target.interval_seconds for target in self.config.targets()),
                    default=0,
                ),
            },
            "volume": {
                "lastHour": last_hour,
                "operatingDay": operating_day,
                "hourly": timeline,
            },
            "storage": storage_payload,
            "maintenance": maintenance,
            "latestDailySummary": summary_payload,
        }

    def _hourly_timeline(
        self, connection: Any, now: datetime
    ) -> list[dict[str, Any]]:
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        first_hour = current_hour - timedelta(hours=23)
        rows = connection.execute(
            """
            SELECT substr(started_at_utc, 1, 13) AS period_key,
                   COALESCE(SUM(CASE WHEN status = 'completed'
                       THEN item_count ELSE 0 END), 0) AS received,
                   COALESCE(SUM(CASE WHEN status = 'completed'
                       THEN inserted_count ELSE 0 END), 0) AS new_items,
                   COALESCE(SUM(status = 'completed'), 0) AS completed,
                   COALESCE(SUM(status = 'failed'), 0) AS failed
            FROM collection_runs
            WHERE started_at_utc >= ? AND started_at_utc < ?
            GROUP BY substr(started_at_utc, 1, 13)
            """,
            (_iso_utc(first_hour), _iso_utc(current_hour + timedelta(hours=1))),
        ).fetchall()
        by_key = {str(row["period_key"]): row for row in rows}
        result: list[dict[str, Any]] = []
        cursor = first_hour
        while cursor <= current_hour:
            key = _iso_utc(cursor)[:13]
            row = by_key.get(key)
            result.append(
                {
                    "periodStart": _iso_utc(cursor),
                    "label": cursor.astimezone(self.zone).strftime("%H:%M"),
                    "received": int(row["received"] or 0) if row else 0,
                    "new": int(row["new_items"] or 0) if row else 0,
                    "completedRuns": int(row["completed"] or 0) if row else 0,
                    "failedRuns": int(row["failed"] or 0) if row else 0,
                }
            )
            cursor += timedelta(hours=1)
        return result

    @staticmethod
    def _summary_payload(
        row: Any, summary_count: int
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        metrics = json.loads(row["summary_json"])
        health = str(row["health_status"])
        coverage = metrics.get("coverage", {})
        active = int(coverage.get("active_collection_minutes", 0))
        expected = int(coverage.get("expected_minutes", 0))
        if (
            summary_count == 1
            and expected > 0
            and active < expected * 0.95
            and health == "critical"
        ):
            health = "partial"
        return {
            "operationalDate": row["operational_date"],
            "generatedAt": row["generated_at_utc"],
            "healthStatus": health,
            "received": int(
                metrics.get("collection", {}).get("received_item_count", 0)
            ),
            "new": int(
                metrics.get("collection", {}).get("new_observation_count", 0)
            ),
            "activeMinutes": active,
            "expectedMinutes": expected,
        }

    @staticmethod
    def _health_payload(
        *,
        collector_status: str,
        freshness: int | None,
        observation_window_seconds: int,
        last_hour: dict[str, Any],
        storage: dict[str, int | float],
        summary: dict[str, Any] | None,
        compaction: dict[str, Any] | None,
    ) -> dict[str, Any]:
        reasons: list[dict[str, str]] = []

        def add(severity: str, code: str, message: str) -> None:
            reasons.append(
                {"severity": severity, "code": code, "message": message}
            )

        if collector_status == "no_data":
            add("info", "no_data", "No collection run has been recorded yet.")
        elif freshness is not None and freshness > 600:
            add(
                "critical",
                "collector_stale",
                f"No collection activity for {freshness // 60} minutes.",
            )
        elif freshness is not None and freshness > 180:
            add(
                "warning",
                "collector_delayed",
                f"No collection activity for {freshness // 60} minutes.",
            )

        warming_up = (
            collector_status != "no_data" and observation_window_seconds < 3600
        )
        if warming_up:
            add(
                "info",
                "startup_window",
                "Collecting the first full hour before judging schedule completion.",
            )

        if (
            not warming_up
            and last_hour["expectedRuns"] > 0
            and last_hour["observedRuns"] > 0
        ):
            if last_hour["completionRate"] < 0.90:
                add(
                    "critical",
                    "schedule_completion",
                    "Last-hour schedule completion is below 90%.",
                )
            elif last_hour["completionRate"] < 0.98:
                add(
                    "warning",
                    "schedule_completion",
                    "Last-hour schedule completion is below 98%.",
                )
        if last_hour["completedRuns"] + last_hour["failedRuns"] > 0:
            if last_hour["requestSuccessRate"] < 0.95:
                add(
                    "critical",
                    "request_failures",
                    "Last-hour request success is below 95%.",
                )
            elif last_hour["requestSuccessRate"] < 0.99:
                add(
                    "warning",
                    "request_failures",
                    "Last-hour request success is below 99%.",
                )

        total = int(storage["diskTotalBytes"])
        free = int(storage["diskFreeBytes"])
        free_ratio = free / total if total else 1.0
        if free_ratio < 0.10:
            add("critical", "disk_space", "Less than 10% disk space remains.")
        elif free_ratio < 0.20:
            add("warning", "disk_space", "Less than 20% disk space remains.")

        wal = int(storage["walBytes"])
        logical = int(storage["logicalDatabaseBytes"])
        if wal > max(5 * 1024**3, logical * 5):
            add(
                "critical",
                "wal_size",
                "SQLite WAL is more than five times the logical database size.",
            )
        elif wal > max(1024**3, logical * 2):
            add(
                "warning",
                "wal_size",
                "SQLite WAL is more than twice the logical database size.",
            )

        if compaction and compaction.get("status") == "failed":
            add(
                "critical",
                "daily_compaction",
                "The latest daily compaction failed.",
            )
        if summary and summary.get("healthStatus") == "critical":
            add(
                "warning",
                "daily_summary",
                "The latest completed operating day needs attention.",
            )

        severities = {reason["severity"] for reason in reasons}
        if "critical" in severities:
            status = "critical"
            headline = "Collection needs immediate attention"
        elif "warning" in severities:
            status = "warning"
            headline = "Collection is running with warnings"
        elif collector_status == "no_data":
            status = "no_data"
            headline = "Waiting for collection data"
        elif warming_up:
            status = "partial"
            headline = "Collection is establishing its first-hour baseline"
        else:
            status = "healthy"
            headline = "Collection is healthy"
        return {
            "status": status,
            "headline": headline,
            "hasAnomaly": status in {"warning", "critical"},
            "reasons": reasons,
        }


def export_dashboard_snapshot(
    config: AppConfig,
    storage: Storage,
    output: Path,
    *,
    mode: str = "static-snapshot",
) -> Path:
    payload = DashboardSnapshot(config, storage).build()
    payload["mode"] = mode
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def serve_dashboard_api(
    config: AppConfig,
    storage: Storage,
    *,
    host: str,
    port: int,
) -> None:
    storage.initialize()
    snapshot = DashboardSnapshot(config, storage)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ShipAnalysisDashboard/2.0"

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/health":
                self._send_json({"status": "ok"})
                return
            if self.path.rstrip("/") != "/api/dashboard":
                self.send_error(404, "Not found")
                return
            try:
                self._send_json(snapshot.build())
            except Exception as error:
                self._send_json(
                    {
                        "status": "error",
                        "error": f"{type(error).__name__}: {error}",
                    },
                    status=500,
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(
            self, payload: dict[str, Any], status: int = 200
        ) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "http://localhost:3000")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        print(f"Dashboard API: http://{host}:{port}/api/dashboard")
        server.serve_forever()
    finally:
        server.server_close()
