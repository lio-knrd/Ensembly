"""Image-to-video adapters (fal.ai default, offline ffmpeg Ken Burns).

Only scenes flagged `video` get a clip; duration is matched to the scene audio.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx

from ..config import settings
from .base import VideoGenerator


class FalVideoGenerator(VideoGenerator):
    name = "fal"

    # Kling v3 accepts an integer seconds duration in [3, 15] (sent as a string).
    _MIN_DURATION, _MAX_DURATION = 3, 15
    # Kling caps how many identity elements a single clip can reference.
    _MAX_ELEMENTS = 3

    def _payload(
        self,
        image_path: Path,
        prompt,
        duration_seconds,
        elements=None,
        end_image_path=None,
    ) -> dict:
        from .image import _to_data_uri

        duration = min(self._MAX_DURATION, max(self._MIN_DURATION, round(duration_seconds)))
        payload = {
            "prompt": prompt,
            "start_image_url": _to_data_uri(image_path),
            "duration": str(duration),
            "generate_audio": False,
            "aspect_ratio": "9:16",
        }
        if end_image_path is not None:
            payload["end_image_url"] = _to_data_uri(end_image_path)
        if elements:
            built = []
            for frontal, variants in elements[: self._MAX_ELEMENTS]:
                frontal_uri = _to_data_uri(frontal)
                ref_uris = [_to_data_uri(v) for v in variants] or [frontal_uri]
                built.append({"frontal_image_url": frontal_uri, "reference_image_urls": ref_uris})
            payload["elements"] = built
        return payload

    def submit(
        self,
        image_path: Path,
        prompt,
        duration_seconds,
        elements=None,
        end_image_path=None,
    ) -> dict[str, str]:
        """Submit a durable fal queue request and return its request URLs."""
        resp = httpx.post(
            f"https://queue.fal.run/{settings.fal_video_model}",
            headers={"Authorization": f"Key {settings.fal_api_key}"},
            json=self._payload(image_path, prompt, duration_seconds, elements, end_image_path),
            timeout=180,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"fal.ai queue {resp.status_code}: {resp.text[:600]}")
        data = resp.json()
        request_id = data.get("request_id")
        if not request_id:
            raise RuntimeError(f"fal.ai queue returned no request_id: {data}")
        return {
            "request_id": request_id,
            "status_url": data.get(
                "status_url",
                f"https://queue.fal.run/{settings.fal_video_model}/requests/{request_id}/status",
            ),
            "response_url": data.get(
                "response_url",
                f"https://queue.fal.run/{settings.fal_video_model}/requests/{request_id}",
            ),
        }

    def retrieve(
        self,
        request_id: str,
        out_path: Path,
        timeout_seconds: int = 1800,
        status_url: str | None = None,
        response_url: str | None = None,
    ) -> Path:
        """Poll a queued request, then stream its MP4 to disk."""
        status_url = status_url or (
            f"https://queue.fal.run/{settings.fal_video_model}/requests/{request_id}/status"
        )
        response_url = response_url or (
            f"https://queue.fal.run/{settings.fal_video_model}/requests/{request_id}"
        )
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Key {settings.fal_api_key}"}
        while time.time() < deadline:
            status = httpx.get(status_url, headers=headers, timeout=60, follow_redirects=True)
            if status.status_code >= 400:
                raise RuntimeError(
                    f"fal.ai queue status {status.status_code} ({status_url}): {status.text[:600]}"
                )
            data = status.json()
            state = str(data.get("status", "")).upper()
            if state == "COMPLETED":
                if data.get("error"):
                    raise RuntimeError(f"fal.ai queue failed: {data['error']}")
                response_url = data.get("response_url") or response_url
                break
            if state in {"FAILED", "CANCELED", "CANCELLED"}:
                raise RuntimeError(f"fal.ai queue {state.lower()}: {data.get('error') or data}")
            time.sleep(3)
        else:
            raise TimeoutError(f"fal.ai queue timed out for request {request_id}")

        result = httpx.get(response_url, headers=headers, timeout=60, follow_redirects=True)
        if result.status_code >= 400:
            raise RuntimeError(f"fal.ai queue result {result.status_code}: {result.text[:600]}")
        data = result.json()
        video = data.get("video") or {}
        url = video.get("url") or (data.get("videos") or [{}])[0].get("url")
        if not url:
            raise RuntimeError(f"fal.ai queue returned no video: {data}")

        timeout = httpx.Timeout(connect=60, read=900, write=60, pool=60)
        with httpx.stream("GET", url, timeout=timeout) as clip:
            clip.raise_for_status()
            with out_path.open("wb") as file_handle:
                for chunk in clip.iter_bytes():
                    file_handle.write(chunk)
        return out_path

    def generate(
        self,
        image_path: Path,
        prompt,
        out_path: Path,
        duration_seconds,
        elements=None,
        end_image_path=None,
    ):
        request = self.submit(
            image_path, prompt, duration_seconds, elements, end_image_path
        )
        return self.retrieve(
            request["request_id"],
            out_path,
            status_url=request.get("status_url"),
            response_url=request.get("response_url"),
        )


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
