from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .compaction import (
    latest_ready_operational_date,
    operational_date_for,
    operational_day_bounds,
)
from .config import AppConfig
from .models import utc_now_iso
from .storage import Storage


LOGGER = logging.getLogger("ship_analysis.reporting")
UTC = timezone.utc


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _floor_utc(value: datetime, granularity: str) -> datetime:
    value = value.astimezone(UTC)
    if granularity == "minute":
        return value.replace(second=0, microsecond=0)
    if granularity == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported UTC floor granularity: {granularity}")


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return float(ordered[index])


@dataclass(frozen=True)
class PeriodStats:
    granularity: str
    period_start_utc: str
    period_end_utc: str
    period_label_local: str
    timezone: str
    operational_date: str | None
    computed_at_utc: str
    is_final: int
    details_complete: int
    expected_target_count: int
    expected_run_count: int
    observed_run_count: int
    completed_run_count: int
    failed_run_count: int
    running_run_count: int
    tiles_seen: int
    received_item_count: int
    unique_item_count: int
    distinct_observation_count: int | None
    distinct_track_count: int | None
    new_observation_count: int
    existing_observation_count: int
    within_run_duplicate_count: int
    outside_bbox_count: int
    page_count: int
    pagination_anomaly_run_count: int
    total_elapsed_seconds: float
    average_elapsed_seconds: float | None
    p95_elapsed_seconds: float | None
    max_elapsed_seconds: float | None

    @property
    def schedule_completion_rate(self) -> float:
        if self.expected_run_count <= 0:
            return 0.0
        return self.completed_run_count / self.expected_run_count

    @property
    def request_success_rate(self) -> float:
        attempted = self.completed_run_count + self.failed_run_count
        if attempted <= 0:
            return 0.0
        return self.completed_run_count / attempted

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["schedule_completion_rate"] = self.schedule_completion_rate
        result["request_success_rate"] = self.request_success_rate
        return result


@dataclass(frozen=True)
class DailySummary:
    operational_date: str
    health_status: str
    summary_text: str
    output_path: str
    metrics: dict[str, Any]


class CollectionReporter:
    """Materialize durable collection statistics from collection-run audit rows."""

    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self.zone = ZoneInfo(config.compaction.timezone)
        self.summary_dir = config.data_dir / "summaries"

    def _expected_runs(self, start_utc: datetime, end_utc: datetime) -> int:
        seconds = max(0.0, (end_utc - start_utc).total_seconds())
        expectation = sum(
            seconds / target.interval_seconds for target in self.config.targets()
        )
        return int(expectation + 0.5)

    def _period_label(self, start_utc: datetime, granularity: str) -> str:
        local = start_utc.astimezone(self.zone)
        if granularity == "minute":
            return local.isoformat(timespec="minutes")
        if granularity == "hour":
            return local.isoformat(timespec="hours")
        if granularity == "day":
            return operational_date_for(start_utc, self.config.compaction).isoformat()
        raise ValueError(f"Unsupported granularity: {granularity}")

    def calculate_period(
        self,
        start_utc: datetime,
        end_utc: datetime,
        granularity: str,
        *,
        is_final: bool,
    ) -> PeriodStats:
        if granularity not in {"minute", "hour", "day"}:
            raise ValueError(f"Unsupported granularity: {granularity}")
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise ValueError("Period bounds must be timezone-aware")
        start_utc = start_utc.astimezone(UTC)
        end_utc = end_utc.astimezone(UTC)
        if start_utc >= end_utc:
            raise ValueError("Period start must be before period end")

        start_text = _iso_utc(start_utc)
        end_text = _iso_utc(end_utc)
        with self.storage.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS observed_run_count,
                    COALESCE(SUM(status = 'completed'), 0)
                        AS completed_run_count,
                    COALESCE(SUM(status = 'failed'), 0)
                        AS failed_run_count,
                    COALESCE(SUM(status = 'running'), 0)
                        AS running_run_count,
                    COUNT(DISTINCT CASE WHEN status = 'completed'
                        THEN area_id || ':' || tile_id END) AS tiles_seen,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN item_count ELSE 0 END), 0) AS received_item_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN COALESCE(
                            unique_item_count,
                            item_count
                                - COALESCE(within_run_duplicate_count, 0),
                            0
                        ) ELSE 0 END), 0)
                        AS unique_item_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN inserted_count ELSE 0 END), 0)
                        AS new_observation_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN duplicate_count ELSE 0 END), 0)
                        AS existing_observation_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN within_run_duplicate_count ELSE 0 END), 0)
                        AS within_run_duplicate_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN outside_bbox_count ELSE 0 END), 0)
                        AS outside_bbox_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN pages ELSE 0 END), 0) AS page_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        AND (
                            COALESCE(reported_count_delta, 0) <> 0
                            OR COALESCE(within_run_duplicate_count, 0) > 0
                        ) THEN 1 ELSE 0 END), 0)
                        AS pagination_anomaly_run_count,
                    COALESCE(SUM(CASE WHEN status = 'completed'
                        THEN elapsed_seconds ELSE 0 END), 0)
                        AS total_elapsed_seconds,
                    AVG(CASE WHEN status = 'completed'
                        THEN elapsed_seconds END) AS average_elapsed_seconds,
                    MAX(CASE WHEN status = 'completed'
                        THEN elapsed_seconds END) AS max_elapsed_seconds,
                    COALESCE(SUM(details_deleted_at_utc IS NOT NULL), 0)
                        AS details_deleted_run_count
                FROM collection_runs
                WHERE started_at_utc >= ? AND started_at_utc < ?
                """,
                (start_text, end_text),
            ).fetchone()
            elapsed = [
                float(item[0])
                for item in connection.execute(
                    """
                    SELECT elapsed_seconds
                    FROM collection_runs
                    WHERE started_at_utc >= ? AND started_at_utc < ?
                      AND status = 'completed' AND elapsed_seconds IS NOT NULL
                    """,
                    (start_text, end_text),
                )
            ]
            details_complete = int(row["details_deleted_run_count"] == 0)
            distinct_observations: int | None = None
            distinct_tracks: int | None = None
            if details_complete:
                distinct = connection.execute(
                    """
                    SELECT COUNT(DISTINCT co.observation_id),
                           COUNT(DISTINCT o.track_id)
                    FROM collection_runs r
                    JOIN collection_observations co ON co.run_id = r.run_id
                    JOIN observations o
                      ON o.observation_id = co.observation_id
                    WHERE r.started_at_utc >= ? AND r.started_at_utc < ?
                      AND r.status = 'completed'
                    """,
                    (start_text, end_text),
                ).fetchone()
                distinct_observations = int(distinct[0])
                distinct_tracks = int(distinct[1])

        operational_date = (
            operational_date_for(start_utc, self.config.compaction).isoformat()
            if granularity == "day"
            else None
        )
        return PeriodStats(
            granularity=granularity,
            period_start_utc=start_text,
            period_end_utc=end_text,
            period_label_local=self._period_label(start_utc, granularity),
            timezone=self.config.compaction.timezone,
            operational_date=operational_date,
            computed_at_utc=utc_now_iso(),
            is_final=int(is_final),
            details_complete=details_complete,
            expected_target_count=len(self.config.targets()),
            expected_run_count=self._expected_runs(start_utc, end_utc),
            observed_run_count=int(row["observed_run_count"]),
            completed_run_count=int(row["completed_run_count"]),
            failed_run_count=int(row["failed_run_count"]),
            running_run_count=int(row["running_run_count"]),
            tiles_seen=min(
                int(row["tiles_seen"]), len(self.config.targets())
            ),
            received_item_count=int(row["received_item_count"]),
            unique_item_count=int(row["unique_item_count"]),
            distinct_observation_count=distinct_observations,
            distinct_track_count=distinct_tracks,
            new_observation_count=int(row["new_observation_count"]),
            existing_observation_count=int(row["existing_observation_count"]),
            within_run_duplicate_count=int(row["within_run_duplicate_count"]),
            outside_bbox_count=int(row["outside_bbox_count"]),
            page_count=int(row["page_count"]),
            pagination_anomaly_run_count=int(
                row["pagination_anomaly_run_count"]
            ),
            total_elapsed_seconds=float(row["total_elapsed_seconds"]),
            average_elapsed_seconds=(
                float(row["average_elapsed_seconds"])
                if row["average_elapsed_seconds"] is not None
                else None
            ),
            p95_elapsed_seconds=_percentile(elapsed, 0.95),
            max_elapsed_seconds=(
                float(row["max_elapsed_seconds"])
                if row["max_elapsed_seconds"] is not None
                else None
            ),
        )

    def materialize_period(
        self,
        start_utc: datetime,
        end_utc: datetime,
        granularity: str,
        *,
        is_final: bool,
        force: bool = False,
    ) -> tuple[PeriodStats, bool]:
        self.storage.initialize()
        start_text = _iso_utc(start_utc)
        with self.storage.connect() as connection:
            existing = connection.execute(
                """
                SELECT is_final
                FROM collection_period_stats
                WHERE granularity = ? AND period_start_utc = ?
                """,
                (granularity, start_text),
            ).fetchone()
            if existing and int(existing["is_final"]) and not force:
                return self._load_period(connection, granularity, start_text), False

        stats = self.calculate_period(
            start_utc, end_utc, granularity, is_final=is_final
        )
        values = asdict(stats)
        columns = tuple(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"granularity", "period_start_utc"}
        )
        with self.storage.connect() as connection:
            connection.execute(
                f"""
                INSERT INTO collection_period_stats ({",".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(granularity, period_start_utc) DO UPDATE SET
                    {updates}
                """,
                tuple(values[column] for column in columns),
            )
        became_final = bool(is_final and (not existing or not int(existing["is_final"])))
        return stats, became_final

    @staticmethod
    def _load_period(
        connection: sqlite3.Connection, granularity: str, start_text: str
    ) -> PeriodStats:
        row = connection.execute(
            """
            SELECT *
            FROM collection_period_stats
            WHERE granularity = ? AND period_start_utc = ?
            """,
            (granularity, start_text),
        ).fetchone()
        if row is None:
            raise RuntimeError("Materialized period disappeared")
        return PeriodStats(**dict(row))

    def refresh(self, now_utc: datetime | None = None) -> None:
        """Refresh live counters and finalize sufficiently old minute/hour buckets."""
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        current_minute = _floor_utc(now, "minute")
        current_hour = _floor_utc(now, "hour")
        current_day = operational_date_for(now, self.config.compaction)
        day_start, day_end = operational_day_bounds(
            current_day, self.config.compaction
        )
        self.materialize_period(
            current_minute,
            current_minute + timedelta(minutes=1),
            "minute",
            is_final=False,
            force=True,
        )
        self.materialize_period(
            current_hour,
            current_hour + timedelta(hours=1),
            "hour",
            is_final=False,
            force=True,
        )
        self.materialize_period(
            day_start, day_end, "day", is_final=False, force=True
        )

        minute_end = _floor_utc(now - timedelta(seconds=90), "minute")
        minute_start = minute_end - timedelta(minutes=1)
        minute_stats, minute_became_final = self.materialize_period(
            minute_start, minute_end, "minute", is_final=True
        )
        if minute_became_final:
            LOGGER.info(
                "minute-summary period=%s received=%d unique=%d new=%d "
                "runs=%d/%d failed=%d",
                minute_stats.period_label_local,
                minute_stats.received_item_count,
                minute_stats.unique_item_count,
                minute_stats.new_observation_count,
                minute_stats.completed_run_count,
                minute_stats.expected_run_count,
                minute_stats.failed_run_count,
            )

        hour_end = _floor_utc(now - timedelta(minutes=5), "hour")
        hour_start = hour_end - timedelta(hours=1)
        hour_stats, hour_became_final = self.materialize_period(
            hour_start, hour_end, "hour", is_final=True
        )
        if hour_became_final:
            LOGGER.info(
                "hour-summary period=%s received=%d unique=%d new=%d "
                "runs=%d/%d failed=%d",
                hour_stats.period_label_local,
                hour_stats.received_item_count,
                hour_stats.unique_item_count,
                hour_stats.new_observation_count,
                hour_stats.completed_run_count,
                hour_stats.expected_run_count,
                hour_stats.failed_run_count,
            )

    def list_periods(self, granularity: str, limit: int = 20) -> list[PeriodStats]:
        if granularity not in {"minute", "hour", "day"}:
            raise ValueError(f"Unsupported granularity: {granularity}")
        self.storage.initialize()
        with self.storage.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM collection_period_stats
                WHERE granularity = ?
                ORDER BY period_start_utc DESC
                LIMIT ?
                """,
                (granularity, max(1, limit)),
            ).fetchall()
        return [PeriodStats(**dict(row)) for row in rows]

    def _peak_minute(
        self, start_utc: datetime, end_utc: datetime
    ) -> dict[str, Any] | None:
        with self.storage.connect() as connection:
            row = connection.execute(
                """
                SELECT substr(started_at_utc, 1, 16) AS minute_utc,
                       SUM(COALESCE(item_count, 0)) AS received,
                       SUM(status = 'completed') AS completed
                FROM collection_runs
                WHERE started_at_utc >= ? AND started_at_utc < ?
                GROUP BY substr(started_at_utc, 1, 16)
                ORDER BY received DESC, minute_utc ASC
                LIMIT 1
                """,
                (_iso_utc(start_utc), _iso_utc(end_utc)),
            ).fetchone()
        if row is None:
            return None
        minute = _parse_utc(f"{row['minute_utc']}:00+00:00")
        return {
            "period_local": minute.astimezone(self.zone).isoformat(
                timespec="minutes"
            ),
            "received_item_count": int(row["received"]),
            "completed_run_count": int(row["completed"]),
        }

    def generate_daily_summary(
        self,
        operational_date: date,
        *,
        force: bool = False,
        allow_incomplete: bool = False,
        now_utc: datetime | None = None,
    ) -> DailySummary:
        self.storage.initialize()
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        latest_ready = latest_ready_operational_date(now, self.config.compaction)
        if not allow_incomplete and operational_date > latest_ready:
            raise ValueError(
                f"Operational day {operational_date.isoformat()} is not ready; "
                f"latest ready day is {latest_ready.isoformat()}"
            )
        day_key = operational_date.isoformat()
        with self.storage.connect() as connection:
            existing = connection.execute(
                """
                SELECT health_status, summary_text, summary_json, output_path
                FROM daily_collection_summaries
                WHERE operational_date = ?
                """,
                (day_key,),
            ).fetchone()
        if existing and not force:
            return DailySummary(
                operational_date=day_key,
                health_status=str(existing["health_status"]),
                summary_text=str(existing["summary_text"]),
                output_path=str(existing["output_path"]),
                metrics=json.loads(existing["summary_json"]),
            )

        start_utc, end_utc = operational_day_bounds(
            operational_date, self.config.compaction
        )
        day_stats, _ = self.materialize_period(
            start_utc, end_utc, "day", is_final=True, force=True
        )
        hourly: list[PeriodStats] = []
        hour_start = start_utc
        while hour_start < end_utc:
            hour_end = min(hour_start + timedelta(hours=1), end_utc)
            stats, _ = self.materialize_period(
                hour_start, hour_end, "hour", is_final=True, force=force
            )
            hourly.append(stats)
            hour_start = hour_end

        peak_hour = max(
            hourly,
            key=lambda item: (item.received_item_count, item.period_start_utc),
            default=None,
        )
        lowest_hour = min(
            hourly,
            key=lambda item: (item.received_item_count, item.period_start_utc),
            default=None,
        )
        peak_minute = self._peak_minute(start_utc, end_utc)
        with self.storage.connect() as connection:
            active_minutes = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT substr(started_at_utc, 1, 16))
                    FROM collection_runs
                    WHERE started_at_utc >= ? AND started_at_utc < ?
                      AND status = 'completed'
                    """,
                    (_iso_utc(start_utc), _iso_utc(end_utc)),
                ).fetchone()[0]
            )
            channel_rows = connection.execute(
                """
                SELECT provider, area_id,
                       COUNT(*) AS observed_run_count,
                       SUM(status = 'completed') AS completed_run_count,
                       SUM(status = 'failed') AS failed_run_count,
                       COALESCE(SUM(CASE WHEN status = 'completed'
                           THEN item_count ELSE 0 END), 0)
                           AS received_item_count,
                       COALESCE(SUM(CASE WHEN status = 'completed'
                           THEN inserted_count ELSE 0 END), 0)
                           AS new_observation_count
                FROM collection_runs
                WHERE started_at_utc >= ? AND started_at_utc < ?
                GROUP BY provider, area_id
                ORDER BY provider, area_id
                """,
                (_iso_utc(start_utc), _iso_utc(end_utc)),
            ).fetchall()
            compaction = connection.execute(
                """
                SELECT status, source_sample_count, track_count,
                       output_record_count, position_record_count,
                       stationary_record_count,
                       stationary_source_sample_count, output_path, error
                FROM daily_compaction_runs
                WHERE operational_date = ?
                """,
                (day_key,),
            ).fetchone()

        attempted = day_stats.completed_run_count + day_stats.failed_run_count
        completion_rate = day_stats.schedule_completion_rate
        success_rate = day_stats.request_success_rate
        if day_stats.observed_run_count == 0:
            health = "no_data"
        elif (
            day_stats.completed_run_count == 0
            or completion_rate < 0.90
            or (attempted and success_rate < 0.95)
        ):
            health = "critical"
        elif (
            completion_rate < 0.98
            or (attempted and success_rate < 0.99)
            or day_stats.failed_run_count > 0
            or day_stats.pagination_anomaly_run_count > 0
            or day_stats.tiles_seen < day_stats.expected_target_count
        ):
            health = "warning"
        else:
            health = "healthy"

        missing_runs = max(
            0, day_stats.expected_run_count - day_stats.completed_run_count
        )
        findings = [
            (
                f"完成 {day_stats.completed_run_count:,}/"
                f"{day_stats.expected_run_count:,} 次预期网格请求"
                f"（{completion_rate:.2%}），失败 "
                f"{day_stats.failed_run_count:,} 次。"
            ),
            (
                f"接口共返回 {day_stats.received_item_count:,} 条；"
                f"单次请求内去重后 {day_stats.unique_item_count:,} 条；"
                f"其中新写入 {day_stats.new_observation_count:,} 条。"
            ),
            (
                f"覆盖 {day_stats.tiles_seen}/"
                f"{day_stats.expected_target_count} 个网格，"
                f"活跃采集分钟 {active_minutes:,}。"
            ),
        ]
        if missing_runs:
            findings.append(f"相对计划少完成 {missing_runs:,} 次请求。")
        if day_stats.pagination_anomaly_run_count:
            findings.append(
                f"{day_stats.pagination_anomaly_run_count:,} 次请求出现"
                "分页计数变化或页内重复，需要复核。"
            )
        if not day_stats.details_complete:
            findings.append(
                "来源关系已被清理，精确跨网格去重数和轨迹数不可再重算。"
            )
        if compaction is None:
            findings.append("该运营日尚无停泊整合结果。")
        elif compaction["status"] != "completed":
            findings.append(f"停泊整合状态为 {compaction['status']}。")

        summary_text = (
            f"{day_key} 抓取总结 [{health}]\n- "
            + "\n- ".join(findings)
        )
        expected_minutes = int((end_utc - start_utc).total_seconds() // 60)
        metrics: dict[str, Any] = {
            "schema_version": 1,
            "operational_date": day_key,
            "timezone": self.config.compaction.timezone,
            "day_start_utc": _iso_utc(start_utc),
            "day_end_utc": _iso_utc(end_utc),
            "generated_at_utc": utc_now_iso(),
            "health_status": health,
            "collection": day_stats.as_dict(),
            "coverage": {
                "missing_completed_runs": missing_runs,
                "active_collection_minutes": active_minutes,
                "expected_minutes": expected_minutes,
            },
            "channels": [dict(row) for row in channel_rows],
            "peaks": {
                "minute": peak_minute,
                "hour": peak_hour.as_dict() if peak_hour else None,
                "lowest_hour": lowest_hour.as_dict() if lowest_hour else None,
            },
            "compaction": dict(compaction) if compaction is not None else None,
            "findings": findings,
        }
        output_path = self._write_daily_summary(operational_date, metrics)
        with self.storage.connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_collection_summaries (
                    operational_date, timezone, day_start_utc, day_end_utc,
                    generated_at_utc, health_status, summary_text,
                    summary_json, output_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operational_date) DO UPDATE SET
                    timezone = excluded.timezone,
                    day_start_utc = excluded.day_start_utc,
                    day_end_utc = excluded.day_end_utc,
                    generated_at_utc = excluded.generated_at_utc,
                    health_status = excluded.health_status,
                    summary_text = excluded.summary_text,
                    summary_json = excluded.summary_json,
                    output_path = excluded.output_path
                """,
                (
                    day_key,
                    self.config.compaction.timezone,
                    _iso_utc(start_utc),
                    _iso_utc(end_utc),
                    metrics["generated_at_utc"],
                    health,
                    summary_text,
                    json.dumps(
                        metrics, ensure_ascii=False, separators=(",", ":")
                    ),
                    str(output_path),
                ),
            )
        LOGGER.info(
            "daily-summary day=%s health=%s received=%d unique=%d new=%d "
            "runs=%d/%d failed=%d output=%s",
            day_key,
            health,
            day_stats.received_item_count,
            day_stats.unique_item_count,
            day_stats.new_observation_count,
            day_stats.completed_run_count,
            day_stats.expected_run_count,
            day_stats.failed_run_count,
            output_path,
        )
        return DailySummary(
            operational_date=day_key,
            health_status=health,
            summary_text=summary_text,
            output_path=str(output_path),
            metrics=metrics,
        )

    def generate_latest_ready_summary(
        self, now_utc: datetime | None = None
    ) -> DailySummary | None:
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        day = latest_ready_operational_date(now, self.config.compaction)
        start_utc, end_utc = operational_day_bounds(day, self.config.compaction)
        with self.storage.connect() as connection:
            has_runs = connection.execute(
                """
                SELECT 1 FROM collection_runs
                WHERE started_at_utc >= ? AND started_at_utc < ?
                LIMIT 1
                """,
                (_iso_utc(start_utc), _iso_utc(end_utc)),
            ).fetchone()
        if not has_runs:
            return None
        return self.generate_daily_summary(day, now_utc=now)

    def _write_daily_summary(
        self, operational_date: date, payload: dict[str, Any]
    ) -> Path:
        directory = (
            self.summary_dir
            / f"{operational_date.year:04d}"
            / f"{operational_date.month:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            f"collection-summary-{operational_date.isoformat()}.json"
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=".summary_", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return path
