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


# --------------------------------------------------------------------------- #
# Burned-in subtitles
# --------------------------------------------------------------------------- #
# Vertical placement of the caption block, as a fraction of the frame height
# measured from the bottom edge (0 = frame bottom, 1 = frame top). This is a
# *constant*, not a DB-backed setting: a project's placement is stored on the
# project itself, so adjusting one project never moves the starting point for
# the next one — every new project opens at the default below.
SUBTITLE_POSITION_DEFAULT = 0.128  # 245px on a 1920px-tall frame
SUBTITLE_POSITION_MIN = 0.02
SUBTITLE_POSITION_MAX = 0.85


def clamp_subtitle_position(value: float | None) -> float:
    """Coerce any stored/submitted placement into the renderable range."""
    try:
        position = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return SUBTITLE_POSITION_DEFAULT
    return max(SUBTITLE_POSITION_MIN, min(position, SUBTITLE_POSITION_MAX))


class Settings(BaseModel):
    # --- Secrets (only source of API keys) ---
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "").strip()
    fal_api_key: str = os.getenv("FAL_API_KEY", "").strip()
    krea_api_key: str = os.getenv("KREA_API_KEY", "").strip()
    jamendo_client_id: str = (
        os.getenv("JAMENDO_CLIENT_ID")
        or os.getenv("JAMEDO_CLIENT_ID")
        or ""
    ).strip()
    jamendo_secret: str = (os.getenv("JAMENDO_SECRET") or os.getenv("JAMEDO_SECRET") or "").strip()

    # --- TikTok (Login Kit + Content Posting API) ---
    # One developer app authorizes many creator accounts; the per-account tokens
    # live in the database, so only the app credentials belong here.
    tiktok_client_key: str = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    tiktok_client_secret: str = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    # TikTok only accepts absolute https redirect URIs registered on the app —
    # http/localhost is rejected, so a local install needs a tunnel or the
    # paste-the-redirected-URL fallback in the linking UI.
    tiktok_redirect_uri: str = os.getenv("TIKTOK_REDIRECT_URI", "").strip()
    # PKCE is required for mobile/desktop client types and rejected by some web
    # client configurations, so it follows the app's registered type.
    tiktok_use_pkce: bool = _bool(os.getenv("TIKTOK_USE_PKCE"), False)

    # --- YouTube (Google OAuth + Data API v3) ---
    # One Google Cloud OAuth client authorizes many channels; the per-channel
    # tokens live in the database, so only the client credentials belong here.
    youtube_client_id: str = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    youtube_client_secret: str = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    # Unlike TikTok, Google exempts localhost from the https rule, so the default
    # loopback callback works for a local install with no tunnel.
    youtube_redirect_uri: str = os.getenv("YOUTUBE_REDIRECT_URI", "").strip()

    default_llm_provider: str = os.getenv("DEFAULT_LLM_PROVIDER", "anthropic").strip().lower()

    # --- App config ---
    app_port: int = int(os.getenv("APP_PORT", "8420"))
    projects_dir: Path = (ROOT_DIR / os.getenv("PROJECTS_DIR", "./data/projects")).resolve()
    characters_dir: Path = (ROOT_DIR / os.getenv("CHARACTERS_DIR", "./data/characters")).resolve()
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
    krea_asset_cache_file: Path = (
        ROOT_DIR / os.getenv("KREA_ASSET_CACHE_FILE", "./data/krea_asset_cache.json")
    ).resolve()

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
    # Narration longer than this many characters is synthesized in multiple
    # requests (ElevenLabs caps a single request at ~10,000 chars) and stitched
    # back into one audio file + one merged timeline. Sentences are packed up to
    # this soft target, so a whole animation run usually stays a single request.
    elevenlabs_max_chars_per_request: int = int(os.getenv("ELEVENLABS_MAX_CHARS", "5000"))
    # Deterministic animation engine for animation scenes: "manim" (default,
    # requires the optional manim package) or "offline" (Pillow placeholder).
    animation_engine: str = os.getenv("ANIMATION_ENGINE", "manim").strip().lower()

    @property
    def root_dir(self) -> Path:
        return ROOT_DIR

    @property
    def youtube_redirect(self) -> str:
        """The redirect URI to send Google, defaulting to the loopback callback.

        Google allows http on localhost, so a local install needs no tunnel —
        but whatever is used here must match a URI registered on the OAuth
        client *exactly*, which is why an explicit override stays available.
        """
        return self.youtube_redirect_uri or (
            f"http://localhost:{self.app_port}/api/youtube/link/callback"
        )

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
            "krea": bool(self.krea_api_key),
            "jamendo": bool(self.jamendo_client_id),
            "tiktok": bool(self.tiktok_client_key and self.tiktok_client_secret),
            "youtube": bool(self.youtube_client_id and self.youtube_client_secret),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
