import unittest

from ship_analysis.models import normalize_observation


class ModelTests(unittest.TestCase):
    def test_normalizes_v2_fields(self) -> None:
        record = {
            "trackID": 42,
            "posTS": "2026-07-26T12:00:00Z",
            "lat": 51.9,
            "lon": 4.5,
            "sog": 12.3,
            "cog": 90.0,
            "moving": True,
            "inlen": 110,
            "inbm": 11.4,
            "positionISRS": "NLABC",
        }
        result = normalize_observation(record, "euris_v2", "2026-07-26T12:01:00Z")
        self.assertEqual("42", result.track_id)
        self.assertEqual(1, result.is_moving)
        self.assertEqual(110.0, result.length_m)
        self.assertEqual("NLABC", result.isrs_code)

    def test_same_track_position_has_stable_key(self) -> None:
        record = {
            "trackID": 42,
            "posTS": "2026-07-26T12:00:00Z",
            "lat": 51.9,
            "lon": 4.5,
        }
        first = normalize_observation(record, "euris_v2", "2026-07-26T12:01:00Z")
        second = normalize_observation(record, "euris_v2", "2026-07-26T12:02:00Z")
        self.assertEqual(first.observation_key, second.observation_key)


if __name__ == "__main__":
    unittest.main()

