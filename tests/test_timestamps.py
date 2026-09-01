import unittest

from src.timestamps import restore_original_timestamps


class RestoreOriginalTimestampsTest(unittest.TestCase):
    def test_restores_speed_adjusted_timestamps_to_original_timeline(self) -> None:
        segments, duration = restore_original_timestamps(
            [{"start": 1.234, "end": 2.345, "text": "테스트"}],
            3.456,
            2.0,
        )

        self.assertEqual(segments, [{"start": 2.468, "end": 4.69, "text": "테스트"}])
        self.assertEqual(duration, 6.912)

    def test_keeps_values_when_speed_is_one(self) -> None:
        source_segments = [{"start": 1.0, "end": 2.0, "text": "테스트"}]

        segments, duration = restore_original_timestamps(source_segments, 2.0, 1.0)

        self.assertIs(segments, source_segments)
        self.assertEqual(duration, 2.0)


if __name__ == "__main__":
    unittest.main()
