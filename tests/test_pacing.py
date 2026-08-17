"""Script density and narration controls that keep short-form stories readable."""
import unittest
from pathlib import Path
from unittest import mock

from backend import prompts
from backend.adapters.base import GeneratedScene, GeneratedScript
from backend.adapters.tts import ElevenLabsTTSGenerator, OfflineTTSGenerator
from backend.services.pacing import panel_pacing_feedback


def _script(scene_count: int, words_per_scene: int) -> GeneratedScript:
    narration = " ".join(["word"] * words_per_scene)
    return GeneratedScript(
        scenes=[
            GeneratedScene(narration_text=narration, image_prompt="image", scene_type="still")
            for _ in range(scene_count)
        ],
        metadata={},
    )


class PromptPacingTests(unittest.TestCase):
    def test_duration_is_elastic_up_to_the_reviewed_ceiling(self):
        prompt = prompts.build_script_prompt("TikTok", "myth", "Io", 75)
        self.assertIn("NOT a deadline", prompt)
        # The same ceiling the automatic review measures against, so the draft
        # is never invited to a length that is then sent back.
        self.assertIn("up to about 150 seconds (2:30)", prompt)
        self.assertIn("Energy should come from stakes", prompt)

    def test_panel_hold_is_capped_however_long_the_group_asks_for(self):
        """A group set to a leisurely panel_seconds still gets the feed's band.

        The posted render that averaged 9.5 seconds a panel was authored under
        an instruction that moved up with the group setting.
        """
        for panel_seconds in (4, 9):
            prompt = prompts.build_script_prompt(
                "TikTok", "myth", "Io", 75,
                panels_mode=True, panel_seconds=panel_seconds,
            )
            self.assertIn("1.2-to-3.0-second", prompt)
            self.assertIn("Never turn every clause into a new panel", prompt)

    def test_automatic_feedback_is_included_on_rewrite(self):
        prompt = prompts.build_script_prompt(
            "TikTok", "myth", "Io", 75, pacing_feedback="Too many panels."
        )
        self.assertIn("Automatic pacing review", prompt)
        self.assertIn("Too many panels.", prompt)


class DensityReviewTests(unittest.TestCase):
    def test_rejects_twenty_tiny_panels(self):
        # 20 x 2 words ~ 17s of narration, under a second a panel.
        feedback = panel_pacing_feedback(
            _script(20, 2), panels_mode=True, panel_seconds=4
        )
        self.assertIn("20 panels", feedback)
        self.assertIn("below this group's 1.2-second", feedback)

    def test_accepts_readable_panel_holds(self):
        # ~2.5s a panel: inside the capped band, and the hold the old 4.0-6.5
        # band would have sent back as too fast.
        self.assertEqual(
            panel_pacing_feedback(_script(20, 6), panels_mode=True, panel_seconds=4),
            "",
        )

    def test_rejects_too_few_panels_for_a_long_script(self):
        feedback = panel_pacing_feedback(
            _script(10, 25), panels_mode=True, panel_seconds=4
        )
        self.assertIn("only 10 panels", feedback)
        self.assertIn("1.2-3.0-second range", feedback)

    def test_rejects_the_hold_that_shipped(self):
        """The 11-panel, 104-second cut that passed the old review."""
        feedback = panel_pacing_feedback(
            _script(11, 23), panels_mode=True, panel_seconds=7
        )
        self.assertIn("only 11 panels", feedback)
        self.assertIn("or more panels", feedback)

    def test_mixed_mode_is_not_constrained_by_panel_density(self):
        self.assertEqual(
            panel_pacing_feedback(_script(20, 5), panels_mode=False, panel_seconds=4),
            "",
        )


class RuntimeReviewTests(unittest.TestCase):
    """The check that would have caught the four-minute-forty mythology cut."""

    def test_rejects_a_script_that_runs_far_past_its_reference(self):
        # 27 panels x 26 words ~ 700 words ~ 4:50 of narration for a 75s brief.
        feedback = panel_pacing_feedback(
            _script(27, 26), panels_mode=True, panel_seconds=4, target_seconds=75
        )
        self.assertIn("702 spoken words", feedback)
        self.assertIn("4:50", feedback)
        self.assertIn("past the 2:30 ceiling", feedback)
        self.assertIn("Keep every story beat", feedback)

    def test_accepts_a_script_inside_the_ceiling(self):
        # 60 x 6 words ~ 2:29 of narration at ~2.5s a panel: inside both checks.
        self.assertEqual(
            panel_pacing_feedback(
                _script(60, 6), panels_mode=True, panel_seconds=4, target_seconds=75
            ),
            "",
        )

    def test_runtime_applies_outside_panels_mode(self):
        feedback = panel_pacing_feedback(
            _script(10, 60), panels_mode=False, panel_seconds=4, target_seconds=60
        )
        self.assertIn("ceiling", feedback)
        self.assertNotIn("panels", feedback)

    def test_panel_advice_is_measured_against_the_trimmed_length(self):
        """A draft told to lose half its words is not also told to add panels.

        The old check measured holds against the length being rejected, so an
        over-long script produced two contradictory instructions in one rewrite.

        62 panels is far too few for this draft's own 5:08, and right for the
        2:30 it is being told to become.
        """
        feedback = panel_pacing_feedback(
            _script(62, 12), panels_mode=True, panel_seconds=4, target_seconds=75
        )
        self.assertIn("ceiling", feedback)
        self.assertNotIn("or more panels", feedback)

    def test_no_target_means_no_runtime_ceiling(self):
        self.assertEqual(
            panel_pacing_feedback(_script(27, 26), panels_mode=False, panel_seconds=4),
            "",
        )


class NarrationControlTests(unittest.TestCase):
    @mock.patch("backend.adapters.tts.httpx.post")
    def test_elevenlabs_receives_speed_and_neighbor_context(self, post):
        post.return_value.json.return_value = {
            "audio_base64": "",
            "alignment": {},
        }
        post.return_value.raise_for_status.return_value = None
        generator = ElevenLabsTTSGenerator("voice", speed=0.9)
        generator._request("Middle.", previous_text="Before.", next_text="After.")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["voice_settings"], {"speed": 0.9})
        self.assertEqual(payload["previous_text"], "Before.")
        self.assertEqual(payload["next_text"], "After.")

    def test_offline_speed_changes_estimated_duration(self):
        generator = OfflineTTSGenerator(speed=0.8)
        with mock.patch.object(generator, "_silent_mp3"):
            with mock.patch("backend.adapters.tts._write_timestamps"):
                result = generator.synthesize(
                    "one two three four five", Path("a.mp3"), Path("a.json")
                )
        self.assertEqual(result.duration_seconds, 2.5)


if __name__ == "__main__":
    unittest.main()
