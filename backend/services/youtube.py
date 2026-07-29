"""Google OAuth + YouTube Data API v3 client (upload / Shorts publishing).

Publishing flow (resumable upload):
  1. ``POST /upload/youtube/v3/videos?uploadType=resumable`` — sends the video
     metadata (snippet + status) and the file size, and returns a session URI in
     the ``Location`` header.
  2. ``PUT`` the file bytes to that session URI. An interrupted upload is probed
     with a ``Content-Range: bytes */SIZE`` probe and resumed from the byte Google
     reports, so a dropped connection never costs a second upload.
  3. ``videos.list`` — read ``status``/``processingDetails`` for the new id.

There is **no Shorts-specific endpoint**. YouTube classifies an upload as a
Short automatically from the file itself (vertical/square, short enough), so a
9:16 render from this pipeline needs no extra flag.

Three limits are Google's, not ours, and shape the UI:
  * Videos uploaded by an API project that has not passed YouTube's compliance
    audit are **locked to private**, whatever ``privacyStatus`` was requested.
    Uploading as private (optionally with ``publishAt``) and flipping it in
    YouTube Studio is the way to work without the audit — the counterpart of
    TikTok's draft/inbox path.
  * Uploads are metered in their own quota bucket: 100 ``videos.insert`` calls
    per day, separate from the 10,000-unit pool the other endpoints share.
  * While the OAuth consent screen is in "Testing", Google issues refresh tokens
    that expire after 7 days, so a long-lived install wants the app published.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ..config import settings

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# youtube.upload is the minimum videos.insert needs; youtube.readonly is only so
# the account card can show the channel's real name and avatar.
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)

PRIVACY_LEVELS = ("private", "unlisted", "public")

# YouTube's own field limits; exceeding either is a hard 400.
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS_CHARS = 500
# A Short is classified by the file, but this is the current ceiling for one.
MAX_SHORT_SECONDS = 180
MAX_VIDEO_BYTES = 256 * 1024 * 1024 * 1024

# Angle brackets are rejected in titles and descriptions.
_FORBIDDEN_TITLE_CHARS = ("<", ">")

# 8 MB keeps a resumed upload from replaying much, and is a multiple of the
# 256 KB chunk size Google requires for anything but the final chunk.
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024

TIMEOUT = httpx.Timeout(60.0, read=600.0)


class YouTubeError(RuntimeError):
    """A YouTube API call failed; the message is safe to surface in the UI."""


class YouTubeNotConfigured(YouTubeError):
    pass


class YouTubeNeedsRelink(YouTubeError):
    """The refresh token was rejected — only a new login can fix it."""


@dataclass
class AuthStart:
    url: str
    state: str


def is_configured() -> bool:
    return bool(settings.youtube_client_id and settings.youtube_client_secret)


def _require_config() -> None:
    if not is_configured():
        raise YouTubeNotConfigured(
            "YouTube is not configured. Set YOUTUBE_CLIENT_ID and "
            "YOUTUBE_CLIENT_SECRET in .env, then restart."
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def start_authorization(state_payload: str = "") -> AuthStart:
    """Build the consent URL a creator opens to grant this app access."""
    _require_config()
    state = (
        f"{secrets.token_urlsafe(24)}.{state_payload}"
        if state_payload
        else secrets.token_urlsafe(24)
    )
    params = {
        "client_id": settings.youtube_client_id,
        "redirect_uri": settings.youtube_redirect,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        # offline + consent is what makes Google return a refresh token. Without
        # prompt=consent it is issued only on the very first authorization, so a
        # re-link would silently come back without one.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return AuthStart(f"{AUTHORIZE_URL}?{urlencode(params)}", state)


def exchange_code(code: str) -> dict:
    """Trade an authorization code for the channel's tokens."""
    _require_config()
    return _token_request(
        {
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.youtube_redirect,
        }
    )


def refresh_tokens(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token.

    Google does not normally return a replacement refresh token here, so the
    caller keeps the one it has.
    """
    _require_config()
    return _token_request(
        {
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )


def revoke(token: str) -> None:
    """Best-effort revoke, so unlinking also drops Google's side of the grant."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            client.post(REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        pass


def _token_request(data: dict) -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    payload = _json(response)
    if response.status_code >= 400 or payload.get("error"):
        error = str(payload.get("error", ""))
        detail = (
            payload.get("error_description")
            or error
            or f"Token request failed ({response.status_code})"
        )
        # invalid_grant covers an expired (7-day Testing), revoked, or already
        # rotated refresh token — all only fixable by linking again.
        if error == "invalid_grant":
            raise YouTubeNeedsRelink(
                f"{detail}. Link the channel again "
                "(refresh tokens expire after 7 days while the OAuth app is in Testing)."
            )
        raise YouTubeError(detail)
    return payload


def token_expiry(payload: dict) -> tuple[datetime | None, datetime | None]:
    """Absolute expiry instants for the access and refresh tokens.

    Google states no refresh-token lifetime, so the second value is always None
    — the same shape as the TikTok helper, to keep the routers symmetrical.
    """
    access = payload.get("expires_in")
    return (_now() + timedelta(seconds=int(access)) if access else None, None)


def fetch_channel(access_token: str) -> dict:
    """The authorizing user's own channel (title/handle/avatar for the card)."""
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(
            f"{API_BASE}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    payload = _json(response)
    _raise_for_error(response, payload, "Could not read the YouTube channel")
    items = payload.get("items") or []
    if not items:
        raise YouTubeError(
            "This Google account has no YouTube channel. Create one at "
            "youtube.com, then link again."
        )
    item = items[0]
    snippet = item.get("snippet", {}) or {}
    thumbnails = snippet.get("thumbnails", {}) or {}
    return {
        "channel_id": item.get("id", ""),
        "title": snippet.get("title", ""),
        "handle": snippet.get("customUrl", ""),
        "avatar_url": (thumbnails.get("default") or {}).get("url", ""),
    }


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def build_snippet(
    *,
    title: str,
    description: str,
    tags: list[str] | None = None,
    category_id: str = "22",
) -> dict:
    """Coerce our metadata into what videos.insert accepts.

    YouTube rejects the whole request over a long title or an angle bracket, so
    the trimming happens here rather than being left to the caller.
    """
    clean_title = _strip_forbidden(title).strip() or "Untitled"
    return {
        "title": clean_title[:MAX_TITLE_CHARS],
        "description": _strip_forbidden(description)[:MAX_DESCRIPTION_CHARS],
        "tags": _fit_tags(tags or []),
        "categoryId": category_id,
    }


def _strip_forbidden(text: str) -> str:
    for char in _FORBIDDEN_TITLE_CHARS:
        text = text.replace(char, "")
    return text


def _fit_tags(tags: list[str]) -> list[str]:
    """Tags share a 500-character budget; drop the overflow rather than 400."""
    kept: list[str] = []
    used = 0
    for tag in tags:
        clean = tag.strip().lstrip("#").strip()
        if not clean:
            continue
        # Google counts the quotes it adds around any tag containing a space.
        cost = len(clean) + (2 if " " in clean else 0) + 1
        if used + cost > MAX_TAGS_CHARS:
            break
        kept.append(clean)
        used += cost
    return kept


def start_resumable_upload(
    access_token: str,
    *,
    video_bytes: int,
    snippet: dict,
    privacy_status: str = "private",
    publish_at: str | None = None,
    made_for_kids: bool = False,
    contains_synthetic_media: bool = True,
    notify_subscribers: bool = True,
) -> str:
    """Declare the video and get back the resumable session URI."""
    if privacy_status not in PRIVACY_LEVELS:
        raise YouTubeError(f"Unknown privacy status: {privacy_status}")
    if video_bytes <= 0:
        raise YouTubeError("The video file is empty.")
    if video_bytes > MAX_VIDEO_BYTES:
        raise YouTubeError("YouTube accepts videos up to 256 GB.")
    status: dict = {
        "privacyStatus": privacy_status,
        # Required for COPPA compliance; videos.insert rejects an omitted value
        # on some channels rather than defaulting it.
        "selfDeclaredMadeForKids": made_for_kids,
        # Everything this app renders is AI-generated, so the disclosure is
        # always on rather than left to the operator to remember.
        "containsSyntheticMedia": contains_synthetic_media,
    }
    if publish_at:
        # Scheduling is only honoured while the video is private.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            UPLOAD_URL,
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": str(notify_subscribers).lower(),
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(video_bytes),
                "X-Upload-Content-Type": "video/mp4",
            },
            content=json.dumps({"snippet": snippet, "status": status}),
        )
    if response.status_code >= 400:
        _raise_for_error(response, _json(response), "YouTube rejected the upload")
    session_uri = response.headers.get("Location") or response.headers.get("location")
    if not session_uri:
        raise YouTubeError("YouTube did not return an upload session.")
    return session_uri


def upload_video(session_uri: str, video_path: Path) -> dict:
    """PUT the file to the resumable session, resuming after an interruption.

    Returns the created video resource. Google answers the final chunk with the
    inserted video, which is where the video id comes from.
    """
    total = video_path.stat().st_size
    offset = 0
    with httpx.Client(timeout=TIMEOUT) as client, video_path.open("rb") as handle:
        while offset < total:
            handle.seek(offset)
            payload = handle.read(UPLOAD_CHUNK_BYTES)
            last = offset + len(payload) - 1
            try:
                response = client.put(
                    session_uri,
                    content=payload,
                    headers={
                        "Content-Length": str(len(payload)),
                        "Content-Range": f"bytes {offset}-{last}/{total}",
                    },
                )
            except httpx.HTTPError as exc:
                resumed = _resume_offset(client, session_uri, total)
                if resumed is None or resumed <= offset:
                    raise YouTubeError(f"Upload failed: {exc}") from exc
                offset = resumed
                continue

            if response.status_code in (200, 201):
                return _json(response)
            if response.status_code == 308:
                # "Resume Incomplete" — Google reports how far it actually got,
                # which may be short of what we just sent.
                offset = _range_end(response.headers.get("Range")) or last + 1
                continue
            if response.status_code in (500, 502, 503, 504):
                resumed = _resume_offset(client, session_uri, total)
                if resumed is None:
                    raise YouTubeError(
                        f"Upload failed ({response.status_code}) and could not be resumed."
                    )
                offset = resumed
                continue
            _raise_for_error(response, _json(response), "Upload failed")

    raise YouTubeError("The upload finished without YouTube confirming the video.")


def _resume_offset(client: httpx.Client, session_uri: str, total: int) -> int | None:
    """Ask Google how many bytes it has, per the resumable protocol."""
    try:
        response = client.put(
            session_uri,
            content=b"",
            headers={"Content-Range": f"bytes */{total}", "Content-Length": "0"},
        )
    except httpx.HTTPError:
        return None
    if response.status_code in (200, 201):
        return total
    if response.status_code != 308:
        return None
    return _range_end(response.headers.get("Range")) or 0


def _range_end(header: str | None) -> int | None:
    """Byte offset to continue from, out of a ``Range: bytes=0-N`` header."""
    if not header or "-" not in header:
        return None
    try:
        return int(header.rsplit("-", 1)[1]) + 1
    except ValueError:
        return None


def video_status(access_token: str, video_id: str) -> dict:
    """Upload/processing state of a video we just inserted."""
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(
            f"{API_BASE}/videos",
            params={"part": "status,processingDetails,snippet", "id": video_id},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    payload = _json(response)
    _raise_for_error(response, payload, "Could not read the video status")
    items = payload.get("items") or []
    if not items:
        return {"id": video_id, "found": False}
    item = items[0]
    status = item.get("status", {}) or {}
    return {
        "id": video_id,
        "found": True,
        "upload_status": status.get("uploadStatus", ""),
        "privacy_status": status.get("privacyStatus", ""),
        "failure_reason": status.get("failureReason", ""),
        "rejection_reason": status.get("rejectionReason", ""),
        "publish_at": status.get("publishAt", ""),
        "processing_status": (item.get("processingDetails", {}) or {}).get(
            "processingStatus", ""
        ),
        "title": (item.get("snippet", {}) or {}).get("title", ""),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def set_thumbnail(access_token: str, video_id: str, image_path: Path) -> None:
    """Upload a custom thumbnail (needs a phone-verified channel)."""
    with httpx.Client(timeout=TIMEOUT) as client, image_path.open("rb") as handle:
        response = client.post(
            "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
            params={"videoId": video_id, "uploadType": "media"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "image/png",
            },
            content=handle.read(),
        )
    if response.status_code >= 400:
        _raise_for_error(response, _json(response), "Could not set the thumbnail")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _raise_for_error(response: httpx.Response, payload: dict, context: str) -> None:
    if response.status_code < 400 and not payload.get("error"):
        return
    error = payload.get("error") or {}
    if isinstance(error, str):  # OAuth-style error body
        detail = payload.get("error_description") or error
    else:
        errors = error.get("errors") or []
        reason = errors[0].get("reason", "") if errors else ""
        detail = error.get("message") or response.text[:200] or f"HTTP {response.status_code}"
        if reason == "quotaExceeded" or reason == "uploadLimitExceeded":
            detail = (
                f"{detail} (YouTube allows 100 uploads per day per API project; "
                "the limit resets at midnight Pacific time.)"
            )
        elif reason == "youtubeSignupRequired":
            detail = "That Google account has no YouTube channel."
        elif reason:
            detail = f"{detail} [{reason}]"
    if response.status_code == 401:
        raise YouTubeNeedsRelink(f"{context}: {detail}")
    raise YouTubeError(f"{context}: {detail}")
