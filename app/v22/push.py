"""
Firebase Cloud Messaging (FCM) push notifier for V22 signals.

Mirrors notify.py's structure, but targets registered native app device
tokens (push_tokens table) instead of Telegram chat_ids, and talks to FCM's
HTTP v1 API directly via httpx + a hand-rolled service-account OAuth2 token
exchange (RS256 JWT, signed with `cryptography`) — no firebase-admin SDK,
which would drag ~15-20 new wheels (incl. grpcio) into this repo's offline
wheel-only Docker build for a single API call.

Fires when the scanner inserts a new row into v22_signals or closes an open
one. Loads the set of enabled device tokens whose owner's tier meets the
configured floor and sends each one an FCM message.

Graceful no-op when FIREBASE_SERVICE_ACCOUNT_JSON is unset — useful for
local dev and before the credential is provisioned in Firebase Console.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx

from app.config import get_settings
from app.db import get_db

log = logging.getLogger("v22.push")

TIER_ORDER = ["free", "trader", "auto"]
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"

# In-memory OAuth2 access-token cache: {project_id: (token, expires_at_epoch)}
_token_cache: dict[str, tuple[str, float]] = {}


def _tier_index(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return 0


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_service_account() -> dict[str, Any] | None:
    raw = get_settings().firebase_service_account_json
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.warning(f"[push] FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")
        return None


def _sign_jwt(sa: dict[str, Any]) -> str:
    """Build + sign a service-account JWT assertion for the OAuth2 token exchange."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": sa["client_email"],
        "scope": FCM_SCOPE,
        "aud": sa.get("token_uri", TOKEN_URI_DEFAULT),
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"

    private_key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


async def _get_access_token(sa: dict[str, Any]) -> str | None:
    project_id = sa.get("project_id", "")
    cached = _token_cache.get(project_id)
    if cached and cached[1] - 60 > time.time():
        return cached[0]

    try:
        assertion = _sign_jwt(sa)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                sa.get("token_uri", TOKEN_URI_DEFAULT),
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        if resp.status_code != 200:
            log.warning(f"[push] token exchange failed: {resp.status_code} {resp.text[:200]}")
            return None
        body = resp.json()
        token = body["access_token"]
        _token_cache[project_id] = (token, time.time() + body.get("expires_in", 3600))
        return token
    except Exception as e:
        log.warning(f"[push] token exchange errored: {e}")
        return None


# ── Eligibility ─────────────────────────────────────────────────────────────

def _list_eligible_tokens() -> list[tuple[int, str, str]]:
    """Return (row_id, token, user_id) for every device token that should
    receive push alerts, mirroring notify.py's _list_eligible_chat_ids."""
    settings = get_settings()
    floor_idx = _tier_index(settings.push_signal_min_tier)
    db = get_db()

    try:
        rows = (
            db.table("push_tokens")
            .select("id, user_id, token, enabled")
            .execute()
        )
    except Exception as e:
        log.warning(f"[push] could not list device tokens: {e}")
        return []

    candidates = [r for r in (rows.data or []) if r.get("enabled") and r.get("token")]
    if not candidates:
        return []

    user_ids = list({c["user_id"] for c in candidates})
    try:
        profs = (
            db.table("profiles")
            .select("id, tier")
            .in_("id", user_ids)
            .execute()
        )
    except Exception as e:
        log.warning(f"[push] could not fetch profiles for tier check: {e}")
        return []

    tier_by_user: dict[str, str] = {p["id"]: (p.get("tier") or "free") for p in (profs.data or [])}
    eligible: list[tuple[int, str, str]] = []
    for c in candidates:
        tier = tier_by_user.get(c["user_id"], "free")
        if _tier_index(tier) >= floor_idx:
            eligible.append((c["id"], c["token"], c["user_id"]))
    return eligible


# ── Message builders ─────────────────────────────────────────────────────────

def _fmt_price(p: float | None) -> str:
    if p is None:
        return "—"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.3f}"
    return f"${p:.6f}".rstrip("0").rstrip(".")


def _build_open_notification(signal: dict[str, Any]) -> tuple[str, str]:
    asset = signal.get("asset") or (signal.get("symbol") or "").split("/")[0]
    direction = (signal.get("direction") or "").upper()
    entry = _fmt_price(signal.get("entry"))
    title = f"{'🟢' if direction == 'LONG' else '🔴'} V22 {direction} · {asset}"
    body = f"Entry {entry} · SL {_fmt_price(signal.get('stop_loss'))} · TP1 {_fmt_price(signal.get('tp1'))}"
    return title, body


def _build_close_notification(signal: dict[str, Any]) -> tuple[str, str]:
    asset = signal.get("asset") or (signal.get("symbol") or "").split("/")[0]
    direction = (signal.get("direction") or "").upper()
    pnl = signal.get("pnl")
    ret_pct = signal.get("ret_pct")
    is_profit = (float(pnl) >= 0) if pnl is not None else (float(ret_pct) >= 0 if ret_pct is not None else True)
    pnl_label = f"{'+' if is_profit else ''}{pnl:.2f}" if pnl is not None else "—"
    ret_label = f"{'+' if is_profit else ''}{ret_pct:.2f}%" if ret_pct is not None else ""
    title = f"{'🎉' if is_profit else '❌'} V22 {direction} closed · {asset}"
    body = f"P&L {pnl_label} ({ret_label})".strip()
    return title, body


# ── Sender ──────────────────────────────────────────────────────────────────

async def _send_one(client: httpx.AsyncClient, project_id: str, access_token: str,
                    token: str, title: str, body: str, data: dict[str, str]) -> bool:
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    message = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": data,
            "android": {"priority": "high"},
        }
    }
    try:
        resp = await client.post(
            url,
            json=message,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return True
        # 404/NOT_FOUND or UNREGISTERED means a stale token (app uninstalled,
        # token rotated) — not actionable here, just log and move on.
        log.warning(f"[push] send to token ...{token[-8:]} -> {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        log.warning(f"[push] send to token ...{token[-8:]} errored: {e}")
        return False


async def _dispatch(title: str, body: str, data: dict[str, str]) -> int:
    sa = _load_service_account()
    if sa is None:
        return 0

    tokens = _list_eligible_tokens()
    if not tokens:
        return 0

    access_token = await _get_access_token(sa)
    if not access_token:
        return 0

    project_id = sa.get("project_id", "")
    sent = 0
    async with httpx.AsyncClient() as client:
        for _row_id, token, _user_id in tokens:
            ok = await _send_one(client, project_id, access_token, token, title, body, data)
            if ok:
                sent += 1
    log.info(f"[push] dispatched to {sent}/{len(tokens)} device(s)")
    return sent


async def notify_new_signal_push(signal: dict[str, Any]) -> int:
    """Dispatch a fresh V22 signal as a native push to every eligible device."""
    title, body = _build_open_notification(signal)
    data = {
        "type": "signal_open",
        "signal_id": str(signal.get("id", "")),
        "asset": str(signal.get("asset") or ""),
    }
    return await _dispatch(title, body, data)


async def notify_closed_signal_push(signal: dict[str, Any]) -> int:
    """Dispatch a trade-close update as a native push to every eligible device."""
    title, body = _build_close_notification(signal)
    data = {
        "type": "signal_close",
        "signal_id": str(signal.get("id", "")),
        "asset": str(signal.get("asset") or ""),
    }
    return await _dispatch(title, body, data)
