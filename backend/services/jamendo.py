"""Jamendo music search and project-level soundtrack downloads."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..models import MusicTrack, Project
from ..storage import project_folder, relative_to_root, slugify

API_BASE = "https://api.jamendo.com/v3.0"
TIMEOUT = 18.0


class JamendoNotConfigured(RuntimeError):
    pass


class MusicLicenseError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.jamendo_client_id)


def _require_client_id() -> str:
    if not settings.jamendo_client_id:
        raise JamendoNotConfigured("JAMENDO_CLIENT_ID is missing")
    return settings.jamendo_client_id


def _license_is_usable(license_url: str) -> bool:
    license_url = (license_url or "").lower()
    if not license_url:
        return False
    return "/by-nc" not in license_url and "/by-nd" not in license_url


def search_tracks(query: str, limit: int = 20, instrumental: bool = True) -> list[dict]:
    client_id = _require_client_id()
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": max(1, min(limit, 50)),
        "search": (query or "ambient").strip(),
        "include": "licenses musicinfo",
        "audioformat": "mp31",
        "audiodlformat": "mp32",
        "ccnc": "false",
        "ccnd": "false",
        "content_id_free": "true",
        "groupby": "artist_id",
    }
    if instrumental:
        params["vocalinstrumental"] = "instrumental"
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        response = client.get(f"{API_BASE}/tracks/", params=params)
        response.raise_for_status()
    payload = response.json()
    headers = payload.get("headers", {})
    if headers.get("status") == "failed":
        raise RuntimeError(headers.get("error_message") or "Jamendo API request failed")
    results = payload.get("results", [])
    return [_track_payload(track) for track in results if _license_is_usable(track.get("license_ccurl", ""))]


def _track_payload(track: dict) -> dict:
    return {
        "provider": "jamendo",
        "provider_track_id": str(track.get("id", "")),
        "title": track.get("name") or "Untitled",
        "artist_name": track.get("artist_name") or "",
        "album_name": track.get("album_name") or "",
        "duration_seconds": int(track.get("duration") or 0),
        "license_url": track.get("license_ccurl") or "",
        "audio_url": track.get("audio") or "",
        "download_url": track.get("audiodownload") or "",
        "download_allowed": bool(track.get("audiodownload_allowed", True)) and bool(track.get("audiodownload")),
        "image_url": track.get("image") or track.get("album_image") or "",
        "share_url": track.get("shareurl") or track.get("shorturl") or "",
    }


def upsert_track(existing: MusicTrack | None, data: dict) -> MusicTrack:
    track = existing or MusicTrack(
        provider="jamendo",
        provider_track_id=str(data["provider_track_id"]),
        title=data["title"],
    )
    track.title = data["title"]
    track.artist_name = data.get("artist_name", "")
    track.album_name = data.get("album_name", "")
    track.duration_seconds = int(data.get("duration_seconds") or 0)
    track.license_url = data.get("license_url", "")
    track.audio_url = data.get("audio_url", "")
    track.download_url = data.get("download_url", "")
    track.download_allowed = bool(data.get("download_allowed"))
    track.image_url = data.get("image_url", "")
    track.share_url = data.get("share_url", "")
    return track


def ensure_downloaded(project: Project, track: MusicTrack) -> Path:
    if not _license_is_usable(track.license_url):
        raise MusicLicenseError("Track license is not allowed for project soundtracks")
    if not track.download_allowed or not track.download_url:
        raise MusicLicenseError("Jamendo does not allow this track to be downloaded")

    folder = _project_folder(project)
    music_dir = folder / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    ext = _extension_from_url(track.download_url) or ".mp3"
    out = music_dir / f"jamendo_{track.provider_track_id}_{slugify(track.title)}{ext}"
    if out.exists() and out.stat().st_size > 0:
        track.local_path = relative_to_root(out)
        return out

    with httpx.stream("GET", track.download_url, timeout=TIMEOUT, follow_redirects=True) as response:
        response.raise_for_status()
        with out.open("wb") as file:
            for chunk in response.iter_bytes():
                if chunk:
                    file.write(chunk)
    track.local_path = relative_to_root(out)
    return out


def local_track_path(track: MusicTrack | None) -> Path | None:
    if not track or not track.local_path:
        return None
    path = (settings.projects_dir.parent.parent / track.local_path).resolve()
    data_root = (settings.projects_dir.parent.parent / "data").resolve()
    if data_root not in path.parents and path != data_root:
        return None
    return path if path.is_file() else None


def _project_folder(project: Project) -> Path:
    if project.folder_path:
        return settings.projects_dir.parent.parent / project.folder_path
    folder = project_folder(project.title)
    project.folder_path = relative_to_root(folder)
    return folder


def _extension_from_url(url: str) -> str:
    parsed = urlparse(url)
    match = re.search(r"\.(mp3|ogg|flac|wav)$", parsed.path, flags=re.IGNORECASE)
    if match:
        return f".{match.group(1).lower()}"
    return ".mp3"
