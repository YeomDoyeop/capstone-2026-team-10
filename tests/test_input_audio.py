import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.input_audio import AudioDownloadError, _atempo_filters, download_audio


class AtempoFiltersTest(unittest.TestCase):
    def test_accepts_two_times_speed(self) -> None:
        self.assertEqual(_atempo_filters(2.0), "atempo=2.0")

    def test_rejects_more_than_two_times_speed(self) -> None:
        with self.assertRaises(AudioDownloadError):
            _atempo_filters(2.1)


class DownloadAudioTest(unittest.TestCase):
    def test_uses_separate_timeouts_and_reports_received_bytes(self) -> None:
        class Response:
            is_redirect = False
            status_code = 200
            headers = {"Content-Length": str(2 * 1024 * 1024)}

            def iter_content(self, chunk_size):
                assert chunk_size == 1024 * 1024
                yield b"a" * chunk_size
                yield b"b" * chunk_size

            def close(self):
                pass

        reports: list[tuple[int, str]] = []
        with tempfile.TemporaryDirectory() as directory, patch("src.input_audio._validate_public_https_url"), patch("src.input_audio.requests.get", return_value=Response()) as get:
            path = download_audio("https://example.com/audio.mp3", Path(directory), progress_callback=lambda progress, message: reports.append((progress, message)))
            self.assertEqual(path.read_bytes(), b"a" * 1024 * 1024 + b"b" * 1024 * 1024)
            self.assertEqual(get.call_args.kwargs["timeout"], (15, 60))
            self.assertEqual(reports[-1][0], 14)


if __name__ == "__main__":
    unittest.main()
