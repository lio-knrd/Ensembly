"""Bringing already-rendered projects onto the current music-credit wording.

The credit is written into final/metadata.json at render time, so changing the
wording only helps future renders unless old metadata is rewritten too.
"""
import json
import unittest
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from backend import pipeline
from backend.models import MusicTrack, Project
from backend.pipeline import _without_attribution, refresh_music_attribution
from backend.routers.projects import without_links

OLD_CREDIT = (
    'Music credit:\n"Whisper in the Deep"\nSource: Local library\n'
    "License: Royalty-free user-provided track"
)
STORY = "Zeus divided the cosmos and the Twelve Olympians took their thrones."


class MusicAttributionBackfillTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.root = self.enterContext(__import__("tempfile").TemporaryDirectory())
        patches = [
            mock.patch("backend.pipeline.engine", self.engine),
            mock.patch(
                "backend.pipeline._folder",
                lambda project: __import__("pathlib").Path(self.root) / project.folder_path,
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.dispose()

    def _project(self, meta, *, music_enabled=True, track=True, folder="proj"):
        from pathlib import Path

        with Session(self.engine) as session:
            track_id = None
            if track:
                music = MusicTrack(
                    provider="local",
                    provider_track_id="whisper-in-the-deep",
                    title="Whisper In The Deep",
                    artist_name="Royalty Free Zone - Epic Journey",
                    license_url="royalty-free",
                    share_url="https://www.youtube.com/watch?v=cqMbxcC5LMU",
                    local_path="data/music/whisper-in-the-deep.mp3",
                )
                session.add(music)
                session.commit()
                session.refresh(music)
                track_id = music.id
            project = Project(
                title="The Twelve Thrones",
                topic_prompt="Olympus",
                folder_path=folder,
                music_enabled=music_enabled,
                music_track_id=track_id,
            )
            session.add(project)
            session.commit()

        path = Path(self.root) / folder / "final" / "metadata.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return path

    def _read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_placeholder_credit_is_replaced_with_the_real_source(self):
        path = self._project({
            "title": "The Twelve Thrones",
            "description": f"{STORY}\n\n{OLD_CREDIT}",
            "music_attribution": OLD_CREDIT,
        })

        self.assertEqual(["The Twelve Thrones"], refresh_music_attribution())

        meta = self._read(path)
        self.assertIn("https://www.youtube.com/watch?v=cqMbxcC5LMU", meta["description"])
        self.assertIn("Royalty Free Zone - Epic Journey", meta["music_attribution"])
        self.assertNotIn("Local library", meta["description"])
        self.assertNotIn("user-provided track", meta["description"])
        # Only the credit changes; the story keeps its own text.
        self.assertTrue(meta["description"].startswith(STORY))
        self.assertEqual(1, meta["description"].count("Music credit:"))

    def test_a_render_that_predates_credits_gets_one(self):
        path = self._project({"title": "The Twelve Thrones", "description": STORY})

        self.assertEqual(["The Twelve Thrones"], refresh_music_attribution())

        meta = self._read(path)
        self.assertTrue(meta["description"].startswith(STORY))
        self.assertIn("Music credit:", meta["description"])

    def test_a_project_rendered_without_music_is_left_alone(self):
        path = self._project(
            {"title": "The Twelve Thrones", "description": STORY}, music_enabled=False
        )

        self.assertEqual([], refresh_music_attribution())

        self.assertNotIn("Music credit:", self._read(path)["description"])

    def test_running_twice_changes_nothing_the_second_time(self):
        path = self._project({
            "title": "The Twelve Thrones",
            "description": f"{STORY}\n\n{OLD_CREDIT}",
            "music_attribution": OLD_CREDIT,
        })

        refresh_music_attribution()
        after_first = path.read_text(encoding="utf-8")
        self.assertEqual([], refresh_music_attribution())

        self.assertEqual(after_first, path.read_text(encoding="utf-8"))

    def test_a_missing_metadata_file_is_skipped(self):
        self._project({"title": "x", "description": STORY})
        (__import__("pathlib").Path(self.root) / "proj" / "final" / "metadata.json").unlink()

        self.assertEqual([], refresh_music_attribution())


class AttributionStrippingTests(unittest.TestCase):
    def test_a_hand_edited_credit_mid_description_is_not_touched(self):
        # The credit is not in its trailing block, so where it ends cannot be
        # known; rewriting would risk eating the creator's own text.
        self.assertIsNone(
            _without_attribution("Music credit: see below. Then the story.", "")
        )

    def test_a_credit_written_by_an_older_format_is_still_found(self):
        stripped = _without_attribution(f"{STORY}\n\nMusic credit:\n\"Old\"\nSource: x", "")

        self.assertEqual(STORY, stripped)

    def test_a_description_with_no_credit_comes_back_whole(self):
        self.assertEqual(STORY, _without_attribution(STORY, ""))


if __name__ == "__main__":
    unittest.main()


class TikTokCaptionLinkTests(unittest.TestCase):
    """TikTok gets the credit without the addresses; YouTube keeps both.

    All three videos posted from this pipeline carried a YouTube URL in their
    caption, appended by the soundtrack credit. None of the fourteen posted
    before it did.
    """

    def test_a_source_line_that_was_only_a_url_is_dropped(self):
        caption = without_links(
            f"{STORY}\n\nMusic credit:\n"
            '"Whisper In The Deep" by Royalty Free Zone\n'
            "Source: https://www.youtube.com/watch?v=cqMbxcC5LMU\n"
            "License: Royalty-free, free to use\n"
            "Changes: shortened and mixed with narration."
        )
        self.assertNotIn("http", caption)
        self.assertNotIn("Source", caption)
        # The obligation is to name the work, the artist, the licence and the
        # change, and all four survive.
        self.assertIn('"Whisper In The Deep" by Royalty Free Zone', caption)
        self.assertIn("License: Royalty-free, free to use", caption)
        self.assertIn("Changes: shortened", caption)
        self.assertIn(STORY, caption)

    def test_a_licence_line_keeps_its_name_when_the_deed_url_goes(self):
        caption = without_links(
            "License: CC BY 3.0 (https://creativecommons.org/licenses/by/3.0/)"
        )
        self.assertEqual(caption, "License: CC BY 3.0")

    def test_prose_around_a_url_survives(self):
        self.assertEqual(
            without_links("Read more at https://example.com/x today."),
            "Read more at today.",
        )

    def test_a_caption_with_no_links_is_unchanged(self):
        self.assertEqual(without_links(STORY), STORY)
