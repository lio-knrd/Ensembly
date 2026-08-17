"""Recovering from a character sheet the image model refuses to draw.

A moderated image model rejecting a prompt is a hard failure for the step, and
the only useful fix is editing that prompt. These cover the path that makes that
possible: the character, its form and the rejected prompt survive the failure,
the error names the member, and a retry from the failed state is accepted.
"""
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from backend import pipeline
from backend.models import (
    Character,
    CharacterForm,
    ContentPreset,
    Project,
    ProjectCharacter,
    Scene,
    Stage,
)
from backend.pipeline import _apply_style, compute_cast, generate_character_sheet


class _RejectingImageGenerator:
    """Stands in for a model whose moderation refuses the prompt."""

    def __init__(self):
        self.prompts = []

    def generate(self, prompt, out_path, reference_images=None, reference_strengths=None):
        self.prompts.append(prompt)
        raise RuntimeError(
            "Krea job failed: {'status': 'failed', 'error': {'code': 'content_policy'}}"
        )


class CastRejectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.images = _RejectingImageGenerator()
        patches = [
            mock.patch("backend.pipeline.engine", self.engine),
            mock.patch("backend.pipeline.get_image_generator", lambda: self.images),
            mock.patch("backend.storage.character_folder", lambda name: Path("/nonexistent")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.dispose()

    def _project(self):
        with Session(self.engine) as session:
            preset = ContentPreset(name="Mythology", image_style_prompt="Inked panel art")
            session.add(preset)
            session.commit()
            session.refresh(preset)
            project = Project(
                title="Born From a Skull and a Sea of Foam",
                topic_prompt="How the younger gods arrived",
                content_preset_id=preset.id,
                stage=Stage.CAST_REVIEW,
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            # The script named her but no library entry exists yet: the state the
            # pipeline is actually in when it generates the missing sheets.
            session.add(
                Scene(project_id=project.id, order_index=0, suggested_characters=["Aphrodite"])
            )
            session.commit()
            return project.id

    def _generate(self, project_id, prompt=None):
        generate_character_sheet(
            project_id,
            "Aphrodite",
            "Rising from the foam.",
            prompt,
            generate_description=False,
            state=None,
        )

    def test_rejected_sheet_names_the_character_in_the_error(self):
        project_id = self._project()

        self._generate(project_id)

        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            self.assertEqual(Stage.FAILED, project.stage)
            self.assertIn("character sheet for Aphrodite:", project.error)
            self.assertIn("content_policy", project.error)
            self.assertEqual(Stage.CAST_REVIEW.value, project.failed_stage)

    def test_rejected_prompt_is_kept_and_visible_in_the_cast(self):
        project_id = self._project()

        self._generate(project_id)

        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            cast = compute_cast(session, project)

            self.assertEqual(["Aphrodite"], [member["name"] for member in cast])
            member = cast[0]
            self.assertFalse(member["has_sheet"])
            # The character is linked, so the panel has a form to edit rather
            # than a bare suggested name.
            self.assertTrue(member["character_id"])
            self.assertTrue(member["form_id"])
            self.assertEqual(self.images.prompts[0], member["reference_prompt"])
            self.assertIn("Rising from the foam.", member["reference_prompt"])
            self.assertIn("Inked panel art", member["reference_prompt"])
            self.assertIsNotNone(
                session.get(ProjectCharacter, (project_id, member["character_id"]))
            )

    def test_edited_prompt_retries_from_the_failed_state_and_is_sent_verbatim(self):
        project_id = self._project()
        self._generate(project_id)
        with Session(self.engine) as session:
            rejected = compute_cast(session, session.get(Project, project_id))[0]
        edited = rejected["reference_prompt"].replace("Rising from the foam.", "Robed in linen.")

        self._generate(project_id, prompt=edited)

        self.assertEqual(2, len(self.images.prompts))
        # Sent as written: the stored prompt already carries the style directive,
        # so re-applying it would stack a second copy on every round trip.
        self.assertEqual(edited, self.images.prompts[1])
        self.assertEqual(1, self.images.prompts[1].count("Visual style directive"))

    def test_a_never_generated_character_shows_the_prompt_it_would_send(self):
        with Session(self.engine) as session:
            preset = ContentPreset(name="Mythology", image_style_prompt="Inked panel art")
            session.add(preset)
            session.commit()
            session.refresh(preset)
            project = Project(title="Olympus", topic_prompt="Zeus", content_preset_id=preset.id)
            char = Character(name="Athena", description="Grey-eyed, in full bronze armor.")
            session.add_all([project, char])
            session.commit()
            session.refresh(project)
            session.refresh(char)
            session.add(CharacterForm(character_id=char.id, name="Default", is_default=True))
            session.add(Scene(project_id=project.id, order_index=0, character_ids=[char.id]))
            session.commit()

            member = compute_cast(session, project)[0]

            self.assertFalse(member["has_sheet"])
            # Editable before the first attempt, not only after a rejection.
            self.assertIn("Athena", member["reference_prompt"])
            self.assertIn("full bronze armor", member["reference_prompt"])
            self.assertIn("Inked panel art", member["reference_prompt"])

    def test_a_failure_at_another_step_is_not_reopened_by_a_sheet_request(self):
        project_id = self._project()
        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            project.stage = Stage.FAILED
            project.failed_stage = Stage.RENDERING.value
            project.error = "final render: ffmpeg exploded"
            session.add(project)
            session.commit()

        self._generate(project_id)

        self.assertEqual([], self.images.prompts)
        with Session(self.engine) as session:
            self.assertEqual(
                "final render: ffmpeg exploded", session.get(Project, project_id).error
            )

    def test_batch_generation_stops_at_the_first_rejection(self):
        project_id = self._project()
        with Session(self.engine) as session:
            scene = session.exec(
                select(Scene).where(Scene.project_id == project_id)
            ).first()
            scene.suggested_characters = ["Aphrodite", "Athena"]
            session.add(scene)
            session.commit()

        pipeline.generate_missing_sheets(project_id)

        # Continuing to Athena would have cleared the FAILED state and buried the
        # prompt that has to be edited.
        self.assertEqual(1, len(self.images.prompts))
        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            self.assertEqual(Stage.FAILED, project.stage)
            self.assertIn("Aphrodite", project.error)


class StyleApplicationTests(unittest.TestCase):
    def test_style_is_not_stacked_when_a_saved_prompt_is_handed_back(self):
        once = _apply_style("A figure at the shore.", "Inked panel art")

        self.assertEqual(once, _apply_style(once, "Inked panel art"))
        self.assertEqual(1, once.count("Visual style directive"))


if __name__ == "__main__":
    unittest.main()
