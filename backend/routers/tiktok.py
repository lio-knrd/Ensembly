"""TikTok account linking + publishing.

Accounts are app-level: you log in once per creator account, and any group
(content preset) can then *select* an existing account instead of logging in
again. Unlinking an account clears it from every group that pointed at it.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..models import ContentPreset, TikTokAccount
from ..schemas import TikTokAccountSelect, TikTokLinkComplete, TikTokLinkStart
from ..services import tiktok

router = APIRouter(prefix="/api/tiktok", tags=["tiktok"])

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
        select(ContentPreset).where(ContentPreset.tiktok_account_id == account_id)
    ).all()
    return [{"id": p.id, "name": p.name} for p in presets]


def _serialize(session: Session, account: TikTokAccount) -> dict:
    access_expires = _as_aware(account.access_expires_at)
    refresh_expires = _as_aware(account.refresh_expires_at)
    return {
        "id": account.id,
        "open_id": account.open_id,
        "display_name": account.display_name,
        "avatar_url": account.avatar_url,
        "scopes": account.scopes,
        "access_expires_at": access_expires.isoformat() if access_expires else None,
        "refresh_expires_at": refresh_expires.isoformat() if refresh_expires else None,
        # A dead refresh token is the one state the creator must fix by
        # re-linking, so it is reported separately from a stale access token.
        "needs_relink": bool(refresh_expires and refresh_expires <= _now()),
        "last_error": account.last_error,
        "groups": _groups_using(session, account.id),
    }


def _get_account(session: Session, account_id: str) -> TikTokAccount:
    account = session.get(TikTokAccount, account_id)
    if not account:
        raise HTTPException(404, "TikTok account not found")
    return account


def ensure_access_token(session: Session, account: TikTokAccount) -> str:
    """A valid access token for this account, refreshing it when due."""
    expires = _as_aware(account.access_expires_at)
    if account.access_token and expires and expires - _REFRESH_MARGIN > _now():
        return account.access_token
    if not account.refresh_token:
        raise HTTPException(400, "This TikTok account needs to be linked again.")
    try:
        payload = tiktok.refresh_tokens(account.refresh_token)
    except tiktok.TikTokError as exc:
        account.last_error = str(exc)
        session.add(account)
        session.commit()
        raise HTTPException(400, f"{exc} Re-link the account to continue.")
    _apply_tokens(account, payload)
    account.last_error = ""
    session.add(account)
    session.commit()
    session.refresh(account)
    return account.access_token


def _apply_tokens(account: TikTokAccount, payload: dict) -> None:
    access_expires, refresh_expires = tiktok.token_expiry(payload)
    account.access_token = payload.get("access_token", "") or account.access_token
    # TikTok may hand back a rotated refresh token; the old one stops working.
    account.refresh_token = payload.get("refresh_token", "") or account.refresh_token
    account.access_expires_at = access_expires
    account.refresh_expires_at = refresh_expires or account.refresh_expires_at
    account.scopes = payload.get("scope", "") or account.scopes
    account.open_id = payload.get("open_id", "") or account.open_id
    account.updated_at = _now()


# --------------------------------------------------------------------------- #
# Status + accounts
# --------------------------------------------------------------------------- #
@router.get("/status")
def status(session: Session = Depends(get_session)):
    accounts = session.exec(select(TikTokAccount).order_by(TikTokAccount.display_name)).all()
    return {
        "configured": tiktok.is_configured(),
        "redirect_uri": settings.tiktok_redirect_uri,
        "scopes": tiktok.SCOPES,
        "privacy_levels": list(tiktok.PRIVACY_LEVELS),
        "accounts": [_serialize(session, a) for a in accounts],
    }


@router.get("/accounts")
def list_accounts(session: Session = Depends(get_session)):
    accounts = session.exec(select(TikTokAccount).order_by(TikTokAccount.display_name)).all()
    return [_serialize(session, a) for a in accounts]


@router.delete("/accounts/{account_id}", status_code=204)
def unlink_account(account_id: str, session: Session = Depends(get_session)):
    account = _get_account(session, account_id)
    for preset in session.exec(
        select(ContentPreset).where(ContentPreset.tiktok_account_id == account_id)
    ):
        preset.tiktok_account_id = None
        session.add(preset)
    session.delete(account)
    session.commit()


@router.post("/accounts/{account_id}/refresh")
def refresh_account(account_id: str, session: Session = Depends(get_session)):
    account = _get_account(session, account_id)
    token = ensure_access_token(session, account)
    try:
        profile = tiktok.fetch_user_info(token)
    except tiktok.TikTokError as exc:
        raise HTTPException(502, str(exc))
    _apply_profile(account, profile)
    session.add(account)
    session.commit()
    session.refresh(account)
    return _serialize(session, account)


@router.get("/accounts/{account_id}/creator-info")
def account_creator_info(account_id: str, session: Session = Depends(get_session)):
    """Privacy options and limits TikTok requires us to show before posting."""
    account = _get_account(session, account_id)
    token = ensure_access_token(session, account)
    try:
        return tiktok.creator_info(token)
    except tiktok.TikTokError as exc:
        raise HTTPException(502, str(exc))


# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #
@router.post("/link/start")
def link_start(body: TikTokLinkStart, session: Session = Depends(get_session)):
    """Begin an authorization; the creator opens the returned URL."""
    if body.content_preset_id and not session.get(ContentPreset, body.content_preset_id):
        raise HTTPException(404, "Group not found")
    try:
        start = tiktok.start_authorization()
    except tiktok.TikTokError as exc:
        raise HTTPException(400, str(exc))
    _prune_pending()
    _PENDING[start.state] = {
        "code_verifier": start.code_verifier,
        "content_preset_id": body.content_preset_id,
        "created_at": time.time(),
    }
    return {"url": start.url, "state": start.state}


@router.get("/link/callback")
def link_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    session: Session = Depends(get_session),
):
    """Where TikTok redirects after the creator approves (https hosts only)."""
    if error:
        return RedirectResponse(f"/settings?tiktok_error={error_description or error}")
    if not code or not state:
        return RedirectResponse("/settings?tiktok_error=Missing+authorization+code")
    try:
        account = _complete_link(session, code=code, state=state)
    except HTTPException as exc:
        return RedirectResponse(f"/settings?tiktok_error={exc.detail}")
    return RedirectResponse(f"/settings?tiktok_linked={account.display_name or account.open_id}")


@router.post("/link/complete")
def link_complete(body: TikTokLinkComplete, session: Session = Depends(get_session)):
    """Finish a link by pasting the redirected URL (or the raw code + state).

    A local install can't receive TikTok's https redirect, so the creator copies
    the URL their browser landed on and pastes it here instead.
    """
    code, state = body.code, body.state
    if body.redirected_url:
        parsed = parse_qs(urlparse(body.redirected_url).query)
        code = (parsed.get("code") or [None])[0]
        state = (parsed.get("state") or [None])[0]
        error = (parsed.get("error_description") or parsed.get("error") or [None])[0]
        if error and not code:
            raise HTTPException(400, f"TikTok returned an error: {error}")
    if not code:
        raise HTTPException(400, "No authorization code found in that URL.")
    account = _complete_link(session, code=code, state=state or "")
    return _serialize(session, account)


def _complete_link(session: Session, *, code: str, state: str) -> TikTokAccount:
    _prune_pending()
    pending = _PENDING.pop(state, None)
    if state and not pending:
        # An expired or unknown state means we lost the PKCE verifier and the
        # group this login was started for, so make the creator start over.
        raise HTTPException(400, "That login link expired. Start the link again.")
    verifier = (pending or {}).get("code_verifier", "")
    try:
        payload = tiktok.exchange_code(code, verifier)
    except tiktok.TikTokError as exc:
        raise HTTPException(400, str(exc))

    open_id = payload.get("open_id", "")
    if not open_id:
        raise HTTPException(502, "TikTok did not return an account id.")
    # Re-authorizing an account already in the app updates it in place, so the
    # groups pointing at it keep working.
    account = session.exec(
        select(TikTokAccount).where(TikTokAccount.open_id == open_id)
    ).first() or TikTokAccount(open_id=open_id)
    _apply_tokens(account, payload)
    account.last_error = ""
    try:
        _apply_profile(account, tiktok.fetch_user_info(account.access_token))
    except tiktok.TikTokError:
        # A missing profile is cosmetic; the tokens are what matter.
        pass
    session.add(account)
    session.commit()
    session.refresh(account)

    preset_id = (pending or {}).get("content_preset_id")
    if preset_id:
        preset = session.get(ContentPreset, preset_id)
        if preset:
            preset.tiktok_account_id = account.id
            session.add(preset)
            session.commit()
    return account


def _apply_profile(account: TikTokAccount, profile: dict) -> None:
    account.display_name = profile.get("display_name", "") or account.display_name
    account.avatar_url = profile.get("avatar_url", "") or account.avatar_url
    account.union_id = profile.get("union_id", "") or account.union_id
    account.updated_at = _now()


# --------------------------------------------------------------------------- #
# Group selection
# --------------------------------------------------------------------------- #
@router.put("/groups/{content_preset_id}/account")
def select_group_account(
    content_preset_id: str,
    body: TikTokAccountSelect,
    session: Session = Depends(get_session),
):
    """Point a group at an already-linked account (or clear it)."""
    preset = session.get(ContentPreset, content_preset_id)
    if not preset:
        raise HTTPException(404, "Group not found")
    if body.account_id:
        _get_account(session, body.account_id)
    preset.tiktok_account_id = body.account_id or None
    session.add(preset)
    session.commit()
    return {"content_preset_id": preset.id, "tiktok_account_id": preset.tiktok_account_id}
