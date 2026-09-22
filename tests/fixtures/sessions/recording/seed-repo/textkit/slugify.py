"""Turn a title into a URL slug."""

ALLOWED = "abcdefghijklmnopqrstuvwxyz0123456789"


def slugify(text):
    """Lowercase the text, keep letters and digits, join words with hyphens."""
    result = ""
    previous_was_hyphen = False
    for char in text.lower():
        if char in ALLOWED:
            result = result + char
            previous_was_hyphen = False
        elif char == " " or char == "-":
            if not previous_was_hyphen and result != "":
                result = result + "-"
                previous_was_hyphen = True
    if result.endswith("-"):
        result = result[:-1]
    return result
