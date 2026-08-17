"""The content limits carried on every prompt sent to an image or video model.

Hosted models moderate their own input and a rejection fails the pipeline step
outright, so the limits are applied at the adapter edge — the one place every
prompt passes through, whichever call site built it.
"""
import unittest
from pathlib import Path
from unittest import mock

from backend import prompts
from backend.adapters.image import KreaDirectImageGenerator, KreaImageGenerator
from backend.adapters.video import FalVideoGenerator


class ImagePromptLimitTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("backend.adapters.image.settings.safe_image_prompts", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _krea_payload(self, prompt):
        captured = {}

        def submit(endpoint, payload):
            captured.update(payload)
            raise RuntimeError("stop after the payload is built")

        with mock.patch("backend.adapters.image._queue_submit", submit):
            with self.assertRaises(RuntimeError):
                KreaImageGenerator().generate(prompt, Path("out.png"))
        return captured

    def test_limits_ride_along_on_the_prompt(self):
        payload = self._krea_payload("Aphrodite rising from the sea foam.")

        self.assertTrue(payload["prompt"].startswith("Aphrodite rising from the sea foam."))
        self.assertIn("opaque fabric", payload["prompt"])
        self.assertIn("figures whole and unharmed", payload["prompt"])

    def test_the_clause_names_no_body_parts(self):
        # Measured: the clause is fine alone and a youthful description is fine
        # alone, but an age cue followed by a second anatomy list is refused.
        # Asking for full covering without inventorying what is covered passed
        # against both youthful and adult descriptions.
        for part in ("chest", "hips", "thighs", "torso", "legs", "skin"):
            self.assertNotIn(part, prompts.IMAGE_CONTENT_LIMITS.lower(), part)
            self.assertNotIn(part, prompts.VIDEO_CONTENT_LIMITS.lower(), part)

    def test_the_clause_carries_no_prohibition_vocabulary(self):
        # Measured against Krea: the provider screens the prompt text before any
        # image exists and matches these words literally, so a clause listing
        # what to avoid is itself refused with content_policy. Everything the
        # model must not draw is expressed as what it should draw instead.
        for trigger in (
            "nudity", "nude", "naked", "genital", "buttock", "breast", "sexual",
            "seductive", "undress", "underwear", "gore", "blood", "wound",
            "dismember", "severed", "torture",
        ):
            self.assertNotIn(trigger, prompts.IMAGE_CONTENT_LIMITS.lower(), trigger)
            self.assertNotIn(trigger, prompts.VIDEO_CONTENT_LIMITS.lower(), trigger)

    def test_the_clause_does_not_over_cover_or_age_figures_up(self):
        # Coverage, not costume: an infant or a lightly-dressed figure from the
        # source has to stay itself rather than be buried in full robes.
        self.assertNotIn("adult", prompts.IMAGE_CONTENT_LIMITS)
        self.assertIn("swaddling", prompts.IMAGE_CONTENT_LIMITS)
        self.assertIn("keep them recognisable as themselves", prompts.IMAGE_CONTENT_LIMITS)

    def test_limits_are_not_repeated_on_a_prompt_that_already_carries_them(self):
        already = prompts.with_image_content_limits("Athena in full armor.")

        payload = self._krea_payload(already)

        self.assertEqual(1, payload["prompt"].count("Content limits (mandatory"))

    def test_the_flag_turns_them_off(self):
        with mock.patch("backend.adapters.image.settings.safe_image_prompts", False):
            payload = self._krea_payload("Athena in full armor.")

        self.assertEqual("Athena in full armor.", payload["prompt"])

    def test_the_direct_krea_adapter_carries_them_too(self):
        with mock.patch("backend.adapters.image.KreaDirectImageGenerator._asset_url"):
            with mock.patch("backend.adapters.image.httpx.post") as post:
                post.return_value = mock.Mock(status_code=500, text="stop")
                with self.assertRaises(RuntimeError):
                    KreaDirectImageGenerator().generate("Zeus on a cliff.", Path("out.png"))

        self.assertIn("opaque fabric", post.call_args.kwargs["json"]["prompt"])


class VideoPromptLimitTests(unittest.TestCase):
    def _payload(self, prompt):
        with mock.patch("backend.adapters.image._to_data_uri", lambda path: "data:,"):
            return FalVideoGenerator()._payload(Path("frame.png"), prompt, 5)

    def test_clip_prompts_carry_the_shorter_limits(self):
        with mock.patch("backend.adapters.video.settings.safe_image_prompts", True):
            payload = self._payload("She turns toward the shore. Slow push in.")

        self.assertTrue(payload["prompt"].startswith("She turns toward the shore."))
        self.assertIn("full, opaque clothing they already wear", payload["prompt"])

    def test_the_flag_turns_them_off(self):
        with mock.patch("backend.adapters.video.settings.safe_image_prompts", False):
            payload = self._payload("She turns toward the shore.")

        self.assertEqual("She turns toward the shore.", payload["prompt"])


class ScriptGuidanceTests(unittest.TestCase):
    def test_the_script_model_is_told_to_keep_the_visuals_clothed(self):
        self.assertIn("fully covering, in opaque fabric", prompts.CORE_SYSTEM_PROMPT)
        # Descriptions echo this guidance, and an anatomy list in a description
        # is what pairs badly with an age cue downstream.
        self.assertIn("not about the parts of the body they cover", prompts.CORE_SYSTEM_PROMPT)
        # The limit is on the pictures, not on what the story may say.
        self.assertIn("the narration may tell the story fully", prompts.CORE_SYSTEM_PROMPT)

    def test_the_script_model_keeps_the_source_rather_than_over_covering(self):
        # What got Himeros refused was the single translucent drape, not his
        # age. The script model reads negation fine, so the specific wording is
        # named here — but it must not sanitise figures out of recognition.
        self.assertIn("young, old, an infant", prompts.CORE_SYSTEM_PROMPT)
        self.assertIn("should not age anyone up", prompts.CORE_SYSTEM_PROMPT)
        for banned in ("sheer", "translucent", "gauzy", "see-through", "clinging"):
            self.assertIn(banned, prompts.CORE_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
