"""Map narration phrases to the seconds they are spoken.

This is the voice-sync primitive. Anchoring by phrase — not a hardcoded time —
means the sync survives re-generating the audio: the timing follows the words.

``CueTimeline`` resolves a scene's cues **in the order the code asks for them**,
from a cursor that only moves forward. That is what makes long (merged) scenes
work: a 60-second narration says "the curve" five times, and resolving each cue
independently binds all five to the *first* occurrence, collapsing the timeline
to its opening seconds. Resolving in call order hands each cue the next unused
occurrence and is monotonic by construction.

``build_cue_schedule`` resolves every ``self.cue(...)`` in the authored code up
front, so the renderer knows the whole timeline before the first frame — it can
both hold each event to its word and cap each animation so it cannot run past
the next cue.
"""
from __future__ import annotations

import re

__all__ = [
    "CueTimeline",
    "build_cue_schedule",
    "extract_cue_phrases",
    "phrase_time",
    "word_stream",
]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# `self.cue("...")` / `self.cue('...')` calls, in source order.
_CUE_CALL_RE = re.compile(r"""\bself\s*\.\s*cue\s*\(\s*(['"])(.*?)\1""")
# Function words say nothing about *where* in the narration we are, so a cue
# must never fall back to matching one of them on its own.
_STOPWORDS = frozenset("""
    a an and are as at be been but by can did do does for from had has have he
    her here his how i if in into is it its me my no not now of on or our out
    she so than that the their them then there these they this those to up us
    was we were what when which who will with you your
""".split())


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def word_stream(words: list[dict]) -> list[tuple[str, float]]:
    """Flatten timestamp words into [(normalized_token, start_seconds), ...]."""
    stream: list[tuple[str, float]] = []
    for entry in words or []:
        try:
            start = float(entry.get("start"))
        except (TypeError, ValueError):
            continue
        for tok in _tokens(str(entry.get("word", ""))):
            stream.append((tok, start))
    return stream


def extract_cue_phrases(code: str) -> list[str]:
    """The phrases passed to ``self.cue(...)`` in ``code``, in source order."""
    return [match.group(2) for match in _CUE_CALL_RE.finditer(code or "")]


def _is_anchor_token(token: str) -> bool:
    """Whether a single word is specific enough to hang a cue on by itself."""
    return len(token) >= 3 and token not in _STOPWORDS


def _is_anchor(tokens: list[str]) -> bool:
    """Whether a verbatim match on these tokens is specific enough to trust."""
    return len(tokens) > 1 or (len(tokens) == 1 and _is_anchor_token(tokens[0]))


class CueTimeline:
    """Locate narration phrases in the spoken word stream.

    ``find`` is the stateless primitive: the token index of a phrase, optionally
    restricted to a window. ``resolve`` is the forward-only convenience used at
    render time — each call starts where the previous one matched, so repeated
    phrases map to successive occurrences instead of collapsing onto the first.

    Both return ``None`` — not 0.0 — for a phrase that is not narrated, so the
    caller can leave the event where it is rather than yanking it to the start.
    """

    def __init__(self, words: list[dict], duration: float = 0.0) -> None:
        stream = word_stream(words)
        self._tokens = [tok for tok, _ in stream]
        self._starts = [start for _, start in stream]
        self.duration = float(duration or 0.0)
        self._cursor = 0

    def time_at(self, index: int) -> float:
        return self._starts[index]

    def find(self, phrase: str, start: int = 0, stop: int | None = None,
             exact: bool = False) -> int | None:
        """Token index of ``phrase`` within ``[start, stop)``, else None.

        With ``exact`` the whole phrase must appear verbatim. Otherwise its
        longest contiguous fragment counts too, which absorbs near-misses: the
        model writes "the area equals 16" for narration that says "that area is
        exactly sixteen", and "area" still lands the cue on the right word.
        """
        tokens = _tokens(phrase)
        if not self._tokens or not _is_anchor(tokens):
            return None
        stop = len(self._tokens) if stop is None else min(stop, len(self._tokens))
        found = self._find(tokens, start, stop)
        if found is not None or exact:
            return found
        return self._find_fragment(tokens, start, stop)

    def resolve(self, phrase: str) -> float | None:
        """The next spoken start of ``phrase``, advancing the cursor past it."""
        index = self.find(phrase, self._cursor)
        if index is None and self._cursor:
            # Quoted out of order. Its real (earlier) time still beats nothing;
            # the cursor stays put so later cues are unaffected.
            index = self.find(phrase, 0)
        if index is None:
            return None
        self._cursor = max(self._cursor, index + 1)
        return self._starts[index]

    def _find(self, tokens: list[str], start: int, stop: int) -> int | None:
        size = len(tokens)
        for i in range(max(0, start), stop - size + 1):
            if self._tokens[i : i + size] == tokens:
                return i
        return None

    def _find_fragment(self, tokens: list[str], start: int, stop: int) -> int | None:
        """Longest contiguous fragment of ``tokens`` inside the window."""
        for size in range(len(tokens) - 1, 1, -1):
            for offset in range(len(tokens) - size + 1):
                found = self._find(tokens[offset : offset + size], start, stop)
                if found is not None:
                    return found
        # Single tokens last, most distinctive (longest) first.
        for token in sorted(set(tokens), key=len, reverse=True):
            if not _is_anchor_token(token):
                continue
            found = self._find([token], start, stop)
            if found is not None:
                return found
        return None


def build_cue_schedule(code: str, words: list[dict], duration: float = 0.0) -> list[dict]:
    """Resolve every ``self.cue(...)`` in ``code`` up front, in source order.

    Returns ``[{"phrase": str, "time": float | None}, ...]``. Resolving here — in
    the parent, before a frame is drawn — rather than live mid-render is what
    lets the renderer see every upcoming cue and budget each animation to end
    before the next one.

    Two passes, because one bad cue must not be able to wreck the rest of a long
    scene. Verbatim matches are resolved first and become fixed anchors; every
    fuzzy match is then confined to the word window between its neighbouring
    anchors. So a phrase the model half-remembered can only be a little early or
    late — it can never leap ten seconds ahead and swallow the cues after it.
    """
    phrases = extract_cue_phrases(code)
    timeline = CueTimeline(words, duration)

    found: list[int | None] = []
    cursor = 0
    for phrase in phrases:
        index = timeline.find(phrase, cursor, exact=True)
        found.append(index)
        if index is not None:
            cursor = index + 1

    for i, phrase in enumerate(phrases):
        if found[i] is not None:
            continue
        low = next((found[j] + 1 for j in range(i - 1, -1, -1) if found[j] is not None), 0)
        high = next((found[j] for j in range(i + 1, len(phrases)) if found[j] is not None), None)
        found[i] = timeline.find(phrase, low, high)

    return [
        {"phrase": phrase, "time": None if index is None else timeline.time_at(index)}
        for phrase, index in zip(phrases, found)
    ]


def phrase_time(phrase: str, words: list[dict], default: float = 0.0) -> float:
    """One-shot lookup: the spoken start of ``phrase``, or ``default``."""
    found = CueTimeline(words).resolve(phrase)
    return default if found is None else found
