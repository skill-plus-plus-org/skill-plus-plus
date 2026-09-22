"""Capitalize every word in a line of text."""


def titlecase(text):
    """Upper-case the first letter of every word and lower-case the rest."""
    result = ""
    previous = " "
    for char in text:
        if previous.isalpha():
            result = result + char.lower()
        else:
            result = result + char.upper()
        previous = char
    return result
