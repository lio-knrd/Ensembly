"""Script-level merge of consecutive animation scenes + deferred code authoring.

Consecutive animation scenes are combined into one BEFORE audio (so each becomes
one continuous animation with one voice take), and the script LLM no longer
authors Manim code — that happens post-audio. These cover the pure
narration/structure merge and the coercion change.
"""
import unittest

from backend.adapters.base import GeneratedScene
from backend.adapters.llm import _coerce_script
from backend.pipeline import _merge_consecutive_animations
from backend.prompts import build_script_schema


def _scene(scene_type, narration="", continuity=None, characters=None):
    return GeneratedScene(
        narration_text=narration,
        image_prompt=f"img-{narration}",
        scene_type=scene_type,
        characters=characters or [],
        continuity_context=continuity or [],
        animation=None,
    )


class MergeConsecutiveAnimationsTests(unittest.TestCase):
    def test_runs_are_combined_and_others_pass_through(self):
        scenes = [
            _scene("still", "S1"),
            _scene("animation", "A."),
            _scene("animation", "B."),
            _scene("still", "S4"),
            _scene("animation", "C."),
            _scene("video", "V6"),
        ]
        merged = _merge_consecutive_animations(scenes)
        self.assertEqual([s.scene_type for s in merged],
                         ["still", "animation", "still", "animation", "video"])
        # The 2-scene run is joined into one continuous narration.
        self.assertEqual(merged[1].narration_text, "A. B.")
        # A lone animation is untouched.
        self.assertEqual(merged[3].narration_text, "C.")
        # No code is authored at this stage.
        self.assertTrue(all(s.animation is None for s in merged))

    def test_continuity_source_scene_is_remapped_after_merge(self):
        # Scene 4 (still) references scene 6 (video); after merging the 2,3 run,
        # scene 6 becomes scene 5, and 4 becomes 3.
        scenes = [
            _scene("still", "S1"),
            _scene("animation", "A."),
            _scene("animation", "B."),
            _scene("still", "S4", continuity=[
                {"source_scene": 6, "visual_anchor": "totem", "reason": "recall"}]),
            _scene("animation", "C."),
            _scene("video", "V6"),
        ]
        merged = _merge_consecutive_animations(scenes)
        ref = merged[2].continuity_context[0]
        self.assertEqual(ref["source_scene"], 5)
        self.assertEqual(ref["visual_anchor"], "totem")

    def test_merged_characters_deduplicated(self):
        scenes = [
            _scene("animation", "A.", characters=[{"name": "Euler"}]),
            _scene("animation", "B.", characters=[{"name": "Euler"}, {"name": "Gauss"}]),
        ]
        merged = _merge_consecutive_animations(scenes)
        self.assertEqual(len(merged), 1)
        self.assertEqual([c["name"] for c in merged[0].characters], ["Euler", "Gauss"])


class CoerceScriptTests(unittest.TestCase):
    def test_animation_scene_kept_without_code(self):
        # Even if the model returned an animation object, it is ignored — code is
        # authored later — and the scene stays an animation (no downgrade to still).
        data = {"scenes": [{
            "narration_text": "Draw the line.",
            "image_prompt": "a line",
            "scene_type": "animation",
            "animation": {"code": "circle = Circle()", "title": "x"},
            "continuity_context": [],
            "characters": [],
        }], "metadata": {}}
        script = _coerce_script(data)
        self.assertEqual(script.scenes[0].scene_type, "animation")
        self.assertIsNone(script.scenes[0].animation)


class SchemaTests(unittest.TestCase):
    def test_schema_adds_animation_enum_but_no_animation_object(self):
        schema = build_script_schema(enable_animations=True)
        scene = schema["properties"]["scenes"]["items"]
        self.assertEqual(scene["properties"]["scene_type"]["enum"],
                         ["still", "video", "animation"])
        self.assertNotIn("animation", scene["properties"])

    def test_schema_unchanged_when_disabled(self):
        from backend.prompts import SCRIPT_JSON_SCHEMA
        self.assertIs(build_script_schema(enable_animations=False), SCRIPT_JSON_SCHEMA)


if __name__ == "__main__":
    unittest.main()
