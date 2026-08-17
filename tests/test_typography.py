"""Dashes belong to the voice, not to the audience.

A dash is a beat the narrator takes, so it stays in narration_text. Everywhere a
human reads instead of hears — captions, title, description, cover text, the
planning copy in the app — it becomes the comma it was standing in for.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend import pipeline
from backend.models import MusicTrack, Project
from backend.services.typography import dashless, dashless_copy


class ProseTests(unittest.TestCase):
    def test_a_dash_between_clauses_becomes_a_comma(self):
        self.assertEqual(
            dashless("the pantheon was completed — with one throne still empty."),
            "the pantheon was completed, with one throne still empty.",
        )

    def test_a_dash_with_no_air_around_it_is_still_punctuation(self):
        self.assertEqual(
            dashless("clad in bronze armor—a fearsome aegis at her chest."),
            "clad in bronze armor, a fearsome aegis at her chest.",
        )

    def test_a_spaced_hyphen_is_a_dash(self):
        self.assertEqual(
            dashless("Rise and fall - the whole story - in ninety seconds."),
            "Rise and fall, the whole story, in ninety seconds.",
        )

    def test_a_hyphen_inside_a_word_is_spelling(self):
        text = "She came out full-grown, armored, and royalty-free."
        self.assertEqual(dashless(text), text)

    def test_urls_are_left_alone(self):
        text = "Source: https://some-site.com/a-b-c"
        self.assertEqual(dashless(text), text)

    def test_a_dash_between_numbers_is_a_range_not_a_list(self):
        self.assertEqual(
            dashless("between 1200–800 BC, over 30–40 years"),
            "between 1200-800 BC, over 30-40 years",
        )

    def test_a_dash_at_either_end_just_goes(self):
        self.assertEqual(dashless("A god born twice —"), "A god born twice")
        self.assertEqual(dashless("— And then, silence."), "And then, silence.")

    def test_no_comma_is_doubled_onto_existing_punctuation(self):
        self.assertEqual(dashless("Wait, — there was more."), "Wait, there was more.")
        self.assertEqual(dashless("Stopped. — Then it moved."), "Stopped. Then it moved.")

    def test_line_breaks_survive(self):
        self.assertEqual(
            dashless("First line — with a clause\nSecond line"),
            "First line, with a clause\nSecond line",
        )

    def test_a_dash_opening_a_line_does_not_comma_the_line_above(self):
        self.assertEqual(dashless("Title\n— Subtitle"), "Title\nSubtitle")

    def test_copy_walks_lists_and_dicts(self):
        self.assertEqual(
            dashless_copy({"title": "A — B", "tags": ["x — y"], "n": 3}),
            {"title": "A, B", "tags": ["x, y"], "n": 3},
        )


class MetadataTests(unittest.TestCase):
    """The published copy is cleaned; the soundtrack credit is reproduced."""

    def _write(self, folder: Path, metadata: dict, track=None) -> dict:
        (folder / "final").mkdir(parents=True, exist_ok=True)
        (folder / "script.json").write_text(
            json.dumps({"scenes": [], "metadata": metadata}), encoding="utf-8"
        )
        pipeline._write_metadata(folder, Project(title="T", topic_prompt="x"), track)
        return json.loads((folder / "final" / "metadata.json").read_text(encoding="utf-8"))

    def test_title_description_and_cover_copy_lose_their_dashes(self):
        with TemporaryDirectory() as tmp:
            meta = self._write(Path(tmp), {
                "title": "Zeus Drowned the World — Two People Survived",
                "description": "It ended — and then it began again.",
                "hashtags": ["#myth — tok"],
                "cover_kicker": "Born from a split skull —",
                "cover_title": "Athena",
            })
        self.assertEqual(meta["title"], "Zeus Drowned the World, Two People Survived")
        self.assertEqual(meta["description"], "It ended, and then it began again.")
        self.assertEqual(meta["hashtags"], ["#myth, tok"])
        self.assertEqual(meta["cover_kicker"], "Born from a split skull")

    def test_the_music_credit_is_reproduced_exactly(self):
        """Attribution is a licence obligation, not copy of ours to edit."""
        track = MusicTrack(
            provider="local",
            provider_track_id="1",
            title="Whisper In The Deep",
            artist_name="Royalty Free Zone - Epic Journey",
            share_url="https://example.com/t",
        )
        with TemporaryDirectory() as tmp:
            meta = self._write(Path(tmp), {"title": "T", "description": "A — B"}, track)
        self.assertIn("Royalty Free Zone - Epic Journey", meta["music_attribution"])
        self.assertIn("Royalty Free Zone - Epic Journey", meta["description"])
        self.assertTrue(meta["description"].startswith("A, B"))


if __name__ == "__main__":
    unittest.main()
