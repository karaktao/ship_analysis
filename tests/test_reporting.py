from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ship_analysis.compaction import operational_day_bounds
from ship_analysis.config import load_config
from ship_analysis.reporting import CollectionReporter
from ship_analysis.storage import Storage


UTC = timezone.utc


class ReportingTests(unittest.TestCase):
    def _project(self, root: Path):
        project_root = Path(__file__).resolve().parents[1]
        base = load_config(project_root / "config" / "regions.toml")
        config = replace(
            base,
            data_dir=root,
            database=root / "test.db",
            raw_dir=root / "raw",
        )
        storage = Storage(config.database, config.raw_dir)
        storage.initialize()
        return config, storage, CollectionReporter(config, storage)

    @staticmethod
    def _insert_run(
        storage: Storage,
        run_id: str,
        started_at: datetime,
        *,
        tile_id: str,
        status: str,
        items: int = 0,
        unique: int = 0,
        inserted: int = 0,
        existing: int = 0,
        page_duplicates: int = 0,
        elapsed: float | None = None,
    ) -> None:
        completed_at = (
            started_at + timedelta(seconds=1) if status != "running" else None
        )
        with storage.connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_runs (
                    run_id, provider, area_id, tile_id, bbox,
                    started_at_utc, completed_at_utc, status, pages,
                    reported_count, reported_count_delta, item_count,
                    unique_item_count, inserted_count, duplicate_count,
                    within_run_duplicate_count, outside_bbox_count,
                    elapsed_seconds
                ) VALUES (
                    ?, 'test', 'nl_coverage', ?, '3,50,4,51',
                    ?, ?, ?, 1, ?, 0, ?, ?, ?, ?, ?, 0, ?
                )
                """,
                (
                    run_id,
                    tile_id,
                    started_at.isoformat(timespec="milliseconds"),
                    (
                        completed_at.isoformat(timespec="milliseconds")
                        if completed_at
                        else None
                    ),
                    status,
                    items,
                    items,
                    unique,
                    inserted,
                    existing,
                    page_duplicates,
                    elapsed,
                ),
            )

    def test_minute_stats_keep_received_unique_and_new_counts_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config, storage, reporter = self._project(Path(directory))
            start = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
            self._insert_run(
                storage,
                "one",
                start + timedelta(seconds=1),
                tile_id="r01c01",
                status="completed",
                items=10,
                unique=9,
                inserted=6,
                existing=3,
                page_duplicates=1,
                elapsed=0.2,
            )
            self._insert_run(
                storage,
                "two",
                start + timedelta(seconds=2),
                tile_id="r01c02",
                status="completed",
                items=5,
                unique=5,
                inserted=3,
                existing=2,
                elapsed=0.4,
            )
            self._insert_run(
                storage,
                "failed",
                start + timedelta(seconds=3),
                tile_id="r01c03",
                status="failed",
            )

            stats, became_final = reporter.materialize_period(
                start,
                start + timedelta(minutes=1),
                "minute",
                is_final=True,
            )

            self.assertTrue(became_final)
            self.assertEqual(48, stats.expected_run_count)
            self.assertEqual(3, stats.observed_run_count)
            self.assertEqual(2, stats.completed_run_count)
            self.assertEqual(1, stats.failed_run_count)
            self.assertEqual(15, stats.received_item_count)
            self.assertEqual(14, stats.unique_item_count)
            self.assertEqual(9, stats.new_observation_count)
            self.assertEqual(5, stats.existing_observation_count)
            self.assertEqual(1, stats.within_run_duplicate_count)
            self.assertEqual(0, stats.pagination_anomaly_run_count)
            self.assertAlmostEqual(0.4, stats.p95_elapsed_seconds or 0)
            self.assertEqual(
                stats.period_start_utc,
                reporter.list_periods("minute", 1)[0].period_start_utc,
            )
            self.assertEqual(config.compaction.timezone, stats.timezone)

    def test_daily_summary_is_written_and_uses_dst_aware_expected_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, storage, reporter = self._project(root)
            operational_date = date(2026, 10, 24)
            start, end = operational_day_bounds(
                operational_date, config.compaction
            )
            self.assertEqual(timedelta(hours=25), end - start)
            self._insert_run(
                storage,
                "one",
                start + timedelta(minutes=1),
                tile_id="r01c01",
                status="completed",
                items=12,
                unique=11,
                inserted=10,
                existing=1,
                page_duplicates=1,
                elapsed=0.5,
            )

            summary = reporter.generate_daily_summary(
                operational_date,
                force=True,
                allow_incomplete=True,
            )

            self.assertEqual("partial", summary.health_status)
            self.assertEqual(
                72_000,
                summary.metrics["collection"]["expected_run_count"],
            )
            self.assertEqual(
                12,
                summary.metrics["collection"]["received_item_count"],
            )
            self.assertEqual("test", summary.metrics["channels"][0]["provider"])
            self.assertEqual(
                12,
                summary.metrics["channels"][0]["received_item_count"],
            )
            output = Path(summary.output_path)
            self.assertTrue(output.is_file())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("2026-10-24", payload["operational_date"])
            self.assertIn("完成 1/72,000", summary.summary_text)
            self.assertTrue(summary.metrics["coverage"]["is_partial"])
            with storage.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM daily_collection_summaries"
                ).fetchone()[0]
            self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
