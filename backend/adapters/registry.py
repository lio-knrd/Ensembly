"""Active-adapter selection (config/constants concern, not a DB setting).

One default implementation per stage. When the relevant API key is missing and
ALLOW_OFFLINE_FALLBACK is on, the offline placeholder implementation is used so
the whole pipeline stays runnable end-to-end without any keys.
"""
from __future__ import annotations

import json

from sqlmodel import Session

from ..config import settings
from ..database import engine
from ..models import Setting
from .base import ImageGenerator, ScriptGenerator, TTSGenerator, VideoGenerator
from .image import FalImageGenerator, KreaImageGenerator, OfflineImageGenerator
from .llm import AnthropicScriptGenerator, OfflineScriptGenerator, OpenAIScriptGenerator
from .tts import ElevenLabsTTSGenerator, OfflineTTSGenerator
from .video import FalVideoGenerator, OfflineVideoGenerator


def _offline_ok() -> bool:
    return settings.allow_offline_fallback


IMAGE_MODEL_OPTIONS = [
    {
        "id": settings.fal_image_model,
        "label": "FLUX.1 dev",
        "description": "General-purpose high-fidelity image generation.",
    },
    {
        "id": settings.fal_krea_image_model,
        "label": "Krea 2 Medium",
        "description": "More aesthetic/stylized; good first test for anime, illustration, and manhwa-like looks.",
    },
]


def _setting(key: str, default):
    with Session(engine) as session:
        row = session.get(Setting, key)
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return row.value


def active_image_model() -> str:
    model = _setting("active_image_model", settings.fal_image_model)
    allowed = {option["id"] for option in IMAGE_MODEL_OPTIONS}
    return model if model in allowed else settings.fal_image_model


def get_script_generator() -> ScriptGenerator:
    provider = settings.default_llm_provider
    if provider == "openai" and settings.openai_api_key:
        return OpenAIScriptGenerator()
    if provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicScriptGenerator()
    # Fall through to whichever key exists, else offline.
    if settings.anthropic_api_key:
        return AnthropicScriptGenerator()
    if settings.openai_api_key:
        return OpenAIScriptGenerator()
    if _offline_ok():
        return OfflineScriptGenerator()
    raise RuntimeError("No LLM API key configured and offline fallback disabled.")


def get_tts_generator() -> TTSGenerator:
    if settings.elevenlabs_api_key:
        return ElevenLabsTTSGenerator()
    if _offline_ok():
        return OfflineTTSGenerator()
    raise RuntimeError("No ElevenLabs API key configured and offline fallback disabled.")


def get_image_generator() -> ImageGenerator:
    if settings.fal_api_key:
        model = active_image_model()
        if model == settings.fal_krea_image_model:
            return KreaImageGenerator()
        return FalImageGenerator()
    if _offline_ok():
        return OfflineImageGenerator()
    raise RuntimeError("No fal.ai API key configured and offline fallback disabled.")


def get_video_generator() -> VideoGenerator:
    if settings.fal_api_key:
        return FalVideoGenerator()
    if _offline_ok():
        return OfflineVideoGenerator()
    raise RuntimeError("No fal.ai API key configured and offline fallback disabled.")
