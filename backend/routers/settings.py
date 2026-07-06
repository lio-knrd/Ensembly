"""Settings + API-key status (masked)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..config import settings as app_settings
from ..database import get_session
from ..schemas import SettingsUpdate
from ..services.ffmpeg import has_ffmpeg
from .common import get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def read_settings(session: Session = Depends(get_session)):
    return {
        "default_duration_seconds": get_setting(session, "default_duration_seconds", 75),
        "default_platform_preset_name": get_setting(session, "default_platform_preset_name", "TikTok"),
        "default_content_preset_name": get_setting(session, "default_content_preset_name", "Greek Mythology"),
        # API key status only — never the raw values.
        "api_keys": app_settings.key_status(),
        "active_llm_provider": app_settings.default_llm_provider,
        "offline_fallback": app_settings.allow_offline_fallback,
        "ffmpeg_available": has_ffmpeg(),
        "models": {
            "script": app_settings.anthropic_script_model
            if app_settings.default_llm_provider == "anthropic"
            else app_settings.openai_script_model,
            "image": app_settings.fal_image_model,
            "video": app_settings.fal_video_model,
            "tts": f"{app_settings.elevenlabs_model} ({app_settings.elevenlabs_voice_id})",
        },
    }


@router.patch("")
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session)):
    for key, value in body.model_dump(exclude_unset=True).items():
        set_setting(session, key, value)
    session.commit()
    return read_settings(session)
