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
    def test_dashboard_snapshot_has_complete_grid_and_timelines(self) -> None:
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
            self.assertEqual(48, len(payload["tiles"]))
            self.assertEqual(60, len(payload["timelines"]["minute"]))
            self.assertEqual(24, len(payload["timelines"]["hour"]))
            self.assertEqual(14, len(payload["timelines"]["day"]))
            self.assertEqual(48, payload["current"]["minute"]["expectedRuns"])

            output = root / "dashboard-snapshot.json"
            export_dashboard_snapshot(
                config, storage, output, mode="test-snapshot"
            )
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("test-snapshot", saved["mode"])
            self.assertEqual(48, saved["collector"]["targetCount"])


if __name__ == "__main__":
    unittest.main()
