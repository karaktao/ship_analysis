from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from ship_analysis.compaction import (
    DailyCompactor,
    SourceSample,
    compact_track,
    latest_ready_operational_date,
    operational_day_bounds,
)
from ship_analysis.config import (
    AppConfig,
    BBox,
    CollectionTarget,
    CompactionConfig,
    ProviderConfig,
    RetentionConfig,
)
from ship_analysis.providers import FetchResult
from ship_analysis.retention import RawRetention
from ship_analysis.storage import Storage


def settings() -> CompactionConfig:
    return CompactionConfig(
        enabled=True,
        timezone="Europe/Amsterdam",
        day_boundary_hour=4,
        process_delay_minutes=15,
        stationary_radius_m=30,
        stationary_min_duration_minutes=10,
        stationary_min_samples=5,
        stationary_max_gap_minutes=5,
    )


def sample(
    minute: int,
    *,
    observation_id: int,
    lat: float = 51.9,
    lon: float = 4.5,
    is_moving: int | None = 0,
) -> SourceSample:
    observed_at = datetime(2026, 7, 26, 2, minute, tzinfo=timezone.utc)
    return SourceSample(
        observation_id=observation_id,
        observed_at=observed_at,
        position_time_utc=observed_at.isoformat(),
        track_id="42",
        lat=lat,
        lon=lon,
        speed_ground=0.0,
        course_ground=0.0,
        is_moving=is_moving,
        vessel_name="Test vessel",
        call_sign=None,
        mmsi=None,
        eni=None,
        imo=None,
        length_m=100.0,
        beam_m=11.0,
        ais_ship_type=None,
        eri_ship_type=None,
        isrs_code=None,
        isrs_name=None,
        direction=None,
        privacy_class=0,
    )


class CompactionTests(unittest.TestCase):
    def test_operational_day_is_0400_amsterdam_with_processing_delay(self) -> None:
        start, end = operational_day_bounds(date(2026, 7, 26), settings())
        self.assertEqual(
            datetime(2026, 7, 26, 2, 0, tzinfo=timezone.utc), start
        )
        self.assertEqual(
            datetime(2026, 7, 27, 2, 0, tzinfo=timezone.utc), end
        )
        self.assertEqual(
            date(2026, 7, 26),
            latest_ready_operational_date(
                datetime(2026, 7, 27, 2, 15, tzinfo=timezone.utc),
                settings(),
            ),
        )
        dst_start, dst_end = operational_day_bounds(
            date(2026, 10, 24), settings()
        )
        self.assertEqual(timedelta(hours=25), dst_end - dst_start)

    def test_stable_positions_are_collapsed_but_moving_point_is_preserved(self) -> None:
        samples = [
            sample(index, observation_id=index + 1) for index in range(12)
        ]
        samples.append(
            sample(
                12,
                observation_id=100,
                lat=51.901,
                lon=4.501,
                is_moving=1,
            )
        )
        records = compact_track(samples, settings())
        self.assertEqual(2, len(records))
        self.assertEqual("stationary", records[0].record_type)
        self.assertEqual(12, records[0].sample_count)
        self.assertEqual(660, records[0].duration_seconds)
        self.assertEqual("position", records[1].record_type)
        self.assertEqual(100, records[1].source_observation_id)

    def test_short_pause_is_not_collapsed(self) -> None:
        samples = [
            sample(index, observation_id=index + 1) for index in range(5)
        ]
        records = compact_track(samples, settings())
        self.assertEqual(5, len(records))
        self.assertTrue(all(record.record_type == "position" for record in records))

    def test_slow_spatial_drift_beyond_radius_is_not_collapsed(self) -> None:
        samples = [
            sample(
                index,
                observation_id=index + 1,
                lat=51.9 + index * 0.00006,
            )
            for index in range(12)
        ]
        records = compact_track(samples, settings())
        self.assertTrue(all(record.record_type == "position" for record in records))

    def test_daily_compactor_writes_derived_layer_and_keeps_raw_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "data" / "test.db", root / "data" / "raw")
            storage.initialize()
            compaction = settings()
            config = AppConfig(
                config_path=root / "config.toml",
                project_root=root,
                data_dir=root / "data",
                database=root / "data" / "test.db",
                raw_dir=root / "data" / "raw",
                provider=ProviderConfig(
                    name="test",
                    base_url="https://example.test",
                    token_env="TEST_TOKEN",
                    timeout_seconds=1,
                    max_retries=0,
                    max_pages=1,
                    request_gap_seconds=0,
                    user_agent="test",
                ),
                compaction=compaction,
                retention=RetentionConfig(
                    enabled=True,
                    cleanup_interval_hours=24,
                    require_completed_compaction=True,
                ),
                idle_sleep_seconds=1,
                areas=(),
            )
            target = CollectionTarget(
                id="nl:r01c01",
                area_id="nl",
                area_label="NL",
                tile_id="r01c01",
                bbox=BBox(4.0, 51.0, 5.0, 52.0),
                interval_seconds=60,
                initial_delay_seconds=0,
                priority=1,
            )
            raw_paths = []
            for index in range(12):
                fetched_at = datetime(
                    2026, 7, 26, 2, index, tzinfo=timezone.utc
                ).isoformat()
                result = FetchResult(
                    items=(
                        {
                            "trackID": "42",
                            "posTS": "2026-07-26T02:00:00Z",
                            "lat": 51.5,
                            "lon": 4.5,
                            "moving": False,
                        },
                    ),
                    pages=1,
                    reported_count=1,
                    reported_count_delta=0,
                    fetched_at_utc=fetched_at,
                    source_url="https://example.test",
                    elapsed_seconds=0.01,
                )
                run_id = storage.start_run("test", target)
                raw_path = storage.write_snapshot(
                    "test", target, run_id, result
                )
                storage.ingest(run_id, "test", target, result, raw_path)
                with storage.connect() as connection:
                    connection.execute(
                        """
                        UPDATE collection_runs
                        SET started_at_utc = ?, completed_at_utc = ?
                        WHERE run_id = ?
                        """,
                        (fetched_at, fetched_at, run_id),
                    )
                raw_paths.append(raw_path)

            compactor = DailyCompactor(config, storage)
            before_compaction = RawRetention(config, storage).prune()
            self.assertEqual(12, before_compaction.skipped_uncompacted)
            self.assertTrue(all(path.exists() for path in raw_paths))
            with self.assertRaisesRegex(ValueError, "is not ready"):
                compactor.compact_day(
                    date(2026, 7, 26),
                    now_utc=datetime(
                        2026, 7, 26, 12, 0, tzinfo=timezone.utc
                    ),
                )
            outcome = compactor.compact_day(
                date(2026, 7, 26), allow_incomplete=True
            )
            self.assertIsNotNone(outcome)
            assert outcome is not None
            self.assertEqual(12, outcome.source_samples)
            self.assertEqual(1, outcome.stationary_records)
            self.assertEqual(12, outcome.stationary_source_samples)
            self.assertTrue(Path(outcome.output_path).exists())
            self.assertTrue(all(path.exists() for path in raw_paths))
            with storage.connect() as connection:
                record = connection.execute(
                    """
                    SELECT record_type, sample_count
                    FROM daily_track_records
                    WHERE operational_date = '2026-07-26'
                    """
                ).fetchone()
            self.assertEqual("stationary", record["record_type"])
            self.assertEqual(12, record["sample_count"])

            # A completed database marker is not sufficient on its own: the
            # final archive must still exist before source data is eligible.
            compacted_path = Path(outcome.output_path)
            held_path = compacted_path.with_suffix(".held")
            compacted_path.replace(held_path)
            missing_archive = RawRetention(config, storage).prune(max_runs=5)
            self.assertEqual(0, missing_archive.candidate_runs)
            self.assertEqual(5, missing_archive.skipped_uncompacted)
            self.assertTrue(all(path.exists() for path in raw_paths))
            held_path.replace(compacted_path)

            # The same normalized observation can be referenced on the next
            # operational day. Cleaning the completed day must remove only the
            # old provenance links and keep the shared observation alive.
            next_fetched_at = datetime(
                2026, 7, 27, 2, 1, tzinfo=timezone.utc
            ).isoformat()
            next_result = FetchResult(
                items=result.items,
                pages=1,
                reported_count=1,
                reported_count_delta=0,
                fetched_at_utc=next_fetched_at,
                source_url="https://example.test",
                elapsed_seconds=0.01,
            )
            next_run = storage.start_run("test", target)
            next_path = storage.write_snapshot(
                "test", target, next_run, next_result
            )
            storage.ingest(
                next_run, "test", target, next_result, next_path
            )
            with storage.connect() as connection:
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET started_at_utc = ?, completed_at_utc = ?
                    WHERE run_id = ?
                    """,
                    (next_fetched_at, next_fetched_at, next_run),
                )

            held = RawRetention(config, storage).prune(
                now_utc=datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
            )
            self.assertEqual(0, held.raw_deleted)
            self.assertEqual(12, held.skipped_raw_too_new)
            self.assertEqual(12, held.provenance_links_deleted)
            self.assertEqual(0, held.observations_deleted)
            self.assertTrue(all(path.exists() for path in raw_paths))

            bounded = RawRetention(config, storage).prune(
                now_utc=datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc),
                max_runs=5,
                batch_pause_seconds=0.001,
            )
            self.assertEqual(5, bounded.candidate_runs)
            self.assertEqual(5, bounded.raw_deleted)
            self.assertEqual(7, sum(path.exists() for path in raw_paths))

            retention = RawRetention(config, storage).prune(
                now_utc=datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)
            )
            self.assertEqual(7, retention.raw_deleted)
            self.assertEqual(0, retention.skipped_raw_too_new)
            self.assertEqual(1, retention.skipped_uncompacted)
            self.assertEqual(0, retention.provenance_links_deleted)
            self.assertEqual(0, retention.observations_deleted)
            self.assertTrue(all(not path.exists() for path in raw_paths))
            self.assertTrue(next_path.exists())
            with storage.connect() as connection:
                deleted_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM collection_runs
                    WHERE raw_deleted_at_utc IS NOT NULL
                    """
                ).fetchone()[0]
                details_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM collection_runs
                    WHERE details_deleted_at_utc IS NOT NULL
                    """
                ).fetchone()[0]
                source_observations = connection.execute(
                    "SELECT COUNT(*) FROM observations"
                ).fetchone()[0]
                daily_records = connection.execute(
                    "SELECT COUNT(*) FROM daily_track_records"
                ).fetchone()[0]
            self.assertEqual(12, deleted_count)
            self.assertEqual(12, details_count)
            self.assertEqual(1, source_observations)
            self.assertEqual(1, daily_records)


if __name__ == "__main__":
    unittest.main()
