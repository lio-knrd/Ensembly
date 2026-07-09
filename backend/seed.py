"""Seed default presets and settings on first run.

Ships one default of each preset type: platform = "TikTok",
content = "Greek Mythology" (spec section 5).
"""
from __future__ import annotations

from sqlmodel import Session, select

from .config import settings
from .database import engine
from .models import ContentPreset, PlatformPreset, Setting
from .services.music_library import backfill_legacy_defaults, seed_local_library

DEFAULT_PLATFORM = PlatformPreset(
    name="TikTok",
    is_default=True,
    format_prompt=(
        "Target length ~60-90 seconds. Open with a strong hook in the first 1-2 "
        "seconds that stops the scroll — a bold question, a surprising claim, or a "
        "vivid image. Keep energy high and sentences short. End with either a "
        "satisfying payoff or a cliffhanger that invites a follow. "
        "Required social metadata: a punchy title (under 100 chars), a short "
        "description, and 5-10 relevant, discoverable hashtags suited to TikTok."
    ),
)

DEFAULT_CONTENT = ContentPreset(
    name="Greek Mythology",
    is_default=True,
    content_prompt=(
        "Retell classic Greek myths faithfully to the classical sources (Homer, "
        "Hesiod, Ovid, the tragedians). Use an engaging, slightly dramatic "
        "narrator voice that treats the gods and heroes as vivid, larger-than-life "
        "figures. Keep names, relationships, and the sequence of events accurate. "
        "Explain unfamiliar names briefly in-line. Favor wonder and stakes over "
        "dry exposition."
    ),
    # Applied to every generated image (character sheets + scenes) for a cohesive
    # look — kept out of the script prompt.
    image_style_prompt=(
        "Cinematic classical oil-painting style: warm dramatic lighting, painterly "
        "brushwork, epic mythological atmosphere, rich but muted color palette, "
        "consistent across all scenes. Vertical 9:16 composition."
    ),
    voice_id=settings.elevenlabs_voice_id,
)

DEFAULT_SETTINGS: dict[str, object] = {
    "default_duration_seconds": 75,
    "default_platform_preset_name": "TikTok",
    "default_content_preset_name": "Greek Mythology",
    "active_image_model": "fal-ai/flux/dev",
}


def seed() -> None:
    import json

    with Session(engine) as session:
        seed_local_library(session)
        session.flush()
        backfill_legacy_defaults(session)
        if not session.exec(select(PlatformPreset)).first():
            session.add(DEFAULT_PLATFORM)
        if not session.exec(select(ContentPreset)).first():
            session.add(DEFAULT_CONTENT)
        for key, value in DEFAULT_SETTINGS.items():
            existing = session.get(Setting, key)
            if existing is None:
                session.add(Setting(key=key, value=json.dumps(value)))
        session.commit()
