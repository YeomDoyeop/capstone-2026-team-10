import unittest

from src.request import RequestValidationError, parse_request


class ParseRequestTest(unittest.TestCase):
    def test_default_values(self) -> None:
        result = parse_request({"audio_url": "https://example.com/audio.mp3"})
        self.assertEqual(result.language, "ko")
        self.assertEqual(result.speed, 1.0)

    def test_rejects_invalid_speed(self) -> None:
        with self.assertRaises(RequestValidationError):
            parse_request({"audio_url": "https://example.com/audio.mp3", "speed": 4.1})

    def test_rejects_slowdown(self) -> None:
        with self.assertRaises(RequestValidationError):
            parse_request({"audio_url": "https://example.com/audio.mp3", "speed": 0.9})

    def test_accepts_four_times_speed(self) -> None:
        result = parse_request({"audio_url": "https://example.com/audio.mp3", "speed": 4.0})
        self.assertEqual(result.speed, 4.0)

    def test_requires_audio_url(self) -> None:
        with self.assertRaises(RequestValidationError):
            parse_request({})


if __name__ == "__main__":
    unittest.main()
