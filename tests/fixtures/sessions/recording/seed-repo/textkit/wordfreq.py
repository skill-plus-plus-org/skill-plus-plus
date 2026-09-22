"""Print the most common words in a text file."""

import argparse

LETTERS = "abcdefghijklmnopqrstuvwxyz'"


def words(text):
    """Split text into lowercase words, dropping punctuation and digits."""
    cleaned = ""
    for char in text.lower():
        if char in LETTERS:
            cleaned = cleaned + char
        else:
            cleaned = cleaned + " "
    result = []
    for word in cleaned.split(" "):
        word = word.strip("'")
        if word != "":
            result.append(word)
    return result


def top_words(text):
    """The five most common words, most common first, ties alphabetical."""
    counts = {}
    for word in words(text):
        if word in counts:
            counts[word] = counts[word] + 1
        else:
            counts[word] = 1
    ranked = []
    for word in counts:
        ranked.append((-counts[word], word))
    ranked.sort()
    result = []
    for negative_count, word in ranked[:5]:
        result.append((word, -negative_count))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="the text file to read")
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8") as handle:
        text = handle.read()
    for word, count in top_words(text):
        print(f"{count:>5}  {word}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
