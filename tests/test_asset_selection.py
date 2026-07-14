import unittest

from sqlmodel import Session, SQLModel, create_engine

from backend.models import Project, Scene, SceneType
from backend.routers.projects import select_scene_asset
from backend.schemas import SceneAssetSelect


class SceneAssetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_selecting_new_image_for_video_scene_clears_stale_clip(self):
        with Session(self.engine) as session:
            project = Project(title="Render test", topic_prompt="Test")
            session.add(project)
            session.commit()
            session.refresh(project)

            scene = Scene(
                project_id=project.id,
                scene_type=SceneType.VIDEO,
                image_path="data/projects/test/images/old.png",
                clip_path="data/projects/test/clips/old.mp4",
                image_variants=[
                    "data/projects/test/images/old.png",
                    "data/projects/test/images/new.png",
                ],
                clip_variants=[],
            )
            session.add(scene)
            session.commit()
            session.refresh(scene)

            select_scene_asset(
                project.id,
                scene.id,
                SceneAssetSelect(kind="image", path="data/projects/test/images/new.png"),
                session,
            )
            session.refresh(scene)

            self.assertEqual("data/projects/test/images/new.png", scene.image_path)
            self.assertIsNone(scene.clip_path)
            self.assertIn("data/projects/test/clips/old.mp4", scene.clip_variants)


if __name__ == "__main__":
    unittest.main()
