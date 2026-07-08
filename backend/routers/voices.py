"""TTS voice options from ElevenLabs.

Previews are ElevenLabs-provided preview_url values. We never synthesize sample
audio here, so browsing voices does not spend TTS characters.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from ..config import settings

router = APIRouter(prefix="/api/voices", tags=["voices"])

_CACHE_TTL_SECONDS = 10 * 60
_voices_cache: tuple[float, list[dict[str, Any]]] | None = None


@router.get("")
def list_voices(refresh: bool = Query(False)):
    if not settings.elevenlabs_api_key:
        return [_configured_fallback()]

    global _voices_cache
    now = time.monotonic()
    if (
        not refresh
        and _voices_cache is not None
        and now - _voices_cache[0] < _CACHE_TTL_SECONDS
    ):
        return _voices_cache[1]

    try:
        voices = _fetch_available_voices()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        raise HTTPException(exc.response.status_code, f"ElevenLabs voices failed: {detail}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ElevenLabs voices failed: {exc}")

    if not any(voice["id"] == settings.elevenlabs_voice_id for voice in voices):
        voices.insert(0, _configured_fallback())

    _voices_cache = (now, voices)
    return voices


def _fetch_available_voices() -> list[dict[str, Any]]:
    voices: list[dict[str, Any]] = []
    seen: set[str] = set()
    next_page_token: str | None = None
    headers = {"xi-api-key": settings.elevenlabs_api_key}

    with httpx.Client(timeout=30) as client:
        while True:
            params: dict[str, Any] = {
                "page_size": 100,
                "include_total_count": False,
                "sort": "name",
                "sort_direction": "asc",
            }
            if next_page_token:
                params["next_page_token"] = next_page_token

            resp = client.get(
                "https://api.elevenlabs.io/v2/voices",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            for raw in data.get("voices", []):
                normalized = _normalize_voice(raw)
                if normalized["id"] in seen:
                    continue
                seen.add(normalized["id"])
                voices.append(normalized)

            if not data.get("has_more"):
                break
            next_page_token = data.get("next_page_token")
            if not next_page_token:
                break

    return sorted(voices, key=lambda voice: (voice["source"], voice["label"].lower()))


def _normalize_voice(raw: dict[str, Any]) -> dict[str, Any]:
    labels = raw.get("labels") or {}
    label_bits = [
        raw.get("category"),
        labels.get("gender"),
        labels.get("accent"),
        labels.get("age"),
    ]
    description = raw.get("description") or " / ".join(str(bit) for bit in label_bits if bit)
    preview_url = raw.get("preview_url") or _verified_language_preview(raw)
    category = raw.get("category") or "voice"
    return {
        "id": raw["voice_id"],
        "label": raw.get("name") or raw["voice_id"],
        "description": description,
        "preview_url": preview_url,
        "source": category,
        "labels": labels,
    }


def _verified_language_preview(raw: dict[str, Any]) -> str:
    for lang in raw.get("verified_languages") or []:
        preview = lang.get("preview_url")
        if preview:
            return preview
    return ""


def _configured_fallback() -> dict[str, Any]:
    voice_id = settings.elevenlabs_voice_id
    return {
        "id": voice_id,
        "label": "Configured default",
        "description": "Voice ID from ELEVENLABS_VOICE_ID.",
        "preview_url": "",
        "source": "configured",
        "labels": {},
    }
