from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ship_analysis.config import BBox, CollectionTarget
from ship_analysis.providers import FetchResult
from ship_analysis.storage import Storage


class StorageTests(unittest.TestCase):
    def test_initialize_migrates_daily_summary_health_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "test.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE daily_collection_summaries (
                        operational_date TEXT PRIMARY KEY,
                        timezone TEXT NOT NULL,
                        day_start_utc TEXT NOT NULL,
                        day_end_utc TEXT NOT NULL,
                        generated_at_utc TEXT NOT NULL,
                        health_status TEXT NOT NULL CHECK (
                            health_status IN (
                                'healthy', 'warning', 'critical', 'no_data'
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
                    VALUES (
                        '2026-07-26', 'Europe/Amsterdam', 'start', 'end',
                        'generated', 'critical', 'summary', '{}', 'output'
                    )
                    """
                )
            connection.close()

            storage = Storage(database, root / "raw")
            storage.initialize()
            with storage.connect() as connection:
                sql = connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table'
                      AND name = 'daily_collection_summaries'
                    """
                ).fetchone()[0]
                preserved = connection.execute(
                    """
                    SELECT health_status
                    FROM daily_collection_summaries
                    WHERE operational_date = '2026-07-26'
                    """
                ).fetchone()[0]
            self.assertIn("'partial'", sql)
            self.assertEqual("critical", preserved)

    def test_ingest_deduplicates_overlapping_area_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "test.db", root / "raw")
            storage.initialize()
            target = CollectionTarget(
                id="test:all",
                area_id="test",
                area_label="Test",
                tile_id="all",
                bbox=BBox(4.0, 51.0, 5.0, 52.0),
                interval_seconds=60,
                initial_delay_seconds=0,
                priority=1,
            )
            result = FetchResult(
                items=(
                    {
                        "trackID": "123",
                        "posTS": "2026-07-26T12:00:00Z",
                        "lat": 51.5,
                        "lon": 4.5,
                    },
                ),
                pages=1,
                reported_count=1,
                reported_count_delta=0,
                fetched_at_utc=datetime.now(timezone.utc).isoformat(),
                source_url="https://example.test",
                elapsed_seconds=0.1,
            )

            first_run = storage.start_run("test", target)
            first_path = storage.write_snapshot("test", target, first_run, result)
            self.assertEqual(
                (1, 0, 0),
                storage.ingest(first_run, "test", target, result, first_path),
            )

            second_run = storage.start_run("test", target)
            second_path = storage.write_snapshot("test", target, second_run, result)
            self.assertEqual(
                (0, 1, 0),
                storage.ingest(second_run, "test", target, result, second_path),
            )
            self.assertEqual(1, storage.counts()["observations"])
            with storage.connect() as connection:
                raw_json = connection.execute(
                    "SELECT raw_json FROM observations"
                ).fetchone()[0]
                inside = connection.execute(
                    """
                    SELECT inside_requested_bbox
                    FROM collection_observations
                    WHERE run_id = ?
                    """,
                    (first_run,),
                ).fetchone()[0]
            self.assertEqual("", raw_json)
            self.assertEqual(1, inside)
            aggregate = storage.aggregate_runs([first_run, second_run], target.bbox)
            self.assertEqual(1, aggregate["distinct_observations"])
            self.assertEqual(1, aggregate["inside_area_bbox"])


if __name__ == "__main__":
    unittest.main()
