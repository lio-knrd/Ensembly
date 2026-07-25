"""Integration test for the animation path through the pipeline.

Exercises the real ``_render_scene_animation`` flow: a DB scene's spec is
normalized, its cues are resolved against a timestamps file, Manim renders an
MP4, and the scene's ``animation_path`` + storyboard readiness are updated.
Skipped when Manim is not installed.
"""
import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from backend import pipeline
from backend.config import settings
from backend.models import Project, Scene, SceneType


@unittest.skipUnless(importlib.util.find_spec("manim") is not None, "manim not installed")
class AnimationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self._patch = mock.patch.object(pipeline, "engine", self.engine)
        self._patch.start()
        # Live under the real data dir so _rel() can make project-relative paths.
        self.folder = settings.projects_dir / "_anim_pipeline_test"
        (self.folder / "audio").mkdir(parents=True, exist_ok=True)
        words = [("As", 0.0), ("x", 0.3), ("grows", 0.6), ("the", 1.0),
                 ("curve", 1.3), ("reaches", 1.9), ("sixteen", 2.3)]
        ts = {"words": [{"word": w, "start": s, "end": s + 0.2} for w, s in words], "duration": 2.6}
        (self.folder / "audio" / "scene_01.timestamps.json").write_text(json.dumps(ts), encoding="utf-8")

    def tearDown(self):
        self._patch.stop()
        self.engine.dispose()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_render_animation_scene_end_to_end(self):
        root = Path(settings.projects_dir.parent.parent)
        ts_rel = (self.folder / "audio" / "scene_01.timestamps.json").relative_to(root).as_posix()
        with Session(self.engine) as session:
            project = Project(title="Anim test", topic_prompt="x squared")
            session.add(project)
            session.commit()
            session.refresh(project)
            scene = Scene(
                project_id=project.id,
                order_index=0,
                scene_type=SceneType.ANIMATION,
                narration_text="As x grows the curve reaches sixteen",
                timestamps_path=ts_rel,
                duration_seconds=2.6,
                # LaTeX-free Manim code so the test does not require a TeX install.
                animation_spec={
                    "title": "y = x squared",
                    "code": (
                        "curve = FunctionGraph(lambda x: 0.4 * x * x, x_range=[-3, 3], color=BLUE)\n"
                        "self.play_at(self.cue('the curve'), Create(curve), run_time=1.0)\n"
                        "answer = Text('16', color=YELLOW, font_size=64).to_edge(DOWN, buff=1.0)\n"
                        "self.play_at(self.cue('sixteen'), FadeIn(answer))\n"
                    ),
                },
            )
            session.add(scene)
            session.commit()
            session.refresh(scene)

            ok = pipeline._render_scene_animation(session, project, scene, self.folder)
            session.refresh(scene)

            self.assertTrue(ok)
            self.assertEqual(scene.status, "ready")
            self.assertTrue(scene.animation_path)
            out = root / scene.animation_path
            self.assertTrue(out.exists() and out.stat().st_size > 0)
            self.assertIn(scene.animation_path, scene.animation_variants)
            # An animation scene is storyboard-ready via its animation, not an image.
            self.assertTrue(pipeline._storyboard_ready([scene]))


if __name__ == "__main__":
    unittest.main()
