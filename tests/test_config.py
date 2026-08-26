from pathlib import Path
import unittest

from ship_analysis.config import AreaConfig, BBox, expand_area, load_config


class ConfigTests(unittest.TestCase):
    def test_grid_expansion_covers_bbox_without_gaps(self) -> None:
        area = AreaConfig(
            id="test",
            label="Test",
            bbox=BBox(3.0, 50.0, 7.0, 53.0),
            interval_seconds=900,
            grid_columns=4,
            grid_rows=3,
            start_delay_seconds=5,
            stagger_seconds=10,
            priority=1,
            enabled=True,
        )
        targets = expand_area(area)
        self.assertEqual(12, len(targets))
        self.assertEqual(BBox(3.0, 50.0, 4.0, 51.0), targets[0].bbox)
        self.assertEqual(BBox(6.0, 52.0, 7.0, 53.0), targets[-1].bbox)
        self.assertEqual(115, targets[-1].initial_delay_seconds)

    def test_project_config_loads(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        config = load_config(project_root / "config" / "regions.toml")
        self.assertEqual(1, len(config.areas))
        self.assertEqual(48, len(config.targets()))
        self.assertTrue(all(target.interval_seconds == 60 for target in config.targets()))
        self.assertEqual(
            list(range(48)),
            [target.initial_delay_seconds for target in config.targets()],
        )
        self.assertEqual(25, config.provider.timeout_seconds)
        self.assertEqual(2, config.provider.max_retries)
        self.assertEqual(45, config.provider.request_budget_seconds)
        self.assertEqual(8, config.provider.circuit_failure_threshold)
        self.assertEqual(60, config.provider.circuit_cooldown_seconds)
        self.assertEqual(4, config.collection_workers)
        self.assertEqual("Europe/Amsterdam", config.compaction.timezone)
        self.assertEqual(4, config.compaction.day_boundary_hour)
        self.assertEqual(24, config.retention.cleanup_interval_hours)
        self.assertEqual(24, config.retention.raw_min_age_hours)
        self.assertEqual(14, config.retention.run_detail_days)
        self.assertEqual(25_000, config.retention.delete_batch_size)
        self.assertTrue(config.retention.require_completed_compaction)


if __name__ == "__main__":
    unittest.main()
