"""The content preset's animation house style reaching the Manim authoring call.

`animation_style_prompt` is the animation-scene counterpart to
`image_style_prompt`: it never touches the script LLM, only the call that authors
Manim code. These tests pin the two properties that matter — the text is actually
delivered to the model, and a group that has not set one produces exactly the
prompt this code produced before the field existed.
"""
import unittest
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from backend import pipeline
from backend.adapters import llm
from backend.models import ContentPreset, Project


class _FakeAnthropic:
    """Captures the instruction instead of calling the API."""

    captured: list[str] = []

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, model=None, max_tokens=None, messages=None):
        _FakeAnthropic.captured.append(messages[0]["content"])
        block = mock.Mock()
        block.type = "text"
        block.text = "self.wait(1)"
        return mock.Mock(content=[block])


def _author(style_prompt: str = "") -> str:
    _FakeAnthropic.captured = []
    fake_module = mock.Mock(Anthropic=_FakeAnthropic)
    with mock.patch.dict("sys.modules", {"anthropic": fake_module}), \
            mock.patch.object(llm.settings, "anthropic_api_key", "test-key"), \
            mock.patch.object(llm.settings, "default_llm_provider", "anthropic"):
        llm.author_manim_code("As x grows the curve climbs", 6.0, False, style_prompt)
    return _FakeAnthropic.captured[0]


class AuthorPromptTests(unittest.TestCase):
    def test_style_prompt_reaches_the_model(self):
        prompt = _author("Amber #F5A623 for money, never blue.")
        self.assertIn("Amber #F5A623 for money, never blue.", prompt)
        self.assertIn("House style for this channel", prompt)

    def test_style_is_placed_after_the_catalog_rules(self):
        # The catalog carries the hard constraints (canvas, sync, safety); the
        # house style must follow it so it can only tighten, never override.
        prompt = _author("Only white and amber.")
        self.assertLess(
            prompt.index("World coordinates"), prompt.index("House style for this channel")
        )

    def test_style_precedes_the_narration(self):
        prompt = _author("Only white and amber.")
        self.assertLess(
            prompt.index("House style for this channel"), prompt.index("Scene narration")
        )

    def test_empty_style_leaves_the_prompt_unchanged(self):
        self.assertNotIn("House style", _author(""))
        self.assertNotIn("House style", _author("   "))
        self.assertEqual(_author(""), _author("   "))

    def test_omitted_style_matches_empty_string(self):
        _FakeAnthropic.captured = []
        fake_module = mock.Mock(Anthropic=_FakeAnthropic)
        with mock.patch.dict("sys.modules", {"anthropic": fake_module}), \
                mock.patch.object(llm.settings, "anthropic_api_key", "test-key"), \
                mock.patch.object(llm.settings, "default_llm_provider", "anthropic"):
            llm.author_manim_code("As x grows the curve climbs", 6.0, False)
        self.assertEqual(_FakeAnthropic.captured[0], _author(""))


class ContentAnimationStyleLookupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_style_is_read_from_the_projects_group(self):
        with Session(self.engine) as session:
            preset = ContentPreset(name="DE", animation_style_prompt="Amber on ink.")
            session.add(preset)
            session.commit()
            session.refresh(preset)
            project = Project(title="p", topic_prompt="t", content_preset_id=preset.id)
            session.add(project)
            session.commit()
            self.assertEqual(
                pipeline._content_animation_style(session, project), "Amber on ink."
            )

    def test_project_without_a_group_gets_empty(self):
        with Session(self.engine) as session:
            project = Project(title="p", topic_prompt="t")
            session.add(project)
            session.commit()
            self.assertEqual(pipeline._content_animation_style(session, project), "")

    def test_group_without_a_style_gets_empty(self):
        with Session(self.engine) as session:
            preset = ContentPreset(name="Myth")
            session.add(preset)
            session.commit()
            session.refresh(preset)
            project = Project(title="p", topic_prompt="t", content_preset_id=preset.id)
            session.add(project)
            session.commit()
            self.assertEqual(pipeline._content_animation_style(session, project), "")


if __name__ == "__main__":
    unittest.main()
