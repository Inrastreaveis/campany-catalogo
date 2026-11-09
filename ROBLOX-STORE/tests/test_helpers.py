import unittest
from src.ui.views import format_robux


class TestFormatRobux(unittest.TestCase):
    def test_integer_price(self):
        self.assertEqual(format_robux(1200), "1.200 Robux")

    def test_missing_price(self):
        self.assertEqual(format_robux(None), "Indisponível")


if __name__ == "__main__":
    unittest.main()
