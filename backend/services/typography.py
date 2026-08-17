"""Punctuation the audience reads, as opposed to punctuation the voice reads.

A dash earns its place in narration: the voice takes a beat on it, and the
alignment timestamps show it consuming real time. It earns nothing on screen.
In a caption it hangs at the end of a line with air on both sides; in a title or
a description it is the tell of generated copy. The pause it marks is what a
comma already means, so anything the audience reads gets the comma and the
narration keeps the dash.

A hyphen inside a word is spelling, not punctuation ("full-grown", "royalty-free",
"open-source"), and a dash between numbers is a range. Both survive.
"""
from __future__ import annotations

import re

# Every dash a narrator reads as a pause. The unicode ones are punctuation
# wherever they appear; a plain hyphen only counts when it has air around it,
# which is what separates a dash from a hyphenated word.
UNICODE_DASHES = "‐‑‒–—―−"
DASHES = "-" + UNICODE_DASHES
_TERMINALS = ",.;:!?"
_CLOSERS = "\"')]”’"
_H = r"[^\S\n]"  # horizontal whitespace only: newlines are layout, not spacing

_RANGE = re.compile(rf"(?<=\d){_H}*[{UNICODE_DASHES}]+{_H}*(?=\d)")
_PUNCT_DASH = re.compile(
    rf"{_H}+[{DASHES}]+{_H}*"  # " - x" and " -x"
    rf"|{_H}*[{DASHES}]+{_H}+"  # "- x"
    rf"|[{UNICODE_DASHES}]+"  # "x—y", with no air at all
)


def with_comma(text: str) -> str:
    """``text`` ending in a mark, adding a comma only if it has none."""
    bare = text.rstrip(_CLOSERS)
    if not bare or bare[-1] in _TERMINALS:
        return text
    return text + ","


def _replacement(match: re.Match) -> str:
    head = match.string[: match.start()].rstrip(" \t")
    tail = match.string[match.end():]
    # Keep a line break that followed the dash; the dash going away must not
    # pull the next line up.
    gap = "" if not tail or tail[0] == "\n" else " "
    if not head or head.endswith("\n"):
        return ""  # nothing on this line to hang a comma on
    if not tail.strip():
        return ""  # nothing follows it either
    bare = head.rstrip(_CLOSERS)
    if bare and bare[-1] in _TERMINALS:
        return gap  # already punctuated; the dash was doubling up
    return f",{gap}"


def dashless(text: str) -> str:
    """Prose with dashes-as-punctuation rewritten as commas.

    Ranges become a plain hyphen ("1200–800 BC" -> "1200-800 BC") rather than a
    comma, which would turn a span into a list.
    """
    if not text:
        return text
    return _PUNCT_DASH.sub(_replacement, _RANGE.sub("-", text))


def dashless_copy(value):
    """``dashless`` over a metadata value: a string, or a list or dict of them.

    Used on the copy the script model writes — title, description, hook, cover
    text, hashtags. Deliberately not used on soundtrack attribution, which is a
    licence obligation to reproduce a credit as it was given, not copy of ours
    to edit.
    """
    if isinstance(value, str):
        return dashless(value)
    if isinstance(value, list):
        return [dashless_copy(v) for v in value]
    if isinstance(value, dict):
        return {k: dashless_copy(v) for k, v in value.items()}
    return value
