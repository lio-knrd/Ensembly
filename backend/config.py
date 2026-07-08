"""Environment loading + per-stage model configuration.

Secrets live only in the `.env` file (loaded here). Which concrete adapter
implementation is active for each pipeline stage is a *constants* concern —
see `ACTIVE_ADAPTERS` below — NOT a DB-backed setting (per spec section 4a).
Swapping in a new model later is a one-line change here plus a new adapter
class, with no pipeline restructuring.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

# Load .env from the project root (parent of the backend package).
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    # --- Secrets (only source of API keys) ---
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "").strip()
    fal_api_key: str = os.getenv("FAL_API_KEY", "").strip()
    jamendo_client_id: str = (
        os.getenv("JAMENDO_CLIENT_ID")
        or os.getenv("JAMEDO_CLIENT_ID")
        or ""
    ).strip()
    jamendo_secret: str = (os.getenv("JAMENDO_SECRET") or os.getenv("JAMEDO_SECRET") or "").strip()

    default_llm_provider: str = os.getenv("DEFAULT_LLM_PROVIDER", "anthropic").strip().lower()

    # --- App config ---
    app_port: int = int(os.getenv("APP_PORT", "8420"))
    projects_dir: Path = (ROOT_DIR / os.getenv("PROJECTS_DIR", "./data/projects")).resolve()
    characters_dir: Path = (ROOT_DIR / os.getenv("CHARACTERS_DIR", "./data/characters")).resolve()
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")

    allow_offline_fallback: bool = _bool(os.getenv("ALLOW_OFFLINE_FALLBACK"), True)

    # ------------------------------------------------------------------ #
    # Per-stage active model constants (one default model per stage).
    # These pick which concrete adapter class runs — see backend/adapters.
    # ------------------------------------------------------------------ #
    # Script generation
    anthropic_script_model: str = "claude-opus-4-8"
    openai_script_model: str = "gpt-4o"
    # Image generation (fal.ai model slug)
    fal_image_model: str = "fal-ai/flux/dev"
    fal_krea_image_model: str = "krea/v2/medium/text-to-image"
    # Image-to-video generation (fal.ai model slug)
    fal_video_model: str = "fal-ai/kling-video/v3/standard/image-to-video"
    # TTS (ElevenLabs voice + model)
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    elevenlabs_model: str = "eleven_multilingual_v2"

    @property
    def db_file(self) -> Path:
        """Filesystem path of the SQLite db (derived from database_url)."""
        url = self.database_url
        if url.startswith("sqlite:///"):
            rel = url.replace("sqlite:///", "", 1)
            return (ROOT_DIR / rel).resolve()
        return (ROOT_DIR / "data" / "app.db").resolve()

    def ensure_dirs(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.characters_dir.mkdir(parents=True, exist_ok=True)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)

    def key_status(self) -> dict[str, bool]:
        """Which provider keys are present (never exposes the raw values)."""
        return {
            "anthropic": bool(self.anthropic_api_key),
            "openai": bool(self.openai_api_key),
            "elevenlabs": bool(self.elevenlabs_api_key),
            "fal": bool(self.fal_api_key),
            "jamendo": bool(self.jamendo_client_id),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
