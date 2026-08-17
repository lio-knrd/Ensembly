"""Burned-in subtitles: per-project on/off switch and vertical placement.

Placement is stored on the project and clamped to a renderable range; the
default is a constant, so a project that moves its captions never changes where
the next project starts.
"""
import re
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
from backend.services.ffmpeg import (
    H,
    W,
    HOOK_HOLD_SECONDS,
    HOOK_KICKER_SIZE,
    HOOK_MAX_LINES,
    HOOK_SIDE_MARGIN,
    HOOK_SIZE_MAX,
    HOOK_TOP_MARGIN,
    _dashless_words,
    _fmt_ass_time,
    _hook_lines,
    _hook_measurer,
    build_ass_captions,
)

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


class HookCardTests(unittest.TestCase):
    """The opening hook, burned over the top of the frame for its first seconds."""

    def _events(self, path: Path, style: str) -> list[str]:
        return [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("Dialogue:") and f",{style}," in line
        ]

    def _hook_style(self, path: Path) -> list[str]:
        line = next(
            l for l in path.read_text(encoding="utf-8").splitlines()
            if l.startswith("Style: Hook,")
        )
        return line.split(",")

    def test_no_hook_means_no_hook_event(self):
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(TIMELINE, Path(tmp) / "captions.ass")
            self.assertEqual(self._events(out, "Hook"), [])

    def _text(self, events: list[str]) -> str:
        return " ".join(e.split("}")[-1] for e in events)

    def test_hook_is_burned_from_zero_for_its_hold(self):
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(
                TIMELINE, Path(tmp) / "captions.ass",
                hook_text="Zeus drowned every human on earth.",
            )
            events = self._events(out, "Hook")
            self.assertTrue(events)
            for event in events:
                self.assertIn("0:00:00.00", event)
                self.assertIn(_fmt_ass_time(HOOK_HOLD_SECONDS), event)
            self.assertEqual(self._text(events), "Zeus drowned every human on earth.")

    def test_each_line_is_placed_individually_for_its_leading(self):
        """ASS has no line-spacing control, so the lines carry their own y."""
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(
                TIMELINE, Path(tmp) / "captions.ass",
                hook_text="Zeus drowned every human on earth.",
            )
            events = self._events(out, "Hook")
            self.assertEqual(len(events), 2)
            ys = [int(re.search(r"\\pos\(\d+,(\d+)\)", e).group(1)) for e in events]
            self.assertEqual(sorted(ys), ys)
            size = int(self._hook_style(out)[2])
            # Tighter than the ~1.2x a renderer would apply on its own.
            self.assertLess(ys[1] - ys[0], size)

    def test_hook_sits_at_the_top_and_leaves_the_captions_alone(self):
        """Its own alignment and position, so it never displaces the narration."""
        with TemporaryDirectory() as tmp:
            plain = build_ass_captions(TIMELINE, Path(tmp) / "a.ass")
            hooked = build_ass_captions(
                TIMELINE, Path(tmp) / "b.ass", hook_text="A goddess stepped out."
            )
            self.assertEqual(
                self._events(plain, "Caption"), self._events(hooked, "Caption")
            )
            # Format: ..., Alignment, MarginL, MarginR, MarginV, Encoding
            self.assertEqual(self._hook_style(hooked)[-5], "8")
            first = self._events(hooked, "Hook")[0]
            self.assertIn(r"\pos(%d,%d)" % (W // 2, round(HOOK_TOP_MARGIN * H)), first)

    def test_a_kicker_is_set_above_the_hook_in_capitals(self):
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(
                TIMELINE, Path(tmp) / "captions.ass",
                hook_text="Zeus drowned every human on earth.",
                hook_kicker="The Great Flood",
            )
            kick = self._events(out, "Kick")
            self.assertEqual(len(kick), 1)
            self.assertEqual(self._text(kick), "THE GREAT FLOOD")
            kick_y = int(re.search(r"\\pos\(\d+,(\d+)\)", kick[0]).group(1))
            hook_y = int(
                re.search(r"\\pos\(\d+,(\d+)\)", self._events(out, "Hook")[0]).group(1)
            )
            self.assertGreater(hook_y, kick_y)

    def test_a_kicker_without_a_hook_is_not_drawn_alone(self):
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(
                TIMELINE, Path(tmp) / "captions.ass", hook_kicker="The Great Flood"
            )
            self.assertEqual(self._events(out, "Kick"), [])

    def test_the_hook_is_sized_to_fill_the_frame_width(self):
        """A short hook gets bigger type than a long one, both inside the frame."""
        usable = W - 2 * HOOK_SIDE_MARGIN
        short_size, short_lines = _hook_lines("Athena was born armed.")
        long_size, long_lines = _hook_lines("Zeus drowned every human on earth.")
        self.assertGreater(short_size, long_size)
        self.assertLessEqual(short_size, HOOK_SIZE_MAX)
        for size, lines in ((short_size, short_lines), (long_size, long_lines)):
            self.assertLessEqual(len(lines), HOOK_MAX_LINES)
            width = _hook_measurer(size)
            self.assertLessEqual(max(width(l) for l in lines), usable)

    def test_an_over_long_hook_takes_a_third_line_rather_than_one_unwrapped(self):
        """Left on one line, libass would break it at its own width."""
        usable = W - 2 * HOOK_SIDE_MARGIN
        size, lines = _hook_lines(
            "Zeus judged every living human unworthy and drowned them all."
        )
        self.assertGreater(len(lines), HOOK_MAX_LINES)
        width = _hook_measurer(size)
        self.assertLessEqual(max(width(l) for l in lines), usable)

    def test_a_wrapped_hook_does_not_strand_a_single_word(self):
        """A greedy wrap left "earth." alone on its own line."""
        _, lines = _hook_lines("Zeus drowned every human on earth.")
        self.assertEqual(len(lines), 2)
        self.assertGreater(min(len(line.split()) for line in lines), 1)

    def test_whitespace_only_hook_is_treated_as_absent(self):
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(
                TIMELINE, Path(tmp) / "captions.ass", hook_text="   \n  "
            )
            self.assertEqual(self._events(out, "Hook"), [])


class DashCaptionTests(unittest.TestCase):
    """Dashes stay in the narration the voice reads and leave the caption."""

    def _words(self, *tokens):
        return _dashless_words([
            {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
            for i, w in enumerate(tokens)
        ])

    def test_a_dash_between_words_becomes_a_comma(self):
        out = self._words("cracked", "open", "—", "and", "a", "god.")
        self.assertEqual([w["word"] for w in out], ["cracked", "open,", "and", "a", "god."])

    def test_the_dropped_dash_hands_its_time_to_its_neighbour(self):
        out = self._words("open", "—", "and")
        # "open" now runs to where the dash ended, so "and" still starts on time.
        self.assertAlmostEqual(out[0]["end"], 0.9)
        self.assertAlmostEqual(out[1]["start"], 1.0)

    def test_a_hyphen_inside_a_word_is_spelling_and_survives(self):
        out = self._words("out,", "full-grown,", "armored.")
        self.assertEqual([w["word"] for w in out], ["out,", "full-grown,", "armored."])

    def test_a_dash_attached_to_a_word_becomes_a_comma(self):
        self.assertEqual([w["word"] for w in self._words("come—", "a", "god")],
                         ["come,", "a", "god"])
        self.assertEqual([w["word"] for w in self._words("come", "—a", "god")],
                         ["come,", "a", "god"])

    def test_no_comma_is_doubled_onto_existing_punctuation(self):
        self.assertEqual([w["word"] for w in self._words("wandered,", "—", "until")],
                         ["wandered,", "until"])
        self.assertEqual([w["word"] for w in self._words("stopped.", "—", "Then")],
                         ["stopped.", "Then"])

    def test_a_leading_dash_gives_its_start_to_the_first_word(self):
        out = self._words("—", "Then", "silence.")
        self.assertEqual([w["word"] for w in out], ["Then", "silence."])
        self.assertAlmostEqual(out[0]["start"], 0.0)

    def test_captions_render_without_a_dash(self):
        timeline = {"words": [
            {"word": "Artemis", "start": 0.0, "end": 0.5},
            {"word": "took", "start": 0.5, "end": 0.8},
            {"word": "the", "start": 0.8, "end": 0.9},
            {"word": "wild", "start": 0.9, "end": 1.3},
            {"word": "—", "start": 1.3, "end": 1.4},
            {"word": "the", "start": 1.4, "end": 1.6},
            {"word": "hunt.", "start": 1.6, "end": 2.0},
        ]}
        with TemporaryDirectory() as tmp:
            out = build_ass_captions(timeline, Path(tmp) / "captions.ass")
            events = [l for l in out.read_text(encoding="utf-8").splitlines()
                      if l.startswith("Dialogue:")]
            text = " ".join(e.split(",,")[-1] for e in events)
            self.assertNotIn("—", text)
            self.assertIn("wild,", text)


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
