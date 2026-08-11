"""TikTok Login Kit (OAuth) + Content Posting API client.

Publishing flow (Direct Post):
  1. ``creator_info/query`` — the creator's nickname, allowed privacy levels and
     limits. TikTok requires these to be shown before a post is confirmed.
  2. ``video/init`` — declares the post options and the upload plan, and returns
     a ``publish_id`` plus a one-hour ``upload_url``.
  3. ``PUT`` the file to ``upload_url`` in sequential byte-range chunks.
  4. ``status/fetch`` — poll ``publish_id`` until the post is published/failed.

Two limits are TikTok's, not ours, and shape the UI:
  * Until the developer app passes TikTok's audit, everything Direct Post
    publishes is forced to private (SELF_ONLY) no matter which privacy level was
    requested. ``init_draft_upload`` is the way around that: it hands the video
    to the creator's TikTok inbox, and they post it publicly themselves.
  * The redirect URI must be an absolute https URL registered on the app, so a
    local install links accounts through a tunnel or the paste-URL fallback.
"""
from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ..config import settings

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API_BASE = "https://open.tiktokapis.com/v2"

# user.info.basic supplies the display name/avatar shown on the account cards;
# video.publish is what Direct Post requires.
SCOPES = "user.info.basic,video.publish,video.upload"

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
MAX_VIDEO_BYTES = 4 * 1024 * MB
MAX_CHUNKS = 1000

# TikTok's single caption field ("title" in the Direct Post payload).
MAX_CAPTION_CHARS = 2200

PRIVACY_LEVELS = (
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
)

TIMEOUT = httpx.Timeout(60.0, read=300.0)


class TikTokError(RuntimeError):
    """A TikTok API call failed; the message is safe to surface in the UI."""


class TikTokNotConfigured(TikTokError):
    pass


@dataclass
class AuthStart:
    url: str
    state: str
    code_verifier: str


def is_configured() -> bool:
    return bool(settings.tiktok_client_key and settings.tiktok_client_secret)


def _require_config() -> None:
    if not is_configured():
        raise TikTokNotConfigured(
            "TikTok is not configured. Set TIKTOK_CLIENT_KEY and "
            "TIKTOK_CLIENT_SECRET in .env, then restart."
        )
    if not settings.tiktok_redirect_uri:
        raise TikTokNotConfigured(
            "Set TIKTOK_REDIRECT_URI in .env to the https redirect URI "
            "registered on your TikTok app."
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def start_authorization(state_payload: str = "") -> AuthStart:
    """Build the authorize URL a creator opens to grant this app access."""
    _require_config()
    state = f"{secrets.token_urlsafe(24)}.{state_payload}" if state_payload else secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)[:128]
    params = {
        "client_key": settings.tiktok_client_key,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": state,
    }
    if settings.tiktok_use_pkce:
        params["code_challenge"] = pkce_challenge(verifier)
        params["code_challenge_method"] = "S256"
    return AuthStart(f"{AUTHORIZE_URL}?{urlencode(params)}", state, verifier)


def exchange_code(code: str, code_verifier: str = "") -> dict:
    """Trade an authorization code for the creator's tokens."""
    _require_config()
    data = {
        "client_key": settings.tiktok_client_key,
        "client_secret": settings.tiktok_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.tiktok_redirect_uri,
    }
    if settings.tiktok_use_pkce and code_verifier:
        data["code_verifier"] = code_verifier
    return _token_request(data)


def refresh_tokens(refresh_token: str) -> dict:
    _require_config()
    return _token_request(
        {
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )


def _token_request(data: dict) -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    payload = _json(response)
    if response.status_code >= 400 or payload.get("error"):
        raise TikTokError(
            payload.get("error_description")
            or payload.get("error")
            or f"Token request failed ({response.status_code})"
        )
    return payload


def token_expiry(payload: dict) -> tuple[datetime | None, datetime | None]:
    """Absolute expiry instants for the access and refresh tokens."""
    access = payload.get("expires_in")
    refresh = payload.get("refresh_expires_in")
    return (
        _now() + timedelta(seconds=int(access)) if access else None,
        _now() + timedelta(seconds=int(refresh)) if refresh else None,
    )


def fetch_user_info(access_token: str) -> dict:
    """Display name/avatar for the account card (user.info.basic)."""
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(
            f"{API_BASE}/user/info/",
            params={"fields": "open_id,union_id,display_name,avatar_url"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    payload = _json(response)
    _raise_for_error(response, payload, "Could not read the TikTok profile")
    return payload.get("data", {}).get("user", {}) or {}


# --------------------------------------------------------------------------- #
# Content Posting
# --------------------------------------------------------------------------- #
def creator_info(access_token: str) -> dict:
    """Privacy options, limits and nickname TikTok requires us to show."""
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            f"{API_BASE}/post/publish/creator_info/query/",
            headers=_headers(access_token),
        )
    payload = _json(response)
    _raise_for_error(response, payload, "Could not read the creator's posting options")
    return payload.get("data", {}) or {}


def chunk_plan(video_bytes: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) honouring TikTok's chunking rules.

    Files under 5 MB go up whole; otherwise chunks stay within 5-64 MB and the
    final chunk carries the remainder, so ``total_chunk_count`` floors.
    """
    if video_bytes <= 0:
        raise TikTokError("The video file is empty.")
    if video_bytes > MAX_VIDEO_BYTES:
        raise TikTokError("TikTok accepts videos up to 4 GB.")
    if video_bytes < MIN_CHUNK:
        return video_bytes, 1
    chunk_size = MIN_CHUNK * 2  # 10 MB keeps most renders to a few chunks
    if math.floor(video_bytes / chunk_size) > MAX_CHUNKS:
        chunk_size = min(MAX_CHUNK, math.ceil(video_bytes / MAX_CHUNKS))
    return chunk_size, max(1, math.floor(video_bytes / chunk_size))


def init_direct_post(
    access_token: str,
    *,
    video_bytes: int,
    title: str,
    privacy_level: str,
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
    is_aigc: bool = True,
    video_cover_timestamp_ms: int | None = None,
) -> dict:
    """Declare the post and get back a publish_id + upload_url."""
    if privacy_level not in PRIVACY_LEVELS:
        raise TikTokError(f"Unknown privacy level: {privacy_level}")
    chunk_size, total_chunks = chunk_plan(video_bytes)
    body = {
        "post_info": {
            "title": title[:MAX_CAPTION_CHARS],
            "privacy_level": privacy_level,
            "disable_comment": disable_comment,
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
            # Everything this app renders is AI-generated, so the disclosure is
            # always on rather than left to the operator to remember.
            "is_aigc": is_aigc,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_bytes,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }
    if video_cover_timestamp_ms is not None:
        body["post_info"]["video_cover_timestamp_ms"] = video_cover_timestamp_ms
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            f"{API_BASE}/post/publish/video/init/",
            headers=_headers(access_token),
            json=body,
        )
    payload = _json(response)
    _raise_for_error(response, payload, "TikTok rejected the post")
    data = payload.get("data", {}) or {}
    if not data.get("publish_id") or not data.get("upload_url"):
        raise TikTokError("TikTok did not return an upload target.")
    return {**data, "chunk_size": chunk_size, "total_chunk_count": total_chunks}


def init_draft_upload(access_token: str, *, video_bytes: int) -> dict:
    """Send the video to the creator's TikTok inbox as a draft.

    This is the path that works *without* TikTok's audit: nothing is posted by
    the API, so nothing is forced private. The creator opens the TikTok inbox
    notification and publishes it themselves, at whatever visibility they want.
    TikTok allows at most 5 pending drafts per creator per 24 hours.
    """
    chunk_size, total_chunks = chunk_plan(video_bytes)
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            f"{API_BASE}/post/publish/inbox/video/init/",
            headers=_headers(access_token),
            json={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_bytes,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                }
            },
        )
    payload = _json(response)
    _raise_for_error(response, payload, "TikTok rejected the draft upload")
    data = payload.get("data", {}) or {}
    if not data.get("publish_id") or not data.get("upload_url"):
        raise TikTokError("TikTok did not return an upload target.")
    return {**data, "chunk_size": chunk_size, "total_chunk_count": total_chunks}


def upload_video(upload_url: str, video_path: Path, chunk_size: int, total_chunks: int) -> None:
    """PUT the file to TikTok in sequential byte ranges."""
    total = video_path.stat().st_size
    with httpx.Client(timeout=TIMEOUT) as client, video_path.open("rb") as handle:
        for index in range(total_chunks):
            first = index * chunk_size
            # The last chunk takes every remaining byte, which may exceed
            # chunk_size — that is what TikTok expects.
            last = total - 1 if index == total_chunks - 1 else first + chunk_size - 1
            handle.seek(first)
            payload = handle.read(last - first + 1)
            response = client.put(
                upload_url,
                content=payload,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(payload)),
                    "Content-Range": f"bytes {first}-{last}/{total}",
                },
            )
            if response.status_code >= 400:
                raise TikTokError(
                    f"Upload failed on chunk {index + 1}/{total_chunks} "
                    f"({response.status_code}): {response.text[:200]}"
                )


def publish_status(access_token: str, publish_id: str) -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            f"{API_BASE}/post/publish/status/fetch/",
            headers=_headers(access_token),
            json={"publish_id": publish_id},
        )
    payload = _json(response)
    _raise_for_error(response, payload, "Could not read the publish status")
    return payload.get("data", {}) or {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}


def _raise_for_error(response: httpx.Response, payload: dict, context: str) -> None:
    error = payload.get("error") or {}
    code = str(error.get("code", "")).lower()
    if response.status_code < 400 and code in ("", "ok"):
        return
    detail = error.get("message") or response.text[:200] or f"HTTP {response.status_code}"
    raise TikTokError(f"{context}: {detail}")


def pkce_challenge(verifier: str) -> str:
    """S256 challenge.

    TikTok departs from RFC 7636 here: the challenge is the *hex* digest of the
    verifier, not the base64url one.
    """
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()
