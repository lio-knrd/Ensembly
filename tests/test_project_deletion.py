import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from backend.models import EditorialItem, EditorialPlan, Project
from backend.routers.projects import delete_project


class ProjectDeletionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_deleting_project_returns_linked_editorial_item_to_planned(self):
        with Session(self.engine) as session:
            plan = EditorialPlan(name="Interest explainer series")
            project = Project(title="200 Euro", topic_prompt="Compound interest")
            session.add_all([plan, project])
            session.commit()
            session.refresh(plan)
            session.refresh(project)

            item = EditorialItem(
                plan_id=plan.id,
                title=project.title,
                status="in_progress",
                source_type="ensembly",
                project_id=project.id,
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            item_id = item.id
            project_id = project.id

            with patch("backend.routers.projects.pipeline.cancel_project"):
                delete_project(project_id, session)

            self.assertIsNone(session.get(Project, project_id))
            restored = session.get(EditorialItem, item_id)
            self.assertIsNotNone(restored)
            self.assertEqual("planned", restored.status)
            self.assertIsNone(restored.project_id)


if __name__ == "__main__":
    unittest.main()
