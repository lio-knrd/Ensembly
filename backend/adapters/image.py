"""Image-generation adapters (fal.ai default, offline PIL placeholder).

Reference images of tagged characters are passed to the model where supported,
so character identity stays locked across scenes.
"""
from __future__ import annotations

import base64
import hashlib
import time
import textwrap
from pathlib import Path

import httpx

from ..config import settings
from .base import ImageGenerator

# 9:16 vertical, a sensible short-form default.
WIDTH, HEIGHT = 720, 1280


class FalImageGenerator(ImageGenerator):
    name = "fal"

    def generate(self, prompt, out_path: Path, reference_images=None):
        payload: dict = {"prompt": prompt, "image_size": {"width": WIDTH, "height": HEIGHT}}
        if reference_images:
            # For models that accept identity references (best-effort).
            payload["image_url"] = _to_data_uri(reference_images[0])
        resp = httpx.post(
            f"https://fal.run/{settings.fal_image_model}",
            headers={"Authorization": f"Key {settings.fal_api_key}"},
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        images = resp.json().get("images", [])
        if not images:
            raise RuntimeError("fal.ai returned no images")
        img = httpx.get(images[0]["url"], timeout=120)
        img.raise_for_status()
        out_path.write_bytes(img.content)
        return out_path


class KreaImageGenerator(ImageGenerator):
    name = "krea"

    _ENDPOINT = "krea/v2/medium/text-to-image"

    def generate(self, prompt, out_path: Path, reference_images=None):
        payload: dict = {
            "prompt": prompt,
            "aspect_ratio": "9:16",
            "creativity": "medium",
        }
        if reference_images:
            payload["image_style_references"] = [
                {"image_url": _to_data_uri(path)} for path in reference_images[:10]
            ]
        status = _queue_submit(self._ENDPOINT, payload)
        result = _queue_result(self._ENDPOINT, status)
        images = result.get("images", [])
        if not images:
            raise RuntimeError("fal.ai Krea returned no images")
        img = httpx.get(images[0]["url"], timeout=120)
        img.raise_for_status()
        out_path = out_path.with_suffix(".png")
        out_path.write_bytes(img.content)
        return out_path


class OfflineImageGenerator(ImageGenerator):
    """Deterministic placeholder still so storyboards render without a fal key."""

    name = "offline"

    def generate(self, prompt, out_path: Path, reference_images=None):
        from PIL import Image, ImageDraw

        seed = int(hashlib.sha256(prompt.encode()).hexdigest(), 16)
        # A calm, muted gradient keyed off the prompt hash.
        top = ((seed >> 0) % 60 + 30, (seed >> 8) % 60 + 30, (seed >> 16) % 80 + 40)
        bottom = ((seed >> 24) % 40 + 15, (seed >> 32) % 40 + 15, (seed >> 40) % 50 + 25)

        img = Image.new("RGB", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(img)
        for y in range(HEIGHT):
            t = y / HEIGHT
            draw.line(
                [(0, y), (WIDTH, y)],
                fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)),
            )
        wrapped = textwrap.fill(prompt, width=34)[:600]
        draw.multiline_text((48, 96), wrapped, fill=(235, 232, 224), spacing=8)
        draw.text((48, HEIGHT - 72), "offline placeholder", fill=(200, 195, 185))
        if reference_images:
            draw.text((48, HEIGHT - 112), f"refs: {len(reference_images)}", fill=(200, 195, 185))
        out_path = out_path.with_suffix(".png")
        img.save(out_path)
        return out_path


def _to_data_uri(path: Path) -> str:
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode()
    suffix = Path(path).suffix.lstrip(".") or "png"
    return f"data:image/{suffix};base64,{b64}"


def _queue_submit(endpoint: str, payload: dict) -> dict:
    resp = httpx.post(
        f"https://queue.fal.run/{endpoint}",
        headers={"Authorization": f"Key {settings.fal_api_key}"},
        json=payload,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"fal.ai {resp.status_code}: {resp.text[:600]}")
    return resp.json()


def _queue_result(endpoint: str, status: dict, timeout_seconds: int = 300) -> dict:
    request_id = status.get("request_id")
    if not request_id:
        raise RuntimeError(f"fal.ai queue returned no request_id: {status}")
    status_url = status.get("status_url") or f"https://queue.fal.run/{endpoint}/requests/{request_id}/status"
    response_url = status.get("response_url") or f"https://queue.fal.run/{endpoint}/requests/{request_id}/response"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        poll = httpx.get(
            status_url,
            headers={"Authorization": f"Key {settings.fal_api_key}"},
            timeout=30,
        )
        if poll.status_code >= 400:
            raise RuntimeError(f"fal.ai queue status {poll.status_code}: {poll.text[:600]}")
        data = poll.json()
        if data.get("status") == "COMPLETED":
            if data.get("error"):
                raise RuntimeError(f"fal.ai queue failed: {data.get('error')}")
            result = httpx.get(
                data.get("response_url") or response_url,
                headers={"Authorization": f"Key {settings.fal_api_key}"},
                timeout=60,
            )
            if result.status_code >= 400:
                raise RuntimeError(f"fal.ai queue result {result.status_code}: {result.text[:600]}")
            return result.json()
        time.sleep(1.5)
    raise TimeoutError(f"fal.ai queue timed out for {endpoint}")
