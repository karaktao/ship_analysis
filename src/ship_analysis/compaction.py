from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
import gzip
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AppConfig, CompactionConfig
from .models import utc_now_iso
from .storage import Storage


@dataclass(frozen=True)
class SourceSample:
    observation_id: int
    observed_at: datetime
    position_time_utc: str | None
    track_id: str | None
    lat: float | None
    lon: float | None
    speed_ground: float | None
    course_ground: float | None
    is_moving: int | None
    vessel_name: str | None
    call_sign: str | None
    mmsi: int | None
    eni: str | None
    imo: int | None
    length_m: float | None
    beam_m: float | None
    ais_ship_type: int | None
    eri_ship_type: int | None
    isrs_code: str | None
    isrs_name: str | None
    direction: int | None
    privacy_class: int | None

    @property
    def track_key(self) -> str:
        if self.track_id:
            return f"track:{self.track_id}"
        # Without a provider track id, separate observations must not be merged
        # into a fictitious vessel.
        return f"observation:{self.observation_id}"


@dataclass(frozen=True)
class CompactedRecord:
    track_key: str
    track_id: str | None
    sequence_no: int
    record_type: str
    started_at_utc: str
    ended_at_utc: str
    first_position_time_utc: str | None
    last_position_time_utc: str | None
    representative_lat: float | None
    representative_lon: float | None
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None
    duration_seconds: float
    sample_count: int
    unique_observation_count: int
    max_radius_m: float | None
    source_observation_id: int | None
    speed_ground: float | None
    course_ground: float | None
    source_is_moving: int | None
    vessel_name: str | None
    call_sign: str | None
    mmsi: int | None
    eni: str | None
    imo: int | None
    length_m: float | None
    beam_m: float | None
    ais_ship_type: int | None
    eri_ship_type: int | None
    isrs_code: str | None
    isrs_name: str | None
    direction: int | None
    privacy_class: int | None


@dataclass(frozen=True)
class CompactionOutcome:
    operational_date: str
    source_samples: int
    tracks: int
    output_records: int
    position_records: int
    stationary_records: int
    stationary_source_samples: int
    output_path: str


def _parse_datetime(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _zone(config: CompactionConfig) -> ZoneInfo:
    try:
        return ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            f"Unknown compaction timezone {config.timezone!r}; "
            "install system time-zone data or choose a valid IANA zone"
        ) from error


def operational_day_bounds(
    operational_date: date, config: CompactionConfig
) -> tuple[datetime, datetime]:
    zone = _zone(config)
    local_start = datetime.combine(
        operational_date,
        wall_time(hour=config.day_boundary_hour),
        tzinfo=zone,
    )
    local_end = datetime.combine(
        operational_date + timedelta(days=1),
        wall_time(hour=config.day_boundary_hour),
        tzinfo=zone,
    )
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def operational_date_for(value: datetime, config: CompactionConfig) -> date:
    local = value.astimezone(_zone(config))
    boundary = datetime.combine(
        local.date(),
        wall_time(hour=config.day_boundary_hour),
        tzinfo=local.tzinfo,
    )
    return local.date() if local >= boundary else local.date() - timedelta(days=1)


def latest_ready_operational_date(
    now_utc: datetime, config: CompactionConfig
) -> date:
    effective_now = now_utc - timedelta(minutes=config.process_delay_minutes)
    return operational_date_for(effective_now, config) - timedelta(days=1)


def _haversine_m(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    earth_radius_m = 6_371_008.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return earth_radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _centroid(samples: Iterable[SourceSample]) -> tuple[float, float]:
    points = [(sample.lat, sample.lon) for sample in samples]
    return (
        sum(point[0] for point in points if point[0] is not None) / len(points),
        sum(point[1] for point in points if point[1] is not None) / len(points),
    )


def _max_radius_m(samples: list[SourceSample]) -> float:
    centroid_lat, centroid_lon = _centroid(samples)
    return max(
        _haversine_m(
            centroid_lat,
            centroid_lon,
            float(sample.lat),
            float(sample.lon),
        )
        for sample in samples
    )


def _latest_metadata(samples: list[SourceSample]) -> SourceSample:
    # Later snapshots can contain enriched vessel attributes. Prefer the most
    # recent sample while the source observation remains auditable in SQLite.
    return samples[-1]


def _position_record(sample: SourceSample, sequence_no: int) -> CompactedRecord:
    observed_at = _iso_utc(sample.observed_at)
    return CompactedRecord(
        track_key=sample.track_key,
        track_id=sample.track_id,
        sequence_no=sequence_no,
        record_type="position",
        started_at_utc=observed_at,
        ended_at_utc=observed_at,
        first_position_time_utc=sample.position_time_utc,
        last_position_time_utc=sample.position_time_utc,
        representative_lat=sample.lat,
        representative_lon=sample.lon,
        start_lat=sample.lat,
        start_lon=sample.lon,
        end_lat=sample.lat,
        end_lon=sample.lon,
        duration_seconds=0.0,
        sample_count=1,
        unique_observation_count=1,
        max_radius_m=0.0 if sample.lat is not None and sample.lon is not None else None,
        source_observation_id=sample.observation_id,
        speed_ground=sample.speed_ground,
        course_ground=sample.course_ground,
        source_is_moving=sample.is_moving,
        vessel_name=sample.vessel_name,
        call_sign=sample.call_sign,
        mmsi=sample.mmsi,
        eni=sample.eni,
        imo=sample.imo,
        length_m=sample.length_m,
        beam_m=sample.beam_m,
        ais_ship_type=sample.ais_ship_type,
        eri_ship_type=sample.eri_ship_type,
        isrs_code=sample.isrs_code,
        isrs_name=sample.isrs_name,
        direction=sample.direction,
        privacy_class=sample.privacy_class,
    )


def _stationary_record(
    samples: list[SourceSample], sequence_no: int
) -> CompactedRecord:
    centroid_lat, centroid_lon = _centroid(samples)
    max_radius = _max_radius_m(samples)
    metadata = _latest_metadata(samples)
    first = samples[0]
    last = samples[-1]
    return CompactedRecord(
        track_key=first.track_key,
        track_id=first.track_id,
        sequence_no=sequence_no,
        record_type="stationary",
        started_at_utc=_iso_utc(first.observed_at),
        ended_at_utc=_iso_utc(last.observed_at),
        first_position_time_utc=first.position_time_utc,
        last_position_time_utc=last.position_time_utc,
        representative_lat=centroid_lat,
        representative_lon=centroid_lon,
        start_lat=first.lat,
        start_lon=first.lon,
        end_lat=last.lat,
        end_lon=last.lon,
        duration_seconds=(last.observed_at - first.observed_at).total_seconds(),
        sample_count=len(samples),
        unique_observation_count=len(
            {sample.observation_id for sample in samples}
        ),
        max_radius_m=max_radius,
        source_observation_id=None,
        speed_ground=metadata.speed_ground,
        course_ground=metadata.course_ground,
        source_is_moving=metadata.is_moving,
        vessel_name=metadata.vessel_name,
        call_sign=metadata.call_sign,
        mmsi=metadata.mmsi,
        eni=metadata.eni,
        imo=metadata.imo,
        length_m=metadata.length_m,
        beam_m=metadata.beam_m,
        ais_ship_type=metadata.ais_ship_type,
        eri_ship_type=metadata.eri_ship_type,
        isrs_code=metadata.isrs_code,
        isrs_name=metadata.isrs_name,
        direction=metadata.direction,
        privacy_class=metadata.privacy_class,
    )


def compact_track(
    samples: list[SourceSample], config: CompactionConfig
) -> list[CompactedRecord]:
    if not samples:
        return []
    samples = sorted(samples, key=lambda sample: (sample.observed_at, sample.observation_id))
    max_gap = timedelta(minutes=config.stationary_max_gap_minutes)
    min_duration = timedelta(minutes=config.stationary_min_duration_minutes)
    groups: list[list[SourceSample]] = []
    candidate: list[SourceSample] = []

    def flush() -> None:
        nonlocal candidate
        if candidate:
            groups.append(candidate)
            candidate = []

    for sample in samples:
        has_position = sample.lat is not None and sample.lon is not None
        explicitly_moving = sample.is_moving == 1
        if not has_position or explicitly_moving:
            flush()
            groups.append([sample])
            continue

        if not candidate:
            candidate = [sample]
            continue

        gap = sample.observed_at - candidate[-1].observed_at
        centroid_lat, centroid_lon = _centroid(candidate)
        distance = _haversine_m(
            centroid_lat,
            centroid_lon,
            float(sample.lat),
            float(sample.lon),
        )
        if gap <= max_gap and distance <= config.stationary_radius_m:
            candidate.append(sample)
        else:
            flush()
            candidate = [sample]
    flush()

    records: list[CompactedRecord] = []
    sequence_no = 0
    for group in groups:
        duration = group[-1].observed_at - group[0].observed_at
        stationary = (
            len(group) >= config.stationary_min_samples
            and duration >= min_duration
            and all(sample.is_moving != 1 for sample in group)
            and all(sample.lat is not None and sample.lon is not None for sample in group)
            and _max_radius_m(group) <= config.stationary_radius_m
        )
        if stationary:
            records.append(_stationary_record(group, sequence_no))
            sequence_no += 1
        else:
            for sample in group:
                records.append(_position_record(sample, sequence_no))
                sequence_no += 1
    return records


class DailyCompactor:
    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.settings = config.compaction
        self.storage = storage
        self.output_dir = config.data_dir / "compacted"

    def compact_day(
        self,
        operational_date: date,
        *,
        force: bool = False,
        allow_incomplete: bool = False,
        now_utc: datetime | None = None,
    ) -> CompactionOutcome | None:
        self.storage.initialize()
        now = now_utc or datetime.now(timezone.utc)
        latest_ready = latest_ready_operational_date(now, self.settings)
        if not allow_incomplete and operational_date > latest_ready:
            raise ValueError(
                f"Operational day {operational_date.isoformat()} is not ready; "
                f"latest ready day is {latest_ready.isoformat()} after the "
                f"{self.settings.process_delay_minutes}-minute closing delay"
            )
        day_key = operational_date.isoformat()
        start_utc, end_utc = operational_day_bounds(
            operational_date, self.settings
        )
        settings_json = json.dumps(
            asdict(self.settings), sort_keys=True, separators=(",", ":")
        )

        with self.storage.connect() as connection:
            existing = connection.execute(
                """
                SELECT status, source_sample_count, track_count,
                       output_record_count, position_record_count,
                       stationary_record_count, stationary_source_sample_count,
                       output_path
                FROM daily_compaction_runs
                WHERE operational_date = ?
                """,
                (day_key,),
            ).fetchone()
            if existing and existing["status"] == "completed" and not force:
                return CompactionOutcome(
                    operational_date=day_key,
                    source_samples=int(existing["source_sample_count"] or 0),
                    tracks=int(existing["track_count"] or 0),
                    output_records=int(existing["output_record_count"] or 0),
                    position_records=int(existing["position_record_count"] or 0),
                    stationary_records=int(existing["stationary_record_count"] or 0),
                    stationary_source_samples=int(
                        existing["stationary_source_sample_count"] or 0
                    ),
                    output_path=str(existing["output_path"] or ""),
                )
            connection.execute(
                """
                INSERT INTO daily_compaction_runs (
                    operational_date, timezone, day_start_utc, day_end_utc,
                    started_at_utc, status, settings_json
                ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                ON CONFLICT(operational_date) DO UPDATE SET
                    timezone = excluded.timezone,
                    day_start_utc = excluded.day_start_utc,
                    day_end_utc = excluded.day_end_utc,
                    started_at_utc = excluded.started_at_utc,
                    completed_at_utc = NULL,
                    status = 'running',
                    source_sample_count = NULL,
                    track_count = NULL,
                    output_record_count = NULL,
                    position_record_count = NULL,
                    stationary_record_count = NULL,
                    stationary_source_sample_count = NULL,
                    output_path = NULL,
                    settings_json = excluded.settings_json,
                    error = NULL
                """,
                (
                    day_key,
                    self.settings.timezone,
                    _iso_utc(start_utc),
                    _iso_utc(end_utc),
                    utc_now_iso(),
                    settings_json,
                ),
            )
            connection.execute(
                "DELETE FROM daily_track_records WHERE operational_date = ?",
                (day_key,),
            )

        try:
            summary = self._summarize_tracks(start_utc, end_utc)
            output_path = self._write_streamed_output(
                operational_date,
                start_utc,
                end_utc,
                summary,
                day_key,
            )
        except Exception as error:
            with self.storage.connect() as connection:
                connection.execute(
                    """
                    UPDATE daily_compaction_runs
                    SET status = 'failed', completed_at_utc = ?, error = ?
                    WHERE operational_date = ?
                    """,
                    (utc_now_iso(), f"{type(error).__name__}: {error}"[:4000], day_key),
                )
            raise

        return CompactionOutcome(
            operational_date=day_key,
            source_samples=summary["source_samples"],
            tracks=summary["tracks"],
            output_records=summary["output_records"],
            position_records=summary["position_records"],
            stationary_records=summary["stationary_records"],
            stationary_source_samples=summary["stationary_source_samples"],
            output_path=str(output_path),
        )

    @staticmethod
    def _row_to_sample(row: sqlite3.Row) -> SourceSample:
        return SourceSample(
            observation_id=int(row["observation_id"]),
            observed_at=_parse_datetime(row["observed_at_utc"]),
            position_time_utc=row["position_time_utc"],
            track_id=row["track_id"],
            lat=row["lat"],
            lon=row["lon"],
            speed_ground=row["speed_ground"],
            course_ground=row["course_ground"],
            is_moving=row["is_moving"],
            vessel_name=row["vessel_name"],
            call_sign=row["call_sign"],
            mmsi=row["mmsi"],
            eni=row["eni"],
            imo=row["imo"],
            length_m=row["length_m"],
            beam_m=row["beam_m"],
            ais_ship_type=row["ais_ship_type"],
            eri_ship_type=row["eri_ship_type"],
            isrs_code=row["isrs_code"],
            isrs_name=row["isrs_name"],
            direction=row["direction"],
            privacy_class=row["privacy_class"],
        )

    def _iter_track_groups(
        self, start_utc: datetime, end_utc: datetime
    ) -> Iterator[list[SourceSample]]:
        """Yield one track at a time with SQLite sorting on disk."""
        query = """
            SELECT
                o.observation_id,
                r.started_at_utc AS observed_at_utc,
                o.position_time_utc,
                o.track_id,
                o.lat,
                o.lon,
                o.speed_ground,
                o.course_ground,
                o.is_moving,
                o.vessel_name,
                o.call_sign,
                o.mmsi,
                o.eni,
                o.imo,
                o.length_m,
                o.beam_m,
                o.ais_ship_type,
                o.eri_ship_type,
                o.isrs_code,
                o.isrs_name,
                o.direction,
                o.privacy_class
            FROM collection_runs r
            JOIN collection_observations co ON co.run_id = r.run_id
            JOIN observations o ON o.observation_id = co.observation_id
            WHERE r.status = 'completed'
              AND r.started_at_utc >= ?
              AND r.started_at_utc < ?
              AND co.inside_requested_bbox = 1
            ORDER BY o.track_id, r.started_at_utc, o.observation_id
        """
        with self.storage.connect() as connection:
            # The result is streamed row-by-row, while SQLite's large sort is
            # forced to disk. This avoids both Python fetchall() amplification
            # and a second full-table scan for every track chunk.
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-32768")
            rows = connection.execute(
                query,
                (
                    start_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    end_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            current_key: str | None = None
            group: list[SourceSample] = []
            for row in rows:
                sample = self._row_to_sample(row)
                if group and sample.track_key != current_key:
                    yield group
                    group = []
                current_key = sample.track_key
                group.append(sample)
            if group:
                yield group

    def _summarize_tracks(
        self, start_utc: datetime, end_utc: datetime
    ) -> dict[str, int]:
        summary = {
            "source_samples": 0,
            "tracks": 0,
            "output_records": 0,
            "position_records": 0,
            "stationary_records": 0,
            "stationary_source_samples": 0,
        }
        for samples in self._iter_track_groups(start_utc, end_utc):
            records = compact_track(samples, self.settings)
            stationary = [
                record for record in records if record.record_type == "stationary"
            ]
            summary["source_samples"] += len(samples)
            summary["tracks"] += 1
            summary["output_records"] += len(records)
            summary["position_records"] += len(records) - len(stationary)
            summary["stationary_records"] += len(stationary)
            summary["stationary_source_samples"] += sum(
                record.sample_count for record in stationary
            )
        return summary

    def _write_streamed_output(
        self,
        operational_date: date,
        start_utc: datetime,
        end_utc: datetime,
        summary: dict[str, int],
        day_key: str,
    ) -> Path:
        directory = (
            self.output_dir
            / f"{operational_date.year:04d}"
            / f"{operational_date.month:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        output_path = directory / (
            f"operational-day-{operational_date.isoformat()}.json.gz"
        )
        metadata = {
            "operational_date": operational_date.isoformat(),
            "timezone": self.settings.timezone,
            "day_start_utc": _iso_utc(start_utc),
            "day_end_utc": _iso_utc(end_utc),
            "created_at_utc": utc_now_iso(),
            **summary,
            "settings": asdict(self.settings),
            "source_layer_retained": True,
        }
        insert_sql = """
            INSERT INTO daily_track_records (
                operational_date, track_key, track_id, sequence_no, record_type,
                started_at_utc, ended_at_utc, first_position_time_utc,
                last_position_time_utc, representative_lat, representative_lon,
                start_lat, start_lon, end_lat, end_lon, duration_seconds,
                sample_count, unique_observation_count, max_radius_m,
                source_observation_id, speed_ground, course_ground,
                source_is_moving, vessel_name, call_sign, mmsi, eni, imo,
                length_m, beam_m, ais_ship_type, eri_ship_type, isrs_code,
                isrs_name, direction, privacy_class
            ) VALUES (
                :operational_date, :track_key, :track_id, :sequence_no,
                :record_type, :started_at_utc, :ended_at_utc,
                :first_position_time_utc, :last_position_time_utc,
                :representative_lat, :representative_lon, :start_lat, :start_lon,
                :end_lat, :end_lon, :duration_seconds, :sample_count,
                :unique_observation_count, :max_radius_m,
                :source_observation_id, :speed_ground, :course_ground,
                :source_is_moving, :vessel_name, :call_sign, :mmsi, :eni, :imo,
                :length_m, :beam_m, :ais_ship_type, :eri_ship_type, :isrs_code,
                :isrs_name, :direction, :privacy_class
            )
        """
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=".compacted_", suffix=".tmp"
        )
        try:
            with os.fdopen(file_descriptor, "wb") as raw_file:
                with gzip.GzipFile(
                    fileobj=raw_file, mode="wb", compresslevel=6
                ) as gz:
                    gz.write(b'{"metadata":')
                    gz.write(
                        json.dumps(
                            metadata, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    gz.write(b',"records":[')
                    first_record = True
                    batch: list[dict[str, Any]] = []
                    for samples in self._iter_track_groups(start_utc, end_utc):
                        records = compact_track(samples, self.settings)
                        for record in records:
                            if not first_record:
                                gz.write(b",")
                            first_record = False
                            gz.write(
                                json.dumps(
                                    asdict(record),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            )
                            values = asdict(record)
                            values["operational_date"] = day_key
                            batch.append(values)
                        if len(batch) >= 1000:
                            with self.storage.connect() as connection:
                                connection.executemany(insert_sql, batch)
                            batch.clear()
                    if batch:
                        with self.storage.connect() as connection:
                            connection.executemany(insert_sql, batch)
                    gz.write(b"]}")
            os.replace(temporary_name, output_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

        with self.storage.connect() as connection:
            connection.execute(
                """
                UPDATE daily_compaction_runs
                SET status = 'completed',
                    completed_at_utc = ?,
                    source_sample_count = ?,
                    track_count = ?,
                    output_record_count = ?,
                    position_record_count = ?,
                    stationary_record_count = ?,
                    stationary_source_sample_count = ?,
                    output_path = ?,
                    error = NULL
                WHERE operational_date = ?
                """,
                (
                    utc_now_iso(),
                    summary["source_samples"],
                    summary["tracks"],
                    summary["output_records"],
                    summary["position_records"],
                    summary["stationary_records"],
                    summary["stationary_source_samples"],
                    str(output_path),
                    day_key,
                ),
            )
        return output_path

    def compact_pending(
        self, now_utc: datetime | None = None
    ) -> list[CompactionOutcome]:
        if not self.settings.enabled:
            return []
        now = now_utc or datetime.now(timezone.utc)
        latest_ready = latest_ready_operational_date(now, self.settings)
        with self.storage.connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(started_at_utc)
                FROM collection_runs
                WHERE status = 'completed'
                """
            ).fetchone()
        if not row or not row[0]:
            return []

        first_date = operational_date_for(_parse_datetime(row[0]), self.settings)
        outcomes: list[CompactionOutcome] = []
        current = first_date
        while current <= latest_ready:
            with self.storage.connect() as connection:
                completed = connection.execute(
                    """
                    SELECT 1 FROM daily_compaction_runs
                    WHERE operational_date = ? AND status = 'completed'
                    """,
                    (current.isoformat(),),
                ).fetchone()
            if not completed:
                outcome = self.compact_day(current, now_utc=now)
                if outcome is not None:
                    outcomes.append(outcome)
            current += timedelta(days=1)
        return outcomes

    def _load_samples(
        self, start_utc: datetime, end_utc: datetime
    ) -> list[SourceSample]:
        with self.storage.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    o.observation_id,
                    r.started_at_utc AS observed_at_utc,
                    o.position_time_utc,
                    o.track_id,
                    o.lat,
                    o.lon,
                    o.speed_ground,
                    o.course_ground,
                    o.is_moving,
                    o.vessel_name,
                    o.call_sign,
                    o.mmsi,
                    o.eni,
                    o.imo,
                    o.length_m,
                    o.beam_m,
                    o.ais_ship_type,
                    o.eri_ship_type,
                    o.isrs_code,
                    o.isrs_name,
                    o.direction,
                    o.privacy_class
                FROM collection_runs r
                JOIN collection_observations co ON co.run_id = r.run_id
                JOIN observations o ON o.observation_id = co.observation_id
                WHERE r.status = 'completed'
                  AND r.started_at_utc >= ?
                  AND r.started_at_utc < ?
                  AND co.inside_requested_bbox = 1
                ORDER BY o.track_id, r.started_at_utc, o.observation_id
                """,
                (
                    start_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    end_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
                ),
            ).fetchall()

        return [
            SourceSample(
                observation_id=int(row["observation_id"]),
                observed_at=_parse_datetime(row["observed_at_utc"]),
                position_time_utc=row["position_time_utc"],
                track_id=row["track_id"],
                lat=row["lat"],
                lon=row["lon"],
                speed_ground=row["speed_ground"],
                course_ground=row["course_ground"],
                is_moving=row["is_moving"],
                vessel_name=row["vessel_name"],
                call_sign=row["call_sign"],
                mmsi=row["mmsi"],
                eni=row["eni"],
                imo=row["imo"],
                length_m=row["length_m"],
                beam_m=row["beam_m"],
                ais_ship_type=row["ais_ship_type"],
                eri_ship_type=row["eri_ship_type"],
                isrs_code=row["isrs_code"],
                isrs_name=row["isrs_name"],
                direction=row["direction"],
                privacy_class=row["privacy_class"],
            )
            for row in rows
        ]

    def _write_output(
        self,
        operational_date: date,
        start_utc: datetime,
        end_utc: datetime,
        samples: list[SourceSample],
        records: list[CompactedRecord],
    ) -> Path:
        directory = (
            self.output_dir
            / f"{operational_date.year:04d}"
            / f"{operational_date.month:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        output_path = directory / (
            f"operational-day-{operational_date.isoformat()}.json.gz"
        )
        stationary = [
            record for record in records if record.record_type == "stationary"
        ]
        payload = {
            "metadata": {
                "operational_date": operational_date.isoformat(),
                "timezone": self.settings.timezone,
                "day_start_utc": _iso_utc(start_utc),
                "day_end_utc": _iso_utc(end_utc),
                "created_at_utc": utc_now_iso(),
                "source_sample_count": len(samples),
                "output_record_count": len(records),
                "position_record_count": len(records) - len(stationary),
                "stationary_record_count": len(stationary),
                "stationary_source_sample_count": sum(
                    record.sample_count for record in stationary
                ),
                "settings": asdict(self.settings),
                "source_layer_retained": True,
            },
            "records": [asdict(record) for record in records],
        }
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=".compacted_", suffix=".tmp"
        )
        try:
            with os.fdopen(file_descriptor, "wb") as raw_file:
                with gzip.GzipFile(fileobj=raw_file, mode="wb", compresslevel=6) as gz:
                    gz.write(
                        json.dumps(
                            payload, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
            os.replace(temporary_name, output_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return output_path

    def _store_records(
        self,
        day_key: str,
        grouped: dict[str, list[SourceSample]],
        samples: list[SourceSample],
        records: list[CompactedRecord],
        output_path: Path,
    ) -> None:
        stationary = [
            record for record in records if record.record_type == "stationary"
        ]
        insert_sql = """
            INSERT INTO daily_track_records (
                operational_date, track_key, track_id, sequence_no, record_type,
                started_at_utc, ended_at_utc, first_position_time_utc,
                last_position_time_utc, representative_lat, representative_lon,
                start_lat, start_lon, end_lat, end_lon, duration_seconds,
                sample_count, unique_observation_count, max_radius_m,
                source_observation_id, speed_ground, course_ground,
                source_is_moving, vessel_name, call_sign, mmsi, eni, imo,
                length_m, beam_m, ais_ship_type, eri_ship_type, isrs_code,
                isrs_name, direction, privacy_class
            ) VALUES (
                :operational_date, :track_key, :track_id, :sequence_no,
                :record_type, :started_at_utc, :ended_at_utc,
                :first_position_time_utc, :last_position_time_utc,
                :representative_lat, :representative_lon, :start_lat, :start_lon,
                :end_lat, :end_lon, :duration_seconds, :sample_count,
                :unique_observation_count, :max_radius_m,
                :source_observation_id, :speed_ground, :course_ground,
                :source_is_moving, :vessel_name, :call_sign, :mmsi, :eni, :imo,
                :length_m, :beam_m, :ais_ship_type, :eri_ship_type, :isrs_code,
                :isrs_name, :direction, :privacy_class
            )
        """
        with self.storage.connect() as connection:
            for record in records:
                values: dict[str, Any] = asdict(record)
                values["operational_date"] = day_key
                connection.execute(insert_sql, values)
            connection.execute(
                """
                UPDATE daily_compaction_runs
                SET status = 'completed',
                    completed_at_utc = ?,
                    source_sample_count = ?,
                    track_count = ?,
                    output_record_count = ?,
                    position_record_count = ?,
                    stationary_record_count = ?,
                    stationary_source_sample_count = ?,
                    output_path = ?,
                    error = NULL
                WHERE operational_date = ?
                """,
                (
                    utc_now_iso(),
                    len(samples),
                    len(grouped),
                    len(records),
                    len(records) - len(stationary),
                    len(stationary),
                    sum(record.sample_count for record in stationary),
                    str(output_path),
                    day_key,
                ),
            )
