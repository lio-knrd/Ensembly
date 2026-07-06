"""Active-adapter selection (config/constants concern, not a DB setting).

One default implementation per stage. When the relevant API key is missing and
ALLOW_OFFLINE_FALLBACK is on, the offline placeholder implementation is used so
the whole pipeline stays runnable end-to-end without any keys.
"""
from __future__ import annotations

from ..config import settings
from .base import ImageGenerator, ScriptGenerator, TTSGenerator, VideoGenerator
from .image import FalImageGenerator, OfflineImageGenerator
from .llm import AnthropicScriptGenerator, OfflineScriptGenerator, OpenAIScriptGenerator
from .tts import ElevenLabsTTSGenerator, OfflineTTSGenerator
from .video import FalVideoGenerator, OfflineVideoGenerator


def _offline_ok() -> bool:
    return settings.allow_offline_fallback


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
