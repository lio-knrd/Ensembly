"""Voice-sync resolution for animation scenes.

The failure these cover is drift on long (merged) animations: cues that all
collapsed onto the first occurrence of a repeated phrase, and a half-remembered
phrase that leapt far ahead and swallowed every cue after it. Resolution is now
ordered, forward-only, and — for anything short of a verbatim quote — confined
between the surrounding verbatim anchors.
"""
import unittest

from backend.adapters.animation import _runner_script
from backend.animation.narration import (
    CueTimeline,
    build_cue_schedule,
    extract_cue_phrases,
)

NARRATION = (
    "We start with a simple curve. As x grows the curve rises slowly. "
    "Now look at the area under the curve between zero and four. "
    "That area is exactly sixteen. Compare the curve to a straight line. "
    "The straight line grows steadily but the curve grows faster. "
    "So the area under the curve wins in the end."
)


def _words(text=NARRATION, step=0.5):
    """Word timings with one word every ``step`` seconds."""
    return [
        {"word": word, "start": round(i * step, 3), "end": round(i * step + 0.3, 3)}
        for i, word in enumerate(text.split())
    ]


def _times(code, words=None):
    return [entry["time"] for entry in build_cue_schedule(code, words or _words())]


class ExtractCuePhrasesTests(unittest.TestCase):
    def test_phrases_in_source_order_both_quote_styles(self):
        code = (
            "self.play_at(self.cue('first one'), Create(a))\n"
            'self.play_at(self.cue("second one"), FadeIn(b))\n'
            "t = self.cue( 'third one' )\n"
        )
        self.assertEqual(extract_cue_phrases(code),
                         ["first one", "second one", "third one"])

    def test_ignores_other_calls_and_empty_code(self):
        self.assertEqual(extract_cue_phrases("self.play(Create(x))\nother.cue('no')"), [])
        self.assertEqual(extract_cue_phrases(""), [])


class RepeatedPhraseTests(unittest.TestCase):
    def test_each_cue_takes_the_next_occurrence(self):
        # "the curve" is spoken five times; five cues must map to five moments.
        code = "\n".join(["self.cue('the curve')"] * 5)
        times = _times(code)
        self.assertEqual(len(set(times)), 5)
        self.assertEqual(times, sorted(times))

    def test_more_cues_than_occurrences_still_only_moves_forward(self):
        # Nothing is left to match, so the extras degrade to fragments of the
        # phrase. They must still advance, and stay inside the scene.
        times = _times("\n".join(["self.cue('wins in the end')"] * 3))
        self.assertEqual(times, sorted(t for t in times if t is not None))


class OrderingTests(unittest.TestCase):
    def test_cues_resolve_in_narration_order(self):
        code = (
            "self.cue('a simple curve')\n"
            "self.cue('the area under the curve')\n"
            "self.cue('a straight line')\n"
            "self.cue('wins in the end')\n"
        )
        times = _times(code)
        self.assertNotIn(None, times)
        self.assertEqual(times, sorted(times))

    def test_a_phrase_quoted_out_of_order_does_not_rewind_later_cues(self):
        code = (
            "self.cue('a straight line')\n"
            "self.cue('a simple curve')\n"   # spoken much earlier
            "self.cue('wins in the end')\n"  # must still land at the end
        )
        first, _out_of_order, last = _times(code)
        self.assertGreater(last, first)


class FuzzyMatchTests(unittest.TestCase):
    def test_near_miss_phrase_stays_between_its_anchors(self):
        # "the area equals 16" is not narrated. Confined to the window, it can
        # only be a little off; unconfined it used to jump to the final
        # "the area under the curve" and strand every cue after it.
        code = (
            "self.cue('the area under the curve')\n"
            "self.cue('the area equals 16')\n"
            "self.cue('a straight line')\n"
        )
        before, fuzzy, after = _times(code)
        self.assertIsNotNone(fuzzy)
        self.assertGreater(fuzzy, before)
        self.assertLess(fuzzy, after)

    def test_unnarrated_phrase_is_none_not_zero(self):
        # None means "play straight after the previous event"; 0.0 would drag
        # the event to the start of the scene and collapse what follows.
        self.assertEqual(_times("self.cue('dragons and wizards')"), [None])

    def test_lone_common_word_is_not_used_as_an_anchor(self):
        self.assertEqual(_times("self.cue('the')"), [None])


class CueTimelineTests(unittest.TestCase):
    def test_resolve_is_forward_only(self):
        timeline = CueTimeline(_words())
        first = timeline.resolve("the curve")
        second = timeline.resolve("the curve")
        self.assertGreater(second, first)

    def test_find_respects_the_window(self):
        timeline = CueTimeline(_words())
        first = timeline.find("the curve", exact=True)
        self.assertIsNotNone(first)
        self.assertIsNone(timeline.find("the curve", 0, first, exact=True))
        self.assertGreaterEqual(timeline.find("the curve", first + 1, exact=True), first + 1)

    def test_empty_inputs(self):
        self.assertIsNone(CueTimeline([]).resolve("anything"))
        self.assertIsNone(CueTimeline(_words()).resolve(""))


class RunnerScriptTests(unittest.TestCase):
    """The generated runner must time off Manim's clock, not an estimate."""

    CODE = "self.play_at(self.cue('a simple curve'), Create(c), run_time=1.0)"

    def _script(self):
        return _runner_script(self.CODE, _words(), 30.0, (1080, 1920))

    def test_no_hand_rolled_clock(self):
        script = self._script()
        # Estimating each animation's length is what made error accumulate.
        self.assertNotIn("_elapsed", script)
        self.assertIn("dt = target - self.time", script)

    def test_cue_schedule_is_resolved_into_the_script(self):
        script = self._script()
        self.assertIn("a simple curve", script)
        self.assertIn("CUES = json.loads(", script)

    def test_body_is_still_extracted_and_padded_to_duration(self):
        script = _runner_script("def construct(self):\n    self.wait(1)", _words(), 30.0, (1080, 1920))
        self.assertIn("DURATION = 30.0", script)
        self.assertTrue(script.rstrip().endswith("self.hold_until(DURATION)"))


if __name__ == "__main__":
    unittest.main()
