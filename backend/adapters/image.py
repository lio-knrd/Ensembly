"""Image-generation adapters (fal.ai default, offline PIL placeholder).

Reference images of tagged characters are passed to the model where supported,
so character identity stays locked across scenes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import threading
import time
import textwrap
from io import BytesIO
from pathlib import Path

import httpx

from ..config import settings
from ..prompts import with_image_content_limits
from .base import ImageGenerator

# 9:16 vertical, a sensible short-form default.
WIDTH, HEIGHT = 720, 1280

_KREA_API_BASE = "https://api.krea.ai"
_KREA_DIRECT_ENDPOINT = "/generate/image/krea/krea-2/medium"
_KREA_ASSET_CACHE_LOCK = threading.Lock()


def _moderated(prompt: str) -> str:
    """Carry the house content limits on every prompt that leaves for a model.

    Applied here, at the edge, rather than at the many call sites that build
    prompts, so a new caller cannot forget it. The offline generator is exempt:
    it moderates nothing and would only draw the clause onto the placeholder.
    """
    return with_image_content_limits(prompt) if settings.safe_image_prompts else prompt


class FalImageGenerator(ImageGenerator):
    name = "fal"

    def generate(
        self, prompt, out_path: Path, reference_images=None, reference_strengths=None
    ):
        payload: dict = {
            "prompt": _moderated(prompt),
            "image_size": {"width": WIDTH, "height": HEIGHT},
        }
        if reference_images:
            # This endpoint accepts one image field; pack multiple refs into a
            # numbered contact sheet so characters and continuity refs arrive together.
            payload["image_url"] = _reference_data_uri(reference_images)
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

    def generate(
        self, prompt, out_path: Path, reference_images=None, reference_strengths=None
    ):
        payload: dict = {
            "prompt": _moderated(prompt),
            "aspect_ratio": "9:16",
            "creativity": "medium",
        }
        if reference_images:
            payload["image_style_references"] = [
                {
                    "image_url": _to_data_uri(path),
                    "strength": _reference_strength(reference_strengths, index),
                }
                for index, path in enumerate(reference_images[:10])
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


class KreaDirectImageGenerator(ImageGenerator):
    """Krea 2 Medium through Krea's own API, bypassing fal.ai."""

    name = "krea-direct"

    def generate(
        self, prompt, out_path: Path, reference_images=None, reference_strengths=None
    ):
        payload: dict = {
            "prompt": _moderated(prompt),
            "aspect_ratio": "9:16",
            "resolution": "1K",
            "creativity": "medium",
        }
        if reference_images:
            payload["image_style_references"] = [
                {
                    "url": self._asset_url(path),
                    "strength": _reference_strength(reference_strengths, index),
                }
                for index, path in enumerate(reference_images[:10])
            ]

        headers = {
            "Authorization": f"Bearer {settings.krea_api_key}",
            "Content-Type": "application/json",
        }
        response = httpx.post(
            f"{_KREA_API_BASE}{_KREA_DIRECT_ENDPOINT}",
            headers=headers,
            json=payload,
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Krea API {response.status_code}: {response.text[:600]}")
        job = response.json()
        job_id = job.get("job_id")
        if not job_id:
            raise RuntimeError(f"Krea API returned no job_id: {job}")

        result = self._job_result(job_id, headers)
        urls = (result.get("result") or {}).get("urls", [])
        if not urls:
            raise RuntimeError(f"Krea API returned no images: {result}")
        image = httpx.get(urls[0], timeout=120)
        image.raise_for_status()
        out_path = out_path.with_suffix(".png")
        out_path.write_bytes(image.content)
        return out_path

    @staticmethod
    def _asset_url(path: Path) -> str:
        path = Path(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cache_path = settings.krea_asset_cache_file
        with _KREA_ASSET_CACHE_LOCK:
            cache = _load_krea_asset_cache(cache_path)
            cached = cache.get(digest)
            if cached:
                return cached

            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            with path.open("rb") as file_handle:
                response = httpx.post(
                    f"{_KREA_API_BASE}/assets",
                    headers={"Authorization": f"Bearer {settings.krea_api_key}"},
                    files={"file": (path.name, file_handle, mime)},
                    data={"description": f"Ensembly reference: {path.name}"},
                    timeout=120,
                )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Krea asset upload {response.status_code}: {response.text[:600]}"
                )
            asset = response.json()
            url = asset.get("image_url") or asset.get("url")
            if not url:
                raise RuntimeError(f"Krea asset upload returned no URL: {asset}")
            cache[digest] = url
            _save_krea_asset_cache(cache_path, cache)
            return url

    @staticmethod
    def _job_result(job_id: str, headers: dict[str, str], timeout_seconds: int = 300) -> dict:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            response = httpx.get(
                f"{_KREA_API_BASE}/jobs/{job_id}",
                headers=headers,
                timeout=30,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Krea job {response.status_code}: {response.text[:600]}"
                )
            data = response.json()
            status = str(data.get("status", "")).lower()
            if status == "completed":
                return data
            if status in {"failed", "canceled", "cancelled"}:
                raise RuntimeError(f"Krea job {status}: {data}")
            time.sleep(3)
        raise TimeoutError(f"Krea job timed out: {job_id}")


def _load_krea_asset_cache(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_krea_asset_cache(path: Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


class OfflineImageGenerator(ImageGenerator):
    """Deterministic placeholder still so storyboards render without a fal key."""

    name = "offline"

    def generate(
        self, prompt, out_path: Path, reference_images=None, reference_strengths=None
    ):
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


def _reference_strength(strengths: list[float] | None, index: int) -> float:
    """A safe Krea style-reference strength for identity/continuity hints.

    Krea's default is 1.0, which transfers composition language along with
    visual style. Ensembly references are not style targets, so callers pass a
    deliberately light per-image influence. The fallback stays conservative
    for older/custom callers that provide paths without strengths.
    """
    value = strengths[index] if strengths and index < len(strengths) else 0.2
    return max(0.0, min(float(value), 1.0))


def _reference_data_uri(paths: list[Path]) -> str:
    if len(paths) == 1:
        return _to_data_uri(paths[0])

    from PIL import Image, ImageDraw, ImageOps

    refs = []
    for path in paths[:10]:
        try:
            refs.append(Image.open(path).convert("RGB"))
        except OSError:
            continue
    if not refs:
        return _to_data_uri(paths[0])

    cols = min(5, len(refs))
    rows = (len(refs) + cols - 1) // cols
    cell_w, cell_h = 320, 420
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)

    for idx, image in enumerate(refs, start=1):
        x = ((idx - 1) % cols) * cell_w
        y = ((idx - 1) // cols) * cell_h
        thumb = ImageOps.fit(image, (cell_w, cell_h), method=Image.Resampling.LANCZOS)
        sheet.paste(thumb, (x, y))
        draw.rectangle((x + 10, y + 10, x + 58, y + 48), fill=(0, 0, 0))
        draw.text((x + 24, y + 17), str(idx), fill=(255, 255, 255))

    buf = BytesIO()
    sheet.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


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
