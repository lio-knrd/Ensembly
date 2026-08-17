"""Long-narration TTS chunking: sentence-aware splitting + merged timelines.

The single-request path (short-form narration) is unchanged; these cover the
long-run path that stitches multiple ElevenLabs requests into one take so an
animation run renders as one continuous audio track and timeline.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.adapters.tts import ElevenLabsTTSGenerator, split_for_tts


class SplitForTtsTests(unittest.TestCase):
    def test_short_text_stays_one_chunk(self):
        self.assertEqual(split_for_tts("Hello world.", 5000), ["Hello world."])

    def test_blank_text(self):
        self.assertEqual(split_for_tts("   ", 5000), [""])

    def test_packs_whole_sentences_up_to_limit(self):
        text = "One two three. Four five six. Seven eight nine."
        chunks = split_for_tts(text, 24)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 24 for c in chunks))
        # Every chunk is a whole sentence boundary (ends with terminal punctuation).
        self.assertTrue(all(c.rstrip().endswith(".") for c in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_over_long_sentence_wraps_on_words(self):
        text = "alpha beta gamma delta epsilon zeta eta theta"
        chunks = split_for_tts(text, 12)
        self.assertTrue(all(len(c) <= 12 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_monster_token_is_hard_cut(self):
        text = "x" * 25
        chunks = split_for_tts(text, 10)
        self.assertTrue(all(len(c) <= 10 for c in chunks))
        self.assertEqual("".join(chunks), text)


class ChunkedSynthesisTests(unittest.TestCase):
    def test_merged_timeline_offsets_each_chunk_by_probed_duration(self):
        gen = ElevenLabsTTSGenerator(voice_id="v")
        responses = {
            "First one.": (
                b"a",
                [{"word": "First", "start": 0.0, "end": 0.4},
                 {"word": "one.", "start": 0.5, "end": 0.9}],
                0.9,
            ),
            "Second two.": (
                b"b",
                [{"word": "Second", "start": 0.0, "end": 0.4},
                 {"word": "two.", "start": 0.5, "end": 0.9}],
                0.9,
            ),
        }

        def fake_concat(parts, out_mp3):
            Path(out_mp3).write_bytes(b"joined")
            return out_mp3

        # Each part is 1.0s; the joined file is 2.0s.
        def fake_probe(path):
            return 2.0 if Path(path).name.startswith("out") else 1.0

        with TemporaryDirectory() as tmp:
            audio_out = Path(tmp) / "out.mp3"
            ts_out = Path(tmp) / "out.timestamps.json"
            with mock.patch.object(
                    gen, "_request", side_effect=lambda t, **_context: responses[t]
                 ), \
                 mock.patch("backend.services.ffmpeg.concat_audio", side_effect=fake_concat), \
                 mock.patch("backend.services.ffmpeg._probe_duration", side_effect=fake_probe):
                result = gen._synthesize_chunked(
                    "First one. Second two.", audio_out, ts_out, limit=11
                )

            data = json.loads(ts_out.read_text(encoding="utf-8"))
            self.assertTrue(audio_out.exists())

        self.assertEqual([w["word"] for w in data["words"]],
                         ["First", "one.", "Second", "two."])
        # Second chunk is shifted by the first part's probed 1.0s.
        self.assertEqual(data["words"][2]["start"], 1.0)
        self.assertEqual(data["words"][3]["end"], 1.9)
        self.assertEqual(data["duration"], 2.0)
        self.assertEqual(result.duration_seconds, 2.0)


if __name__ == "__main__":
    unittest.main()
