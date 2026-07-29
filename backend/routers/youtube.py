"""YouTube channel linking + publishing.

The shape mirrors ``routers/tiktok.py``: channels are app-level, you log in once
per channel, and any group (content preset) then *selects* an existing channel
instead of logging in again. Unlinking clears it from every group that pointed
at it.

The one structural difference is the callback. Google allows http on localhost,
so the redirect lands back on this app directly and the link completes without
the creator pasting anything.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..models import ContentPreset, YouTubeAccount
from ..schemas import YouTubeAccountSelect, YouTubeLinkComplete, YouTubeLinkStart
from ..services import youtube

router = APIRouter(prefix="/api/youtube", tags=["youtube"])

# Pending authorizations, keyed by the OAuth state. In-memory on purpose: a
# restart mid-login just means starting the login again.
_PENDING: dict[str, dict] = {}
_PENDING_TTL_SECONDS = 900
# Refresh a little before expiry so a publish never starts on a dying token.
_REFRESH_MARGIN = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prune_pending() -> None:
    cutoff = time.time() - _PENDING_TTL_SECONDS
    for state in [s for s, entry in _PENDING.items() if entry["created_at"] < cutoff]:
        _PENDING.pop(state, None)


def _as_aware(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; compare them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _groups_using(session: Session, account_id: str) -> list[dict]:
    presets = session.exec(
        select(ContentPreset).where(ContentPreset.youtube_account_id == account_id)
    ).all()
    return [{"id": p.id, "name": p.name} for p in presets]


def _serialize(session: Session, account: YouTubeAccount) -> dict:
    access_expires = _as_aware(account.access_expires_at)
    return {
        "id": account.id,
        "channel_id": account.channel_id,
        "title": account.title,
        "handle": account.handle,
        "avatar_url": account.avatar_url,
        "scopes": account.scopes,
        "access_expires_at": access_expires.isoformat() if access_expires else None,
        # Google publishes no refresh-token lifetime, so this is only known once
        # a refresh has actually been rejected.
        "needs_relink": account.needs_relink,
        "last_error": account.last_error,
        "groups": _groups_using(session, account.id),
    }


def _get_account(session: Session, account_id: str) -> YouTubeAccount:
    account = session.get(YouTubeAccount, account_id)
    if not account:
        raise HTTPException(404, "YouTube account not found")
    return account


def ensure_access_token(session: Session, account: YouTubeAccount) -> str:
    """A valid access token for this account, refreshing it when due."""
    expires = _as_aware(account.access_expires_at)
    if account.access_token and expires and expires - _REFRESH_MARGIN > _now():
        return account.access_token
    if not account.refresh_token:
        raise HTTPException(400, "This YouTube channel needs to be linked again.")
    try:
        payload = youtube.refresh_tokens(account.refresh_token)
    except youtube.YouTubeNeedsRelink as exc:
        account.needs_relink = True
        account.last_error = str(exc)
        session.add(account)
        session.commit()
        raise HTTPException(400, str(exc))
    except youtube.YouTubeError as exc:
        account.last_error = str(exc)
        session.add(account)
        session.commit()
        raise HTTPException(400, f"{exc} Re-link the channel to continue.")
    _apply_tokens(account, payload)
    account.needs_relink = False
    account.last_error = ""
    session.add(account)
    session.commit()
    session.refresh(account)
    return account.access_token


def _apply_tokens(account: YouTubeAccount, payload: dict) -> None:
    access_expires, refresh_expires = youtube.token_expiry(payload)
    account.access_token = payload.get("access_token", "") or account.access_token
    # A refresh response normally carries no refresh_token; keep the stored one.
    account.refresh_token = payload.get("refresh_token", "") or account.refresh_token
    account.access_expires_at = access_expires
    account.refresh_expires_at = refresh_expires or account.refresh_expires_at
    account.scopes = payload.get("scope", "") or account.scopes
    account.updated_at = _now()


# --------------------------------------------------------------------------- #
# Status + accounts
# --------------------------------------------------------------------------- #
@router.get("/status")
def status(session: Session = Depends(get_session)):
    accounts = session.exec(select(YouTubeAccount).order_by(YouTubeAccount.title)).all()
    return {
        "configured": youtube.is_configured(),
        "redirect_uri": settings.youtube_redirect,
        "scopes": youtube.SCOPES,
        "privacy_levels": list(youtube.PRIVACY_LEVELS),
        "accounts": [_serialize(session, a) for a in accounts],
    }


@router.get("/accounts")
def list_accounts(session: Session = Depends(get_session)):
    accounts = session.exec(select(YouTubeAccount).order_by(YouTubeAccount.title)).all()
    return [_serialize(session, a) for a in accounts]


@router.delete("/accounts/{account_id}", status_code=204)
def unlink_account(account_id: str, session: Session = Depends(get_session)):
    account = _get_account(session, account_id)
    for preset in session.exec(
        select(ContentPreset).where(ContentPreset.youtube_account_id == account_id)
    ):
        preset.youtube_account_id = None
        session.add(preset)
    if account.refresh_token:
        youtube.revoke(account.refresh_token)
    session.delete(account)
    session.commit()


@router.post("/accounts/{account_id}/refresh")
def refresh_account(account_id: str, session: Session = Depends(get_session)):
    account = _get_account(session, account_id)
    token = ensure_access_token(session, account)
    try:
        profile = youtube.fetch_channel(token)
    except youtube.YouTubeError as exc:
        raise HTTPException(502, str(exc))
    _apply_profile(account, profile)
    session.add(account)
    session.commit()
    session.refresh(account)
    return _serialize(session, account)


# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #
@router.post("/link/start")
def link_start(body: YouTubeLinkStart, session: Session = Depends(get_session)):
    """Begin an authorization; the creator opens the returned URL."""
    if body.content_preset_id and not session.get(ContentPreset, body.content_preset_id):
        raise HTTPException(404, "Group not found")
    try:
        start = youtube.start_authorization()
    except youtube.YouTubeError as exc:
        raise HTTPException(400, str(exc))
    _prune_pending()
    _PENDING[start.state] = {
        "content_preset_id": body.content_preset_id,
        "created_at": time.time(),
    }
    return {"url": start.url, "state": start.state}


@router.get("/link/callback")
def link_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
):
    """Where Google redirects after consent (localhost is allowed here)."""
    if error:
        return RedirectResponse(f"/settings?youtube_error={quote(error)}")
    if not code or not state:
        return RedirectResponse("/settings?youtube_error=Missing+authorization+code")
    try:
        account = _complete_link(session, code=code, state=state)
    except HTTPException as exc:
        # Also log it: the browser only gets the redirect, so without this a
        # failed link leaves no trace anywhere.
        print(f"[youtube] link failed: {exc.detail}")
        return RedirectResponse(f"/settings?youtube_error={quote(str(exc.detail))}")
    return RedirectResponse(
        f"/settings?youtube_linked={quote(account.title or account.channel_id)}"
    )


@router.post("/link/complete")
def link_complete(body: YouTubeLinkComplete, session: Session = Depends(get_session)):
    """Finish a link by pasting the redirected URL (or the raw code + state)."""
    code, state = body.code, body.state
    if body.redirected_url:
        parsed = parse_qs(urlparse(body.redirected_url).query)
        code = (parsed.get("code") or [None])[0]
        state = (parsed.get("state") or [None])[0]
        error = (parsed.get("error") or [None])[0]
        if error and not code:
            raise HTTPException(400, f"Google returned an error: {error}")
    if not code:
        raise HTTPException(400, "No authorization code found in that URL.")
    account = _complete_link(session, code=code, state=state or "")
    return _serialize(session, account)


def _complete_link(session: Session, *, code: str, state: str) -> YouTubeAccount:
    _prune_pending()
    pending = _PENDING.pop(state, None)
    if state and not pending:
        # An expired or unknown state means we lost the group this login was
        # started for, so make the creator start over.
        raise HTTPException(400, "That login link expired. Start the link again.")
    try:
        payload = youtube.exchange_code(code)
    except youtube.YouTubeError as exc:
        raise HTTPException(400, str(exc))

    access_token = payload.get("access_token", "")
    if not access_token:
        raise HTTPException(502, "Google did not return an access token.")
    # Google's consent screen lists each scope as its own checkbox, and they
    # start unticked. Approving with only youtube.upload ticked yields a token
    # that cannot read the channel, so name the actual mistake here instead of
    # letting channels.list fail with a generic 403 further down.
    granted = set((payload.get("scope") or "").split())
    if not granted & {
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube",
    }:
        raise HTTPException(
            400,
            "The permission to read your channel was not granted. Start the link "
            "again and tick BOTH checkboxes on Google's consent screen (they are "
            "unticked by default).",
        )
    try:
        profile = youtube.fetch_channel(access_token)
    except youtube.YouTubeError as exc:
        raise HTTPException(502, str(exc))
    channel_id = profile.get("channel_id", "")
    if not channel_id:
        raise HTTPException(502, "Google did not return a channel id.")

    # Re-authorizing a channel already in the app updates it in place, so the
    # groups pointing at it keep working.
    account = session.exec(
        select(YouTubeAccount).where(YouTubeAccount.channel_id == channel_id)
    ).first() or YouTubeAccount(channel_id=channel_id)
    _apply_tokens(account, payload)
    _apply_profile(account, profile)
    if not account.refresh_token:
        # Without a refresh token the link dies in an hour. prompt=consent is
        # supposed to prevent this, so say so rather than storing a dud.
        raise HTTPException(
            400,
            "Google did not return a refresh token. Remove this app at "
            "myaccount.google.com/permissions and link again.",
        )
    account.needs_relink = False
    account.last_error = ""
    session.add(account)
    session.commit()
    session.refresh(account)

    preset_id = (pending or {}).get("content_preset_id")
    if preset_id:
        preset = session.get(ContentPreset, preset_id)
        if preset:
            preset.youtube_account_id = account.id
            session.add(preset)
            session.commit()
    return account


def _apply_profile(account: YouTubeAccount, profile: dict) -> None:
    account.title = profile.get("title", "") or account.title
    account.handle = profile.get("handle", "") or account.handle
    account.avatar_url = profile.get("avatar_url", "") or account.avatar_url
    account.updated_at = _now()


# --------------------------------------------------------------------------- #
# Group selection
# --------------------------------------------------------------------------- #
@router.put("/groups/{content_preset_id}/account")
def select_group_account(
    content_preset_id: str,
    body: YouTubeAccountSelect,
    session: Session = Depends(get_session),
):
    """Point a group at an already-linked channel (or clear it)."""
    preset = session.get(ContentPreset, content_preset_id)
    if not preset:
        raise HTTPException(404, "Group not found")
    if body.account_id:
        _get_account(session, body.account_id)
    preset.youtube_account_id = body.account_id or None
    session.add(preset)
    session.commit()
    return {"content_preset_id": preset.id, "youtube_account_id": preset.youtube_account_id}
