import unittest

from src.input_audio import _atempo_filters


class AtempoFiltersTest(unittest.TestCase):
    def test_creates_filter_chain_for_four_times_speed(self) -> None:
        self.assertEqual(_atempo_filters(4.0), "atempo=2.0,atempo=2.0")


if __name__ == "__main__":
    unittest.main()
