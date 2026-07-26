from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from typing import Any

from .compaction import operational_date_for, operational_day_bounds
from .config import AppConfig
from .reporting import CollectionReporter
from .storage import Storage


UTC = timezone.utc


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


def _period_payload(stats: Any, *, expected_so_far: int | None = None) -> dict[str, Any]:
    expected = (
        expected_so_far
        if expected_so_far is not None
        else stats.expected_run_count
    )
    return {
        "label": stats.period_label_local,
        "received": stats.received_item_count,
        "unique": stats.unique_item_count,
        "new": stats.new_observation_count,
        "existing": stats.existing_observation_count,
        "completedRuns": stats.completed_run_count,
        "failedRuns": stats.failed_run_count,
        "expectedRuns": stats.expected_run_count,
        "expectedRunsSoFar": expected,
        "completionRate": (
            stats.completed_run_count / expected if expected > 0 else 0.0
        ),
        "requestSuccessRate": stats.request_success_rate,
        "tilesSeen": stats.tiles_seen,
        "expectedTiles": stats.expected_target_count,
        "p95Seconds": stats.p95_elapsed_seconds,
        "paginationAnomalies": stats.pagination_anomaly_run_count,
        "outsideBBox": stats.outside_bbox_count,
        "distinctObservations": stats.distinct_observation_count,
        "distinctTracks": stats.distinct_track_count,
        "detailsComplete": bool(stats.details_complete),
    }


def _english_summary_findings(summary_json: dict[str, Any]) -> list[str]:
    collection = summary_json.get("collection", {})
    completed = int(collection.get("completed_run_count", 0))
    expected = int(collection.get("expected_run_count", 0))
    failed = int(collection.get("failed_run_count", 0))
    received = int(collection.get("received_item_count", 0))
    unique = int(collection.get("unique_item_count", 0))
    new_items = int(collection.get("new_observation_count", 0))
    tiles = int(collection.get("tiles_seen", 0))
    expected_tiles = int(collection.get("expected_target_count", 0))
    completion = completed / expected if expected else 0.0
    return [
        f"Completed {completed:,} of {expected:,} scheduled grid requests ({completion:.1%}); {failed:,} failed.",
        f"The API returned {received:,} items; {unique:,} remained after per-request deduplication; {new_items:,} were new observations.",
        f"Coverage reached {tiles}/{expected_tiles} configured grids.",
        *[
            "The source relations were retained when this report was materialized, so cross-grid distinct counts are available."
            if collection.get("details_complete")
            else "Source relations have been cleaned; exact cross-grid distinct counts cannot be recomputed."
        ],
    ]


class DashboardSnapshot:
    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self.reporter = CollectionReporter(config, storage)
        self.zone = self.reporter.zone

    def build(self, now_utc: datetime | None = None) -> dict[str, Any]:
        self.storage.initialize()
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        minute_start = now.replace(second=0, microsecond=0)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        operational_date = operational_date_for(now, self.config.compaction)
        day_start, day_end = operational_day_bounds(
            operational_date, self.config.compaction
        )
        minute_stats = self.reporter.calculate_period(
            minute_start,
            minute_start + timedelta(minutes=1),
            "minute",
            is_final=False,
        )
        hour_stats = self.reporter.calculate_period(
            hour_start,
            hour_start + timedelta(hours=1),
            "hour",
            is_final=False,
        )
        day_stats = self.reporter.calculate_period(
            day_start, day_end, "day", is_final=False
        )
        expected_so_far = self.reporter._expected_runs(
            day_start, min(now, day_end)
        )

        with self.storage.connect() as connection:
            latest_run = connection.execute(
                """
                SELECT started_at_utc, completed_at_utc, status, area_id,
                       tile_id, item_count, error
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
            recent_runs = [
                {
                    "startedAt": row["started_at_utc"],
                    "tileId": row["tile_id"],
                    "status": row["status"],
                    "items": int(row["item_count"] or 0),
                    "new": int(row["inserted_count"] or 0),
                    "existing": int(row["duplicate_count"] or 0),
                    "pages": int(row["pages"] or 0),
                    "elapsedSeconds": (
                        float(row["elapsed_seconds"])
                        if row["elapsed_seconds"] is not None
                        else None
                    ),
                    "error": row["error"],
                }
                for row in connection.execute(
                    """
                    SELECT started_at_utc, tile_id, status, item_count,
                           inserted_count, duplicate_count, pages,
                           elapsed_seconds, error
                    FROM collection_runs
                    ORDER BY started_at_utc DESC
                    LIMIT 12
                    """
                )
            ]
            tile_rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT tile_id, status, started_at_utc,
                           ROW_NUMBER() OVER (
                               PARTITION BY tile_id
                               ORDER BY started_at_utc DESC
                           ) AS rank
                    FROM collection_runs
                    WHERE area_id = 'nl_coverage'
                ),
                totals AS (
                    SELECT tile_id,
                           SUM(status = 'completed') AS completed_runs,
                           SUM(status = 'failed') AS failed_runs,
                           COALESCE(SUM(CASE WHEN status = 'completed'
                               THEN item_count ELSE 0 END), 0)
                               AS received_items,
                           MAX(started_at_utc) AS last_run_at
                    FROM collection_runs
                    WHERE area_id = 'nl_coverage'
                      AND started_at_utc >= ? AND started_at_utc < ?
                    GROUP BY tile_id
                )
                SELECT totals.*, ranked.status AS latest_status
                FROM totals
                LEFT JOIN ranked
                  ON ranked.tile_id = totals.tile_id AND ranked.rank = 1
                """,
                (_iso_utc(day_start), _iso_utc(day_end)),
            ).fetchall()
            final_summary = connection.execute(
                """
                SELECT operational_date, generated_at_utc, health_status,
                       summary_text, summary_json
                FROM daily_collection_summaries
                ORDER BY operational_date DESC
                LIMIT 1
                """
            ).fetchone()
            compaction = connection.execute(
                """
                SELECT operational_date, status, source_sample_count,
                       output_record_count, position_record_count,
                       stationary_record_count,
                       stationary_source_sample_count
                FROM daily_compaction_runs
                ORDER BY operational_date DESC
                LIMIT 1
                """
            ).fetchone()

        last_started = latest_run["started_at_utc"] if latest_run else None
        freshness = _seconds_since(last_started, now)
        if running_count:
            collector_status = "collecting"
        elif freshness is not None and freshness <= 120:
            collector_status = "online"
        elif latest_run is None:
            collector_status = "no_data"
        else:
            collector_status = "stopped"

        tile_by_id = {str(row["tile_id"]): row for row in tile_rows}
        tiles: list[dict[str, Any]] = []
        for target in self.config.targets():
            row = tile_by_id.get(target.tile_id)
            last_run_at = row["last_run_at"] if row else None
            tile_freshness = _seconds_since(last_run_at, now)
            if row is None:
                status = "missing"
            elif row["latest_status"] == "failed":
                status = "failed"
            elif tile_freshness is not None and tile_freshness <= 120:
                status = "fresh"
            else:
                status = "stale"
            tiles.append(
                {
                    "tileId": target.tile_id,
                    "row": int(target.tile_id[1:3]),
                    "column": int(target.tile_id[4:6]),
                    "status": status,
                    "received": int(row["received_items"] or 0) if row else 0,
                    "completedRuns": int(row["completed_runs"] or 0) if row else 0,
                    "failedRuns": int(row["failed_runs"] or 0) if row else 0,
                    "lastRunAt": last_run_at,
                    "freshnessSeconds": tile_freshness,
                }
            )

        latest_summary: dict[str, Any] | None = None
        if final_summary:
            summary_json = json.loads(final_summary["summary_json"])
            english_findings = _english_summary_findings(summary_json)
            latest_summary = {
                "operationalDate": final_summary["operational_date"],
                "generatedAt": final_summary["generated_at_utc"],
                "healthStatus": final_summary["health_status"],
                "summaryText": "\n".join(english_findings),
                "findings": english_findings,
            }

        return {
            "schemaVersion": 1,
            "mode": "live-local",
            "generatedAt": _iso_utc(now),
            "timezone": self.config.compaction.timezone,
            "collector": {
                "status": collector_status,
                "runningRequests": running_count,
                "lastRunAt": last_started,
                "freshnessSeconds": freshness,
                "latestTile": latest_run["tile_id"] if latest_run else None,
                "latestItems": int(latest_run["item_count"] or 0)
                if latest_run
                else 0,
                "latestError": latest_run["error"] if latest_run else None,
                "targetCount": len(self.config.targets()),
                "intervalSeconds": min(
                    (target.interval_seconds for target in self.config.targets()),
                    default=0,
                ),
            },
            "current": {
                "minute": _period_payload(minute_stats),
                "hour": _period_payload(hour_stats),
                "day": _period_payload(
                    day_stats, expected_so_far=expected_so_far
                ),
            },
            "timelines": {
                "minute": self._timeline(
                    "minute", minute_start, 60, timedelta(minutes=1)
                ),
                "hour": self._timeline(
                    "hour", hour_start, 24, timedelta(hours=1)
                ),
                "day": self._day_timeline(operational_date, 14),
            },
            "tiles": tiles,
            "recentRuns": recent_runs,
            "latestDailySummary": latest_summary,
            "latestCompaction": dict(compaction) if compaction else None,
        }

    def _timeline(
        self,
        granularity: str,
        current_start: datetime,
        count: int,
        step: timedelta,
    ) -> list[dict[str, Any]]:
        first_start = current_start - step * (count - 1)
        with self.storage.connect() as connection:
            if granularity == "minute":
                group_expression = "substr(started_at_utc, 1, 16)"
                key_length = 16
            else:
                group_expression = "substr(started_at_utc, 1, 13)"
                key_length = 13
            rows = connection.execute(
                f"""
                SELECT {group_expression} AS period_key,
                       COALESCE(SUM(CASE WHEN status = 'completed'
                           THEN item_count ELSE 0 END), 0) AS received,
                       COALESCE(SUM(CASE WHEN status = 'completed'
                           THEN inserted_count ELSE 0 END), 0) AS new_items,
                       SUM(status = 'completed') AS completed,
                       SUM(status = 'failed') AS failed
                FROM collection_runs
                WHERE started_at_utc >= ? AND started_at_utc < ?
                GROUP BY {group_expression}
                """,
                (
                    _iso_utc(first_start),
                    _iso_utc(current_start + step),
                ),
            ).fetchall()
        by_key = {str(row["period_key"]): row for row in rows}
        result: list[dict[str, Any]] = []
        cursor = first_start
        while cursor <= current_start:
            key = _iso_utc(cursor)[:key_length]
            row = by_key.get(key)
            local = cursor.astimezone(self.zone)
            label = (
                local.strftime("%H:%M")
                if granularity == "minute"
                else local.strftime("%d %Hh")
            )
            result.append(
                {
                    "periodStart": _iso_utc(cursor),
                    "label": label,
                    "received": int(row["received"] or 0) if row else 0,
                    "new": int(row["new_items"] or 0) if row else 0,
                    "completedRuns": int(row["completed"] or 0) if row else 0,
                    "failedRuns": int(row["failed"] or 0) if row else 0,
                }
            )
            cursor += step
        return result

    def _day_timeline(
        self, current_date: date, count: int
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for offset in range(count - 1, -1, -1):
            operational_date = current_date - timedelta(days=offset)
            start, end = operational_day_bounds(
                operational_date, self.config.compaction
            )
            stats = self.reporter.calculate_period(
                start, end, "day", is_final=operational_date < current_date
            )
            result.append(
                {
                    "periodStart": _iso_utc(start),
                    "label": operational_date.strftime("%m-%d"),
                    "received": stats.received_item_count,
                    "new": stats.new_observation_count,
                    "completedRuns": stats.completed_run_count,
                    "failedRuns": stats.failed_run_count,
                }
            )
        return result


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
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def serve_dashboard_api(
    config: AppConfig,
    storage: Storage,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    snapshot = DashboardSnapshot(config, storage)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ShipAnalysisDashboard/1.0"

        def do_GET(self) -> None:  # noqa: N802
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

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _cors_headers(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin in {
                "http://localhost:3000",
                "http://127.0.0.1:3000",
            }:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store")

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self._cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        print(f"Dashboard API: http://{host}:{port}/api/dashboard")
        server.serve_forever()
    finally:
        server.server_close()
