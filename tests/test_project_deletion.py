import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from backend.models import EditorialItem, EditorialPlan, Project
from backend.routers.projects import delete_project


class ProjectDeletionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys = ON")

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

            with (
                patch("backend.routers.projects.pipeline.stop_project_jobs") as stop,
                patch("backend.routers.projects.pipeline.cancel_project") as cancel,
            ):
                delete_project(project_id, session)

            stop.assert_called_once_with(project_id)
            cancel.assert_not_called()

            self.assertIsNone(session.get(Project, project_id))
            restored = session.get(EditorialItem, item_id)
            self.assertIsNotNone(restored)
            self.assertEqual("planned", restored.status)
            self.assertIsNone(restored.project_id)


if __name__ == "__main__":
    unittest.main()
