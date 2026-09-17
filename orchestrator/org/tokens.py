"""Personal access tokens for /api/v1.

A token looks like rly_<8 hex id>_<43 url-safe characters> (256 bits of randomness). Only its SHA-256 is
stored; the full value is shown once, at creation. Lookup is by the id part, comparison is constant-time.
Tokens carry scopes (read, tasks:write, admin), an optional expiry, and record when and from where they
were last used. A token acts as its owner, capped by its scopes, and dies with its owner's account.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import identity
from .common import JsonStore, now_iso, parse_iso
from .rbac import SCOPES, level

_store = JsonStore("tokens.json", {"tokens": []})
TOKEN_RX = re.compile(r"^rly_([0-9a-f]{8})_([A-Za-z0-9_-]{40,64})$")
_used_at: dict[str, float] = {}
_pending_uses: dict[str, int] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(user: dict, name: str, scopes: list[str], expires_days: int | None) -> tuple[dict, str]:
    name = str(name or "").strip()[:80]
    if not name:
        raise ValueError("Name the token after what uses it (for example “GitHub Actions: web”).")
    scopes = [s for s in dict.fromkeys(scopes or []) if s in SCOPES]
    if not scopes:
        raise ValueError("Choose at least one scope.")
    for s in scopes:
        if level(SCOPES[s]) > level(user.get("role") or "viewer"):
            raise ValueError(f"The {s} scope needs the {SCOPES[s]} role; you are {user.get('role')}.")
    if expires_days not in (None, 0):
        expires_days = int(expires_days)
        if not 1 <= expires_days <= 366:
            raise ValueError("Expiry is between 1 and 366 days, or never.")
    tid = secrets.token_hex(4)
    token = f"rly_{tid}_{secrets.token_urlsafe(32)}"
    row = {"id": tid, "name": name, "username": user["username"], "scopes": scopes, "hash": _hash(token),
           "prefix": token[:12], "created_at": now_iso(),
           "expires_at": (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat(timespec="seconds") if expires_days else None,
           "last_used_at": None, "last_used_ip": None, "uses": 0, "revoked": False}
    _store.update(lambda d: {**d, "tokens": (d.get("tokens") or []) + [row]})
    return public(row), token


def public(row: dict) -> dict:
    out = {k: row.get(k) for k in ("id", "name", "username", "scopes", "prefix", "created_at", "expires_at", "last_used_at", "last_used_ip", "uses", "revoked")}
    out["expired"] = bool(row.get("expires_at")) and parse_iso(row["expires_at"]) < time.time()
    out["active"] = not out["expired"] and not row.get("revoked")
    return out


def list_for(username: str | None = None) -> list[dict]:
    rows = _store.read().get("tokens") or []
    return [public(r) for r in rows if username is None or r.get("username") == username][::-1]


def revoke(token_id: str, actor: dict) -> dict:
    out = {}

    def fn(d):
        for r in d.get("tokens") or []:
            if r["id"] == token_id:
                if r["username"] != actor.get("username") and level(actor.get("role")) < level("owner"):
                    raise PermissionError("Only the token's owner or a Relay owner can revoke it.")
                r["revoked"] = True
                r["revoked_at"] = now_iso()
                r["revoked_by"] = actor.get("username")
                out.update(public(r))
    _store.update(fn)
    if not out:
        raise KeyError("No such token")
    return out


def authenticate(header: str, ip: str = "") -> tuple[dict, dict]:
    """(user, token row) for an Authorization header, or raises PermissionError with a reason safe to show."""
    m = re.match(r"^\s*Bearer\s+(\S+)\s*$", header or "", re.I)
    if not m:
        raise PermissionError("Send a personal access token as Authorization: Bearer rly_…")
    token = m.group(1)
    tm = TOKEN_RX.match(token)
    if not tm:
        raise PermissionError("That is not a Relay access token.")
    row = next((r for r in _store.read().get("tokens") or [] if r["id"] == tm.group(1)), None)
    # Compare against a dummy hash when the id is unknown, so timing does not reveal which ids exist.
    expected = row["hash"] if row else _hash("rly_00000000_" + "x" * 43)
    if not hmac.compare_digest(expected, _hash(token)) or not row:
        raise PermissionError("Invalid access token.")
    if row.get("revoked"):
        raise PermissionError("This access token was revoked.")
    if row.get("expires_at") and parse_iso(row["expires_at"]) < time.time():
        raise PermissionError("This access token has expired.")
    user = identity.get(row["username"])
    if not user or user.get("disabled"):
        raise PermissionError("The person this token belongs to no longer has access.")
    _mark_used(row["id"], ip)
    return user, row


def _mark_used(tid: str, ip: str):
    # Written at most every 30 s per token, so a busy CI job does not rewrite the file on every call.
    now = time.time()
    _pending_uses[tid] = _pending_uses.get(tid, 0) + 1
    if now - _used_at.get(tid, 0) < 30:
        return
    _used_at[tid] = now
    n = _pending_uses.pop(tid, 1)

    def fn(d):
        for r in d.get("tokens") or []:
            if r["id"] == tid:
                r["last_used_at"] = now_iso()
                r["last_used_ip"] = ip
                r["uses"] = int(r.get("uses") or 0) + n
    _store.update(fn)


def raw_store() -> JsonStore:
    return _store
