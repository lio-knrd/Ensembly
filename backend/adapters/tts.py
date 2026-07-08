"""Text-to-speech adapters (ElevenLabs default, offline mock).

Both produce an mp3 plus a normalized word-level timestamps JSON:
    {"words": [{"word": str, "start": float, "end": float}, ...],
     "duration": float}
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import httpx

from ..config import settings
from .base import TTSGenerator, TTSResult


def _write_timestamps(path: Path, words: list[dict], duration: float) -> None:
    path.write_text(json.dumps({"words": words, "duration": duration}, indent=2), encoding="utf-8")


def _estimate_duration(text: str) -> float:
    n_words = max(1, len(text.split()))
    return round(n_words / 2.5, 3)  # ~2.5 words/sec


class ElevenLabsTTSGenerator(TTSGenerator):
    name = "elevenlabs"

    def __init__(self, voice_id: str | None = None) -> None:
        self.voice_id = (voice_id or settings.elevenlabs_voice_id).strip()

    def synthesize(self, text, audio_out: Path, timestamps_out: Path) -> TTSResult:
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/"
            f"{self.voice_id}/with-timestamps"
        )
        resp = httpx.post(
            url,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            json={"text": text, "model_id": settings.elevenlabs_model},
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        audio_out.write_bytes(base64.b64decode(data["audio_base64"]))

        words = self._chars_to_words(text, data.get("alignment", {}))
        duration = words[-1]["end"] if words else _estimate_duration(text)
        _write_timestamps(timestamps_out, words, duration)
        return TTSResult(audio_out, timestamps_out, duration)

    @staticmethod
    def _chars_to_words(text: str, alignment: dict) -> list[dict]:
        chars = alignment.get("characters", [])
        starts = alignment.get("character_start_times_seconds", [])
        ends = alignment.get("character_end_times_seconds", [])
        words: list[dict] = []
        cur, cur_start = "", None
        for i, ch in enumerate(chars):
            if ch.isspace():
                if cur:
                    words.append({"word": cur, "start": cur_start, "end": ends[i - 1]})
                    cur, cur_start = "", None
                continue
            if not cur:
                cur_start = starts[i]
            cur += ch
        if cur:
            words.append({"word": cur, "start": cur_start, "end": ends[-1] if ends else 0.0})
        return words


class OfflineTTSGenerator(TTSGenerator):
    """Silent audio of the estimated length + evenly-spaced word timings."""

    name = "offline"

    def synthesize(self, text, audio_out: Path, timestamps_out: Path) -> TTSResult:
        duration = _estimate_duration(text)
        self._silent_mp3(audio_out, duration)
        words = self._even_words(text, duration)
        _write_timestamps(timestamps_out, words, duration)
        return TTSResult(audio_out, timestamps_out, duration)

    @staticmethod
    def _silent_mp3(out: Path, duration: float) -> None:
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"anullsrc=r=44100:cl=mono", "-t", f"{duration:.3f}",
                    "-q:a", "9", str(out),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            # No ffmpeg available: write a tiny placeholder file so paths exist.
            out.write_bytes(b"")

    @staticmethod
    def _even_words(text: str, duration: float) -> list[dict]:
        tokens = text.split() or ["silence"]
        step = duration / len(tokens)
        return [
            {"word": w, "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
            for i, w in enumerate(tokens)
        ]
