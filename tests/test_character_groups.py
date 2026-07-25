import unittest

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from backend.database import _backfill_character_groups
from backend.models import Character, ContentPreset, Project, Scene
from backend.pipeline import _group_characters


class GroupScopedLibraryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _two_groups(self, session):
        myth = ContentPreset(name="Greek Mythology", is_default=True)
        cats = ContentPreset(name="Cat Cartoon")
        session.add(myth)
        session.add(cats)
        session.commit()
        session.refresh(myth)
        session.refresh(cats)
        return myth, cats

    def test_project_sees_only_its_own_group(self):
        with Session(self.engine) as session:
            myth, cats = self._two_groups(session)
            session.add(Character(name="Zeus", content_preset_id=myth.id))
            session.add(Character(name="Garry", content_preset_id=cats.id))
            session.add(Character(name="Nobody"))
            project = Project(
                title="Olympus", topic_prompt="The Olympian order", content_preset_id=myth.id
            )
            session.add(project)
            session.commit()
            session.refresh(project)

            self.assertEqual(
                ["Zeus"], [c.name for c in _group_characters(session, project)]
            )

    def test_same_name_in_two_groups_stays_separate(self):
        with Session(self.engine) as session:
            myth, cats = self._two_groups(session)
            session.add(Character(name="Zeus", content_preset_id=myth.id))
            cat_zeus = Character(name="Zeus", content_preset_id=cats.id)
            session.add(cat_zeus)
            project = Project(
                title="Cat tale", topic_prompt="A cat named Zeus", content_preset_id=cats.id
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            session.refresh(cat_zeus)

            visible = _group_characters(session, project)
            self.assertEqual([cat_zeus.id], [c.id for c in visible])

    def test_groupless_project_sees_only_ungrouped_characters(self):
        with Session(self.engine) as session:
            myth, _ = self._two_groups(session)
            session.add(Character(name="Zeus", content_preset_id=myth.id))
            session.add(Character(name="Narrator"))
            project = Project(title="Loose", topic_prompt="No group")
            session.add(project)
            session.commit()
            session.refresh(project)

            self.assertEqual(
                ["Narrator"], [c.name for c in _group_characters(session, project)]
            )


class BackfillTests(unittest.TestCase):
    """The migration that turns the previously global library into group-owned."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_backfill_infers_group_from_project_usage(self):
        with Session(self.engine) as session:
            myth = ContentPreset(name="Greek Mythology", is_default=True)
            cats = ContentPreset(name="Cat Cartoon")
            zeus = Character(name="Zeus")
            garry = Character(name="Garry")
            orphan = Character(name="Unused")
            session.add_all([myth, cats, zeus, garry, orphan])
            session.commit()
            myth_project = Project(
                title="Olympus", topic_prompt="Olympus", content_preset_id=myth.id
            )
            cat_project = Project(
                title="Water", topic_prompt="Water", content_preset_id=cats.id
            )
            session.add_all([myth_project, cat_project])
            session.commit()
            session.add(Scene(project_id=myth_project.id, character_ids=[zeus.id]))
            session.add(Scene(project_id=cat_project.id, character_ids=[garry.id]))
            session.commit()
            # Simulate the pre-migration state.
            session.exec(text("UPDATE characters SET content_preset_id = NULL"))
            session.commit()
            ids = (zeus.id, garry.id, orphan.id, myth.id, cats.id)

        with self.engine.begin() as conn:
            _backfill_character_groups(conn)

        zeus_id, garry_id, orphan_id, myth_id, cats_id = ids
        with Session(self.engine) as session:
            self.assertEqual(myth_id, session.get(Character, zeus_id).content_preset_id)
            self.assertEqual(cats_id, session.get(Character, garry_id).content_preset_id)
            # Never-used characters land in the default group rather than nowhere.
            self.assertEqual(myth_id, session.get(Character, orphan_id).content_preset_id)

    def test_backfill_prefers_the_most_used_group(self):
        with Session(self.engine) as session:
            myth = ContentPreset(name="Greek Mythology", is_default=True)
            cats = ContentPreset(name="Cat Cartoon")
            shared = Character(name="Narrator")
            session.add_all([myth, cats, shared])
            session.commit()
            cat_a = Project(title="A", topic_prompt="A", content_preset_id=cats.id)
            cat_b = Project(title="B", topic_prompt="B", content_preset_id=cats.id)
            myth_a = Project(title="C", topic_prompt="C", content_preset_id=myth.id)
            session.add_all([cat_a, cat_b, myth_a])
            session.commit()
            for project in (cat_a, cat_b, myth_a):
                session.add(Scene(project_id=project.id, character_ids=[shared.id]))
            session.commit()
            session.exec(text("UPDATE characters SET content_preset_id = NULL"))
            session.commit()
            shared_id, cats_id = shared.id, cats.id

        with self.engine.begin() as conn:
            _backfill_character_groups(conn)

        with Session(self.engine) as session:
            self.assertEqual(cats_id, session.get(Character, shared_id).content_preset_id)


if __name__ == "__main__":
    unittest.main()
