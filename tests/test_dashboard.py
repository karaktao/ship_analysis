from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ship_analysis.config import load_config
from ship_analysis.dashboard import DashboardSnapshot, export_dashboard_snapshot
from ship_analysis.storage import Storage


class DashboardTests(unittest.TestCase):
    def test_dashboard_snapshot_has_health_volume_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
            now = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)

            payload = DashboardSnapshot(config, storage).build(now)

            self.assertEqual("no_data", payload["collector"]["status"])
            self.assertEqual("no_data", payload["health"]["status"])
            self.assertEqual(24, len(payload["volume"]["hourly"]))
            self.assertEqual(48, payload["collector"]["targetCount"])
            self.assertEqual(60, payload["collector"]["intervalSeconds"])
            self.assertEqual(24, payload["storage"]["rawRetentionHours"])
            self.assertIn("diskFreeBytes", payload["storage"])

            output = root / "dashboard-snapshot.json"
            export_dashboard_snapshot(
                config, storage, output, mode="test-snapshot"
            )
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("test-snapshot", saved["mode"])
            self.assertEqual(48, saved["collector"]["targetCount"])
            self.assertEqual(2, saved["schemaVersion"])

    def test_first_hour_is_partial_not_schedule_anomaly(self) -> None:
        health = DashboardSnapshot._health_payload(
            collector_status="online",
            freshness=5,
            observation_window_seconds=600,
            last_hour={
                "expectedRuns": 2880,
                "observedRuns": 48,
                "completedRuns": 48,
                "failedRuns": 0,
                "completionRate": 48 / 2880,
                "requestSuccessRate": 1.0,
            },
            storage={
                "diskTotalBytes": 250 * 1024**3,
                "diskFreeBytes": 240 * 1024**3,
                "walBytes": 0,
                "logicalDatabaseBytes": 4 * 1024**2,
            },
            summary=None,
            compaction=None,
        )

        self.assertEqual("partial", health["status"])
        self.assertFalse(health["hasAnomaly"])
        self.assertEqual(["startup_window"], [
            reason["code"] for reason in health["reasons"]
        ])


if __name__ == "__main__":
    unittest.main()
