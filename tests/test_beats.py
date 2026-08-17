"""Beats: consecutive panels that make up one continuous scene.

A beat's panels have to agree on place, light and palette, so each one after
the first is handed the picture it continues from — whether or not the script
thought to ask for it.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from backend import pipeline
from backend.models import Project, Scene


class BeatContinuityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.tmp = self.enterContext(TemporaryDirectory())
        self.root = Path(self.tmp)
        patcher = mock.patch.object(
            pipeline.settings, "projects_dir", self.root / "data" / "projects"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def _project(self, session, scenes):
        project = Project(title="T", topic_prompt="t", folder_path="p")
        session.add(project)
        session.commit()
        for index, (beat, has_image) in enumerate(scenes):
            image_path = None
            if has_image:
                image_path = f"img/scene_{index}.png"
                full = self.root / image_path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(b"x")
            session.add(Scene(
                project_id=project.id, order_index=index, narration_text="n",
                image_prompt="p", beat_id=beat, image_path=image_path,
            ))
        session.commit()
        return project

    def _refs(self, session, project, order_index):
        scene = next(
            s for s in session.exec(
                __import__("sqlmodel").select(Scene).where(Scene.project_id == project.id)
            ).all() if s.order_index == order_index
        )
        return pipeline._scene_continuity_candidates(session, project, scene)

    def test_a_panel_continuing_a_beat_gets_the_previous_panel(self):
        with Session(self.engine) as session:
            project = self._project(session, [("fight", True), ("fight", False)])
            refs = self._refs(session, project, 1)
            self.assertEqual([r["scene_number"] for r in refs], [1])
            self.assertIn("continued", refs[0]["visual_anchor"])

    def test_the_panel_that_opens_a_beat_gets_nothing(self):
        with Session(self.engine) as session:
            project = self._project(session, [("chase", True), ("fight", False)])
            self.assertEqual(self._refs(session, project, 1), [])

    def test_a_standalone_panel_gets_nothing(self):
        """Empty beat_id is the default, and must not chain panels together."""
        with Session(self.engine) as session:
            project = self._project(session, [("", True), ("", False)])
            self.assertEqual(self._refs(session, project, 1), [])

    def test_a_beat_only_carries_from_the_panel_just_before(self):
        """Not every earlier panel of the beat, or the references pile up."""
        with Session(self.engine) as session:
            project = self._project(
                session, [("fight", True), ("fight", True), ("fight", False)]
            )
            refs = self._refs(session, project, 2)
            self.assertEqual([r["scene_number"] for r in refs], [2])

    def test_a_resumed_beat_does_not_reach_across_the_gap(self):
        """Same slug either side of a different beat is not continuous."""
        with Session(self.engine) as session:
            project = self._project(
                session, [("fight", True), ("aftermath", True), ("fight", False)]
            )
            self.assertEqual(self._refs(session, project, 2), [])

    def test_the_beat_reference_is_not_duplicated_by_an_explicit_link(self):
        with Session(self.engine) as session:
            project = self._project(session, [("fight", True), ("fight", False)])
            scene = next(
                s for s in session.exec(
                    __import__("sqlmodel").select(Scene).where(Scene.project_id == project.id)
                ).all() if s.order_index == 1
            )
            scene.continuity_context = [
                {"source_scene": 1, "visual_anchor": "the lion", "reason": "same lion"}
            ]
            session.add(scene)
            session.commit()
            refs = pipeline._scene_continuity_candidates(session, project, scene)
            self.assertEqual(len(refs), 1)


if __name__ == "__main__":
    unittest.main()


class ShakeEnvelopeTests(unittest.TestCase):
    """The shake has to start and end on the panel's true framing.

    Both bugs this covers were visible as a jolt: a constant zoom headroom that
    switched off when the shake ended, and a cosine axis that was already at
    full displacement on the first frame.
    """

    def _state(self, t):
        from backend.services import parallax
        zoom, fx, fy = parallax._shake_at((1.0, 0.0, 0.0), t)
        return zoom, fx * (1080 - 1080 / zoom), fy * (1920 - 1920 / zoom)

    def test_it_opens_on_the_true_framing(self):
        zoom, dx, dy = self._state(0.0)
        self.assertAlmostEqual(dx, 0.0, places=6)
        self.assertAlmostEqual(dy, 0.0, places=6)

    def test_it_closes_without_a_pop(self):
        from backend.services import parallax
        before = self._state(parallax._SHAKE_SECONDS - 1e-4)
        after = self._state(parallax._SHAKE_SECONDS)
        for a, b in zip(before, after):
            self.assertAlmostEqual(a, b, places=3)

    def test_the_zoom_returns_to_where_it_started(self):
        from backend.services import parallax
        self.assertAlmostEqual(self._state(parallax._SHAKE_SECONDS)[0], 1.0, places=6)

    def test_it_actually_displaces_in_between(self):
        peak = max(abs(self._state(t)[1]) + abs(self._state(t)[2])
                   for t in (0.02, 0.04, 0.06, 0.08, 0.10))
        self.assertGreater(peak, 5.0)


class GradeTests(unittest.TestCase):
    """Colour grades: a mood laid over the panel, not a repaint of it."""

    def setUp(self):
        from PIL import Image
        # A neutral mid-grey reads every channel shift without clipping.
        self.panel = Image.new("RGB", (8, 8), (120, 120, 120))

    def _rgb(self, grade):
        from backend.services.parallax import _graded
        return _graded(self.panel, grade).getpixel((0, 0))

    def test_none_is_untouched(self):
        self.assertEqual(self._rgb("none"), (120, 120, 120))

    def test_an_unknown_grade_is_untouched(self):
        """A value the renderer does not know must not silently tint a video."""
        self.assertEqual(self._rgb("sepia-deluxe"), (120, 120, 120))

    def test_warm_and_cold_push_opposite_ways(self):
        warm, cold = self._rgb("warm"), self._rgb("cold")
        self.assertGreater(warm[0], warm[2])
        self.assertLess(cold[0], cold[2])

    def test_blood_leads_on_red(self):
        red, green, blue = self._rgb("blood")
        self.assertGreater(red, green)
        self.assertGreater(red, blue)

    def test_memory_drains_the_colour(self):
        from PIL import Image
        from backend.services.parallax import _graded
        vivid = Image.new("RGB", (8, 8), (200, 40, 40))
        before = max(vivid.getpixel((0, 0))) - min(vivid.getpixel((0, 0)))
        after_px = _graded(vivid, "memory").getpixel((0, 0))
        self.assertLess(max(after_px) - min(after_px), before)

    def test_every_declared_grade_stays_in_range(self):
        from backend.services.parallax import _GRADES
        from PIL import Image
        for grade in _GRADES:
            for value in (0, 120, 255):
                px = self._rgb(grade) if value == 120 else __import__(
                    "backend.services.parallax", fromlist=["_graded"]
                )._graded(Image.new("RGB", (4, 4), (value,) * 3), grade).getpixel((0, 0))
                self.assertTrue(all(0 <= c <= 255 for c in px), f"{grade} at {value}: {px}")
