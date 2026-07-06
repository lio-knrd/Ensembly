"""Image-to-video adapters (fal.ai default, offline ffmpeg Ken Burns).

Only scenes flagged `video` get a clip; duration is matched to the scene audio.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from ..config import settings
from .base import VideoGenerator


class FalVideoGenerator(VideoGenerator):
    name = "fal"

    # Kling v3 accepts an integer seconds duration in [3, 15] (sent as a string).
    _MIN_DURATION, _MAX_DURATION = 3, 15
    # Kling caps how many identity elements a single clip can reference.
    _MAX_ELEMENTS = 4

    def generate(
        self,
        image_path: Path,
        prompt,
        out_path: Path,
        duration_seconds,
        elements=None,
        end_image_path=None,
    ):
        from .image import _to_data_uri

        duration = min(self._MAX_DURATION, max(self._MIN_DURATION, round(duration_seconds)))
        payload = {
            "prompt": prompt,
            "start_image_url": _to_data_uri(image_path),
            "duration": str(duration),
            "generate_audio": False,  # narration is muxed separately from ElevenLabs
            "aspect_ratio": "9:16",  # vertical, matches the final 1080x1920 render
        }
        if end_image_path is not None:
            payload["end_image_url"] = _to_data_uri(end_image_path)
        if elements:
            built = []
            for frontal, variants in elements[: self._MAX_ELEMENTS]:
                frontal_uri = _to_data_uri(frontal)
                # Kling requires a non-empty reference_image_urls; fall back to
                # the reference sheet itself when a character has no variants.
                ref_uris = [_to_data_uri(v) for v in variants] or [frontal_uri]
                built.append({"frontal_image_url": frontal_uri, "reference_image_urls": ref_uris})
            payload["elements"] = built
        resp = httpx.post(
            f"https://fal.run/{settings.fal_video_model}",
            headers={"Authorization": f"Key {settings.fal_api_key}"},
            json=payload,
            timeout=600,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"fal.ai {resp.status_code}: {resp.text[:600]}")
        data = resp.json()
        video = data.get("video") or {}
        url = video.get("url") or (data.get("videos") or [{}])[0].get("url")
        if not url:
            raise RuntimeError("fal.ai returned no video")
        clip = httpx.get(url, timeout=300)
        clip.raise_for_status()
        out_path.write_bytes(clip.content)
        return out_path


class OfflineVideoGenerator(VideoGenerator):
    """Ken Burns pan/zoom over the still via ffmpeg — a stand-in motion clip."""

    name = "offline"

    def generate(
        self,
        image_path: Path,
        prompt,
        out_path: Path,
        duration_seconds,
        elements=None,
        end_image_path=None,
    ):
        from ..services.ffmpeg import ken_burns_clip

        ken_burns_clip(image_path, out_path, max(1.0, duration_seconds))
        return out_path
