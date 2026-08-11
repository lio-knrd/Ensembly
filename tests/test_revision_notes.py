"""Creator feedback reaching both generation stages of a rewrite.

``Project.revision_notes`` is what the creator typed when regenerating a script
("the numbers are never actually calculated"). It has to survive two very
different prompts: the script LLM call, and the Manim authoring call that runs
much later, after audio. These tests pin that it reaches both, that it outranks
the preset guidance by being placed last, and that a project without notes
produces exactly the prompts this code produced before the field existed.
"""
import unittest
from unittest import mock

from backend import prompts
from backend.adapters import llm

NOTES = "Compute every number on screen; never state a result the script did not derive."


def _script_prompt(revision_notes: str = "", **kwargs) -> str:
    return prompts.build_script_prompt(
        "vertical 9:16",
        "explainer tone",
        "200 euro over 30 years",
        75,
        revision_notes=revision_notes,
        **kwargs,
    )


class ScriptPromptTests(unittest.TestCase):
    def test_notes_reach_the_script_prompt(self):
        prompt = _script_prompt(NOTES)
        self.assertIn(NOTES, prompt)
        self.assertIn("Correction notes from the creator", prompt)

    def test_notes_come_last_so_they_outrank_the_presets(self):
        prompt = _script_prompt(NOTES)
        self.assertLess(
            prompt.index("Content / subject matter"),
            prompt.index("Correction notes from the creator"),
        )
        self.assertTrue(prompt.rstrip().endswith(NOTES))

    def test_no_notes_leaves_the_prompt_unchanged(self):
        self.assertNotIn("Correction notes", _script_prompt(""))
        self.assertEqual(_script_prompt(""), _script_prompt("   "))
        self.assertEqual(_script_prompt(""), _script_prompt())

    def test_animation_guidance_demands_worked_out_numbers(self):
        prompt = _script_prompt(enable_animations=True)
        self.assertIn("Numbers must be worked out", prompt)
        # Off by default: the narrative/photographic presets never see it.
        self.assertNotIn("Numbers must be worked out", _script_prompt())


class _FakeStream:
    """Stands in for the SDK's streaming context manager."""

    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeAnthropic:
    """Captures the instruction instead of calling the API."""

    captured: list[str] = []

    def __init__(self, api_key=None):
        self.messages = self

    def stream(self, model=None, max_tokens=None, messages=None, output_config=None):
        _FakeAnthropic.captured.append(messages[0]["content"])
        block = mock.Mock()
        block.type = "text"
        block.text = "self.wait(1)"
        return _FakeStream(mock.Mock(content=[block], stop_reason="end_turn"))


def _author(*args) -> str:
    _FakeAnthropic.captured = []
    fake_module = mock.Mock(Anthropic=_FakeAnthropic)
    with mock.patch.dict("sys.modules", {"anthropic": fake_module}), \
            mock.patch.object(llm.settings, "anthropic_api_key", "test-key"), \
            mock.patch.object(llm.settings, "default_llm_provider", "anthropic"):
        llm.author_manim_code("The balance climbs to sixteen", 6.0, False, *args)
    return _FakeAnthropic.captured[0]


class AnimationAuthoringTests(unittest.TestCase):
    def test_notes_reach_the_manim_author(self):
        prompt = _author("", NOTES)
        self.assertIn(NOTES, prompt)
        self.assertIn("Correction notes from the creator", prompt)

    def test_notes_follow_the_house_style_and_precede_the_narration(self):
        prompt = _author("Amber on ink.", NOTES)
        self.assertLess(
            prompt.index("House style for this channel"),
            prompt.index("Correction notes from the creator"),
        )
        self.assertLess(
            prompt.index("Correction notes from the creator"), prompt.index("Scene narration")
        )

    def test_no_notes_leaves_the_authoring_prompt_unchanged(self):
        self.assertNotIn("Correction notes", _author(""))
        self.assertEqual(_author("", ""), _author("", "   "))
        self.assertEqual(_author(""), _author("", ""))

    def test_catalog_requires_the_arithmetic_on_screen(self):
        prompt = _author("")
        self.assertIn("Show the arithmetic, not just the answer", prompt)


if __name__ == "__main__":
    unittest.main()
