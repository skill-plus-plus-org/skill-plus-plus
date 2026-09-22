import unittest

from textkit.slugify import slugify


class TestSlugify(unittest.TestCase):
    def test_lowercases_and_joins_with_hyphens(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_drops_punctuation(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_collapses_spaces(self):
        self.assertEqual(slugify("  spaced   out  "), "spaced-out")

    def test_keeps_digits(self):
        self.assertEqual(slugify("Top 10 Tips"), "top-10-tips")


if __name__ == "__main__":
    unittest.main()
