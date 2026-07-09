import unittest

from sqlmodel import Session, SQLModel, create_engine

from backend.models import Character, CharacterForm, Project, Scene
from backend.pipeline import _reconcile_scene_assignments, compute_cast


class MixedCastTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _mixed_scene(self, session):
        project = Project(title="Olympus", topic_prompt="The Olympian order")
        zeus = Character(
            name="Zeus",
            reference_image_path="data/characters/zeus/reference.png",
        )
        typhon = Character(
            name="Typhon",
            reference_image_path="data/characters/typhon/reference.png",
        )
        session.add(project)
        session.add(zeus)
        session.add(typhon)
        session.commit()
        session.refresh(project)
        session.refresh(zeus)
        session.refresh(typhon)

        zeus_form = CharacterForm(
            character_id=zeus.id,
            name="Default",
            is_default=True,
            reference_image_path=zeus.reference_image_path,
        )
        typhon_form = CharacterForm(
            character_id=typhon.id,
            name="Default",
            is_default=True,
            reference_image_path=typhon.reference_image_path,
        )
        session.add(zeus_form)
        session.add(typhon_form)
        session.commit()
        session.refresh(zeus_form)
        session.refresh(typhon_form)

        scene = Scene(
            project_id=project.id,
            character_ids=[zeus.id, typhon.id],
            character_assignments=[{
                "character_id": zeus.id,
                "character_name": zeus.name,
                "form_id": zeus_form.id,
                "form_name": zeus_form.name,
                "state": "",
                "state_importance": "default",
                "state_notes": "",
                "missing_form": False,
            }],
        )
        session.add(scene)
        session.commit()
        session.refresh(scene)
        return project, scene, typhon, typhon_form

    def test_compute_cast_merges_unassigned_character_ids(self):
        with Session(self.engine) as session:
            project, _, _, _ = self._mixed_scene(session)

            cast = compute_cast(session, project)

            self.assertEqual(["Zeus", "Typhon"], [member["name"] for member in cast])
            self.assertTrue(all(member["has_sheet"] for member in cast))

    def test_reconcile_adds_generated_character_to_nonempty_assignments(self):
        with Session(self.engine) as session:
            project, scene, typhon, typhon_form = self._mixed_scene(session)

            _reconcile_scene_assignments(
                session, project.id, typhon, typhon_form, state=""
            )
            session.commit()
            session.refresh(scene)

            assigned_ids = {
                assignment["character_id"]
                for assignment in scene.character_assignments
            }
            self.assertEqual(set(scene.character_ids), assigned_ids)


if __name__ == "__main__":
    unittest.main()
