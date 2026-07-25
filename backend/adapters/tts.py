"""Text-to-speech adapters (ElevenLabs default, offline mock).

Both produce an mp3 plus a normalized word-level timestamps JSON:
    {"words": [{"word": str, "start": float, "end": float}, ...],
     "duration": float}
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path

import httpx

from ..config import settings
from .base import TTSGenerator, TTSResult


def _write_timestamps(path: Path, words: list[dict], duration: float) -> None:
    path.write_text(json.dumps({"words": words, "duration": duration}, indent=2), encoding="utf-8")


def _estimate_duration(text: str) -> float:
    n_words = max(1, len(text.split()))
    return round(n_words / 2.5, 3)  # ~2.5 words/sec


# A sentence ends at .!? plus any trailing quotes/brackets; a trailing fragment
# with no terminal punctuation is kept as its own piece.
_SENTENCE_RE = re.compile(r"\S.*?(?:[.!?]+[\"')\]]*|\Z)", re.S)


def _sentences(text: str) -> list[str]:
    return [piece.strip() for piece in _SENTENCE_RE.findall(text) if piece.strip()]


def _wrap_piece(piece: str, limit: int) -> list[str]:
    """Split one over-long sentence on word boundaries, hard-cutting monster tokens."""
    if len(piece) <= limit:
        return [piece]
    out: list[str] = []
    current = ""
    for word in piece.split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= limit:
            current = f"{current} {word}"
        else:
            out.append(current)
            current = word
        while len(current) > limit:  # a single token longer than the limit
            out.append(current[:limit])
            current = current[limit:]
    if current:
        out.append(current)
    return out


def split_for_tts(text: str, limit: int) -> list[str]:
    """Pack narration into <=``limit``-char chunks, preferring sentence boundaries.

    Whole sentences are greedily packed up to the limit; a sentence that is
    itself too long is wrapped on word boundaries. Returns a single chunk when
    the text already fits.
    """
    text = " ".join(text.split())
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in _sentences(text):
        for piece in _wrap_piece(sentence, limit):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= limit:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


class ElevenLabsTTSGenerator(TTSGenerator):
    name = "elevenlabs"

    def __init__(self, voice_id: str | None = None) -> None:
        self.voice_id = (voice_id or settings.elevenlabs_voice_id).strip()

    def synthesize(self, text, audio_out: Path, timestamps_out: Path) -> TTSResult:
        audio_out.parent.mkdir(parents=True, exist_ok=True)
        # Cap the soft target below the hard ~10k API limit, with margin.
        limit = max(1000, min(settings.elevenlabs_max_chars_per_request, 9000))
        # Common case (short-form narration) stays a single request — byte-for-byte
        # the previous behavior — so only genuinely long runs get chunked.
        if len(text) <= limit:
            audio, words, duration = self._request(text)
            audio_out.write_bytes(audio)
            _write_timestamps(timestamps_out, words, duration)
            return TTSResult(audio_out, timestamps_out, duration)
        return self._synthesize_chunked(text, audio_out, timestamps_out, limit)

    def _request(self, text: str) -> tuple[bytes, list[dict], float]:
        """One TTS call: raw mp3 bytes, word timings, and the chunk's duration."""
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
        audio = base64.b64decode(data["audio_base64"])
        words = self._chars_to_words(text, data.get("alignment", {}))
        duration = words[-1]["end"] if words else _estimate_duration(text)
        return audio, words, duration

    def _synthesize_chunked(
        self, text: str, audio_out: Path, timestamps_out: Path, limit: int
    ) -> TTSResult:
        """Synthesize long narration in multiple requests, stitched into one take.

        Each chunk's word times are offset by the real (probed) duration of the
        preceding audio, so the merged timeline stays accurate across the joins
        that animation cues and burned captions anchor to.
        """
        from ..services import ffmpeg

        chunks = split_for_tts(text, limit)
        global_words: list[dict] = []
        offset = 0.0
        with tempfile.TemporaryDirectory() as tmp:
            parts: list[Path] = []
            for idx, chunk in enumerate(chunks):
                audio, words, duration = self._request(chunk)
                part = Path(tmp) / f"part_{idx:03d}.mp3"
                part.write_bytes(audio)
                parts.append(part)
                for word in words:
                    global_words.append({
                        "word": word["word"],
                        "start": round(offset + word["start"], 3),
                        "end": round(offset + word["end"], 3),
                    })
                offset += ffmpeg._probe_duration(part) or duration
            ffmpeg.concat_audio(parts, audio_out)
        total = ffmpeg._probe_duration(audio_out) or offset
        _write_timestamps(timestamps_out, global_words, round(total, 3))
        return TTSResult(audio_out, timestamps_out, round(total, 3))

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
