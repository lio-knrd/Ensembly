"""Burned-in subtitles: per-project on/off switch and vertical placement.

Placement is stored on the project and clamped to a renderable range; the
default is a constant, so a project that moves its captions never changes where
the next project starts.
"""
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from backend import pipeline
from backend.config import (
    SUBTITLE_POSITION_DEFAULT,
    SUBTITLE_POSITION_MAX,
    SUBTITLE_POSITION_MIN,
    clamp_subtitle_position,
    settings,
)
from backend.models import Project, Scene, Stage
from backend.services.ffmpeg import H, build_ass_captions

TIMELINE = {
    "words": [
        {"word": "Hello", "start": 0.0, "end": 0.4},
        {"word": "world.", "start": 0.4, "end": 0.9},
    ],
    "duration": 0.9,
}


def _margin_v(path: Path) -> int:
    style = next(l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("Style:"))
    # ... Alignment, MarginL, MarginR, MarginV, Encoding
    return int(style.split(",")[-2])


class ClampTests(unittest.TestCase):
    def test_in_range_value_is_kept(self):
        self.assertAlmostEqual(clamp_subtitle_position(0.4), 0.4)

    def test_out_of_range_values_are_clamped(self):
        self.assertAlmostEqual(clamp_subtitle_position(-3.0), SUBTITLE_POSITION_MIN)
        self.assertAlmostEqual(clamp_subtitle_position(9.0), SUBTITLE_POSITION_MAX)

    def test_garbage_falls_back_to_the_default(self):
        self.assertAlmostEqual(clamp_subtitle_position(None), SUBTITLE_POSITION_DEFAULT)
        self.assertAlmostEqual(clamp_subtitle_position("high"), SUBTITLE_POSITION_DEFAULT)


class CaptionPlacementTests(unittest.TestCase):
    def test_position_drives_the_style_margin(self):
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(TIMELINE, Path(tmp) / "captions.ass", position=0.5)
            self.assertEqual(_margin_v(out), round(0.5 * H))

    def test_default_position_is_used_when_unspecified(self):
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(TIMELINE, Path(tmp) / "captions.ass")
            self.assertEqual(_margin_v(out), round(SUBTITLE_POSITION_DEFAULT * H))

    def test_out_of_range_position_never_leaves_the_frame(self):
        with TemporaryDirectory() as tmp:
            low = build_ass_captions(TIMELINE, Path(tmp) / "low.ass", position=-1.0)
            high = build_ass_captions(TIMELINE, Path(tmp) / "high.ass", position=4.0)
            self.assertGreaterEqual(_margin_v(low), 0)
            self.assertLess(_margin_v(high), H)

    def test_placement_does_not_disturb_the_caption_events(self):
        with TemporaryDirectory() as tmp:
            default = build_ass_captions(TIMELINE, Path(tmp) / "a.ass")
            moved = build_ass_captions(TIMELINE, Path(tmp) / "b.ass", position=0.6)
            events = [
                [l for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("Dialogue:")]
                for p in (default, moved)
            ]
            self.assertEqual(events[0], events[1])
            self.assertIn("Hello world.", events[0][0])


class ProjectDefaultsTests(unittest.TestCase):
    def test_new_projects_start_from_the_shared_default(self):
        first = Project(title="One", topic_prompt="x")
        first.subtitle_position = 0.62  # a placement chosen for this project only
        second = Project(title="Two", topic_prompt="y")
        self.assertTrue(second.subtitles_enabled)
        self.assertAlmostEqual(first.subtitle_position, 0.62)
        self.assertAlmostEqual(second.subtitle_position, SUBTITLE_POSITION_DEFAULT)


class RenderWiringTests(unittest.TestCase):
    """The final render honours the project's switch and placement."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self._patch = mock.patch.object(pipeline, "engine", self.engine)
        self._patch.start()
        self.folder = settings.projects_dir / "_subtitle_render_test"
        (self.folder / "images").mkdir(parents=True, exist_ok=True)
        (self.folder / "audio").mkdir(parents=True, exist_ok=True)
        self.image = self.folder / "images" / "scene_01.png"
        self.image.write_bytes(b"not really a png")

    def tearDown(self):
        self._patch.stop()
        self.engine.dispose()
        shutil.rmtree(self.folder, ignore_errors=True)

    def _render(self, *, subtitles_enabled: bool, position: float):
        root = Path(settings.projects_dir.parent.parent)
        with Session(self.engine) as session:
            project = Project(
                title="Subtitle render test",
                topic_prompt="x",
                stage=Stage.CLIPS_APPROVED,
                folder_path=self.folder.relative_to(root).as_posix(),
                subtitles_enabled=subtitles_enabled,
                subtitle_position=position,
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            session.add(
                Scene(
                    project_id=project.id,
                    order_index=0,
                    narration_text="Hello world.",
                    image_path=self.image.relative_to(root).as_posix(),
                    duration_seconds=2.0,
                )
            )
            session.commit()
            project_id = project.id

        fake = mock.MagicMock()
        fake.merge_timestamps.return_value = {"words": [], "duration": 2.0}
        fake.build_ass_captions.side_effect = lambda timeline, out, **kw: out
        with mock.patch.object(pipeline, "ffmpeg", fake):
            pipeline.render(project_id)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Project, project_id).stage, Stage.DONE)
        return fake

    def test_enabled_render_burns_captions_at_the_project_position(self):
        fake = self._render(subtitles_enabled=True, position=0.42)
        self.assertEqual(fake.build_ass_captions.call_args.kwargs["position"], 0.42)
        captions = fake.render_final.call_args.args[2]
        self.assertEqual(Path(captions).name, "captions.ass")

    def test_disabled_render_passes_no_captions(self):
        fake = self._render(subtitles_enabled=False, position=0.42)
        fake.build_ass_captions.assert_not_called()
        self.assertIsNone(fake.render_final.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
