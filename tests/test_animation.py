import unittest

from backend.adapters.animation import _construct_body
from backend.animation import normalize_spec, phrase_time


class PhraseTimeTests(unittest.TestCase):
    def _words(self):
        pairs = [("As", 0.0), ("x", 0.3), ("grows", 0.6), ("the", 1.0),
                 ("area", 1.2), ("under", 1.6), ("the", 1.9), ("curve", 2.1),
                 ("climbs", 2.6), ("to", 3.0), ("sixteen", 3.2)]
        return [{"word": w, "start": s, "end": s + 0.2} for w, s in pairs]

    def test_contiguous_phrase_binds_to_first_word(self):
        # "the curve" -> the 'the' at 1.9 that begins the contiguous match.
        self.assertAlmostEqual(phrase_time("the curve", self._words()), 1.9)

    def test_single_salient_word(self):
        self.assertAlmostEqual(phrase_time("sixteen", self._words()), 3.2)

    def test_missing_phrase_returns_default(self):
        self.assertEqual(phrase_time("dragons", self._words(), default=-1.0), -1.0)

    def test_empty_inputs(self):
        self.assertEqual(phrase_time("", self._words(), default=7.0), 7.0)
        self.assertEqual(phrase_time("x", [], default=7.0), 7.0)


class NormalizeSpecTests(unittest.TestCase):
    def test_valid_code(self):
        spec = normalize_spec({"code": "self.wait(1)", "title": "  Demo  "})
        self.assertEqual(spec, {"code": "self.wait(1)", "title": "Demo"})

    def test_missing_or_empty_code_is_none(self):
        self.assertIsNone(normalize_spec({"code": "   "}))
        self.assertIsNone(normalize_spec({"title": "no code"}))
        self.assertIsNone(normalize_spec("not a dict"))

    def test_code_is_length_capped(self):
        spec = normalize_spec({"code": "x" * 50000})
        self.assertLessEqual(len(spec["code"]), 24000)


class ConstructBodyTests(unittest.TestCase):
    def test_plain_body_passthrough(self):
        self.assertEqual(_construct_body("self.wait(1)"), "self.wait(1)")

    def test_extracts_body_from_full_construct_def(self):
        code = "def construct(self):\n    self.wait(1)\n    self.play(x)"
        self.assertEqual(_construct_body(code), "self.wait(1)\nself.play(x)")

    def test_strips_stray_manim_import(self):
        self.assertEqual(_construct_body("from manim import *\nself.wait(1)"), "self.wait(1)")

    def test_empty_raises(self):
        with self.assertRaises(RuntimeError):
            _construct_body("   \n  ")


if __name__ == "__main__":
    unittest.main()
