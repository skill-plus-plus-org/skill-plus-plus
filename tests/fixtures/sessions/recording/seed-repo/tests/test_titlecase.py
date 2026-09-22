import unittest

from textkit.titlecase import titlecase


class TestTitlecase(unittest.TestCase):
    def test_capitalizes_each_word(self):
        self.assertEqual(titlecase("hello world"), "Hello World")

    def test_lowercases_the_rest(self):
        self.assertEqual(titlecase("HELLO THERE"), "Hello There")

    def test_keeps_spacing(self):
        self.assertEqual(titlecase("mixed  CaSe"), "Mixed  Case")


if __name__ == "__main__":
    unittest.main()
