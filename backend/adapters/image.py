"""Image-generation adapters (fal.ai default, offline PIL placeholder).

Reference images of tagged characters are passed to the model where supported,
so character identity stays locked across scenes.
"""
from __future__ import annotations

import base64
import hashlib
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
