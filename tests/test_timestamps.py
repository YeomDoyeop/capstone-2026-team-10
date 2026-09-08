import unittest

from src.timestamps import restore_original_segment_timestamps


class TimestampTests(unittest.TestCase):
    def test_restores_segments_to_original_timeline(self):
        segments, duration = restore_original_segment_timestamps(
            [
                {
                    "start": 0.25,
                    "end": 1.0,
                    "text": " 안녕하세요.",
                }
            ],
            1.0,
            2.0,
        )

        self.assertEqual(
            segments,
            [
                {
                    "start": 0.5,
                    "end": 2.0,
                    "text": " 안녕하세요.",
                }
            ],
        )
        self.assertEqual(duration, 2.0)


if __name__ == "__main__":
    unittest.main()
