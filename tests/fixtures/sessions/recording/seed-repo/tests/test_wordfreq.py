import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from textkit.wordfreq import main, top_words, words


class TestWords(unittest.TestCase):
    def test_lowercases_and_drops_punctuation(self):
        self.assertEqual(words("Hello, hello! World."), ["hello", "hello", "world"])

    def test_keeps_apostrophes_inside_words(self):
        self.assertEqual(words("It's fine"), ["it's", "fine"])


class TestTopWords(unittest.TestCase):
    def test_most_common_first(self):
        self.assertEqual(top_words("b a b c b a"), [("b", 3), ("a", 2), ("c", 1)])

    def test_ties_are_alphabetical(self):
        self.assertEqual(top_words("pear apple"), [("apple", 1), ("pear", 1)])

    def test_at_most_five(self):
        self.assertEqual(len(top_words("a b c d e f g")), 5)


class TestMain(unittest.TestCase):
    def test_prints_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("sun sun rain")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main([path]), 0)
        self.assertEqual(out.getvalue().split(), ["2", "sun", "1", "rain"])


if __name__ == "__main__":
    unittest.main()
