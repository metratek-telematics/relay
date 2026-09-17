"""Who is asking.

Relay sits behind a forward-auth proxy (Traefik + Authentik). The proxy authenticates people and passes
X-Authentik-Username / -Email / -Name / -Groups / -Uid (or X-Forwarded-User). Those headers are only
believed when the request's direct peer is a configured trusted proxy (or header auth is switched on);
from anyone else they are ignored, so a client that reaches Relay's port directly cannot claim to be
somebody. With no identity configured at all, every request is the local owner, as before.
"""
from __future__ import annotations

import hashlib
import re
import threading
import unicodedata

from . import settings as OS
from .common import JsonStore, now_iso, parse_iso, epoch

LEVEL = {"anonymous": 0, "viewer": 10, "member": 20, "admin": 30, "owner": 40}
LOCAL_USERNAME = "owner"

NOTIFY_EVENTS = ["needs_input", "approval", "delivered", "failed", "digest", "budget"]
CHANNELS = ["inapp", "browser", "email", "slack", "telegram", "discord", "webhook"]

DEFAULT_PREFS = {
    "theme": "system",
    "density": "comfortable",
    "gravatar": False,
    "notifications": {
        "events": {"needs_input": ["inapp", "browser"], "approval": ["inapp", "browser"], "delivered": ["inapp"],
                   "failed": ["inapp", "browser"], "digest": ["inapp"], "budget": ["inapp"]},
        "channels": {"email": {"address": ""}, "slack": {"webhook_url": ""}, "telegram": {"chat_id": ""},
                     "discord": {"webhook_url": ""}, "webhook": {"url": "", "secret": ""}},
        "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
        "digest_time": "08:00",
        "projects": [],   # empty = every project
    },
}

_store = JsonStore("users.json", {"users": []})
_seen_lock = threading.Lock()
_last_touch: dict[str, float] = {}


def _deep(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep(base.get(k), v) if isinstance(base.get(k), dict) and isinstance(v, dict) and k != "events" else v
        return out
    return over


def norm_username(s) -> str:
    s = str(s or "").strip()
    return re.sub(r"[^A-Za-z0-9@._+-]", "", s)[:120]


def clean_text(s) -> str:
    """A display string as a person would type it.

    WSGI hands over request headers decoded as Latin-1, so a UTF-8 name from the identity provider arrives as
    mojibake ("Andreas\u00e2\u0080\u008b"). Undo that when it round-trips, then drop zero-width and control
    characters, which otherwise become visible initials such as "A\u00c2".
    """
    s = str(s or "")
    if any("\u0080" <= ch <= "\u00ff" for ch in s):
        try:
            s = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cf", "Cc", "Co", "Cn") or ch in "\t")
    return re.sub(r"\s+", " ", s).strip()


def initials(name: str, username: str = "") -> str:
    words = [w for w in re.split(r"[\s._@-]+", clean_text(name) or clean_text(username) or "?") if w]
    letters = ["".join(ch for ch in w if ch.isalnum()) for w in words]
    letters = [w for w in letters if w]
    if not letters:
        return "?"
    if len(letters) == 1:
        return letters[0][:2].upper()
    return (letters[0][0] + letters[-1][0]).upper()


def avatar_color(key: str) -> str:
    h = int(hashlib.sha256((key or "?").encode()).hexdigest()[:6], 16)
    return f"hsl({h % 360}, 50%, 36%)"  # dark enough for white initials in both themes (WCAG AA)


def users() -> list[dict]:
    return [with_defaults(u) for u in _store.read().get("users") or []]


def get(username: str) -> dict | None:
    username = norm_username(username).lower()
    for u in users():
        if u["username"].lower() == username:
            return u
    return None


def with_defaults(u: dict) -> dict:
    u = dict(u)
    u["prefs"] = _deep(DEFAULT_PREFS, u.get("prefs") or {})
    u.setdefault("groups", [])
    u.setdefault("role", "viewer")
    u.setdefault("disabled", False)
    return u


def public(u: dict | None, full: bool = False) -> dict | None:
    if not u:
        return None
    email = (u.get("email") or "").strip().lower()
    out = {
        "username": u["username"], "name": clean_text(u.get("name")) or u["username"], "email": clean_text(u.get("email")),
        "role": u.get("role"), "role_source": u.get("role_source"), "groups": u.get("groups") or [],
        "disabled": bool(u.get("disabled")), "first_seen": u.get("first_seen"), "last_seen": u.get("last_seen"),
        "initials": initials(u.get("name") or "", u["username"]), "color": avatar_color(u["username"]),
        "avatar_url": u.get("avatar_url") or (f"https://www.gravatar.com/avatar/{hashlib.md5(email.encode()).hexdigest()}?s=96&d=404"
                                              if email and (u.get("prefs") or {}).get("gravatar") else ""),
        "budget_monthly_usd": float(u.get("budget_monthly_usd") or 0), "local": bool(u.get("local")),
    }
    if full:
        from .common import mask
        prefs = u.get("prefs") or {}
        ch = ((prefs.get("notifications") or {}).get("channels") or {})
        masked = mask(prefs)
        # People see that a secret is set, never the value (MASK round-trips unchanged on save).
        for name in ("slack", "discord"):
            masked["notifications"]["channels"][name]["has_webhook_url"] = bool((ch.get(name) or {}).get("webhook_url"))
        masked["notifications"]["channels"]["webhook"]["has_secret"] = bool((ch.get("webhook") or {}).get("secret"))
        out["prefs"] = masked
        out["telegram_linked"] = bool(u.get("telegram_user_id"))
    return out


def role_from_groups(groups: list[str], data: dict) -> str | None:
    mapping = {k.lower(): v for k, v in (data["auth"].get("group_roles") or {}).items()}
    best = None
    for g in groups:
        r = mapping.get(g.strip().lower())
        if r and (best is None or LEVEL[r] > LEVEL[best]):
            best = r
    return best


def _save_user(u: dict):
    def fn(d):
        rows = [x for x in d.get("users") or [] if x.get("username", "").lower() != u["username"].lower()]
        rows.append(u)
        d["users"] = sorted(rows, key=lambda x: x.get("username", "").lower())
    _store.update(fn)


def upsert_from_headers(h: dict, ip: str) -> dict | None:
    """Create or refresh the user a trusted proxy vouched for. Returns None when auto-provisioning is off."""
    data = OS.load()
    username = norm_username(h.get("username"))
    if not username:
        return None
    groups = [g.strip() for g in re.split(r"[|,]", h.get("groups") or "") if g.strip()]
    with _store.lock:
        u = get(username)
        now = now_iso()
        if not u:
            if not data["auth"].get("auto_provision", True):
                return None
            has_owner = any(x.get("role") == "owner" and not x.get("local") for x in users())
            u = {"username": username, "first_seen": now, "role_source": "default", "role": data["auth"].get("default_role") or "viewer"}
            if not has_owner and data["auth"].get("first_user_owner", True):
                u["role"], u["role_source"] = "owner", "bootstrap"
        before = repr({k: u.get(k) for k in ("uid", "email", "name", "groups", "last_ip", "role", "role_source")})
        u.update({"uid": h.get("uid") or u.get("uid") or "", "email": h.get("email") or u.get("email") or "",
                  "name": h.get("name") or u.get("name") or username, "groups": groups, "last_ip": ip})
        if username.lower() in {o.lower() for o in data["auth"].get("owners") or []}:
            u["role"], u["role_source"] = "owner", "env"
        elif u.get("role_source") not in ("manual", "bootstrap"):
            mapped = role_from_groups(groups, data)
            if mapped:
                u["role"], u["role_source"] = mapped, "groups"
            elif u.get("role_source") == "groups":
                u["role"], u["role_source"] = data["auth"].get("default_role") or "viewer", "default"
        after = repr({k: u.get(k) for k in ("uid", "email", "name", "groups", "last_ip", "role", "role_source")})
        if before == after and not _stale(u):
            return with_defaults(u)
        touch(u, save=False)
        _save_user(u)
        return with_defaults(u)


def local_owner(ip: str = "") -> dict:
    with _store.lock:
        u = get(LOCAL_USERNAME)
        if not u:
            u = {"username": LOCAL_USERNAME, "name": "Owner", "email": "", "role": "owner", "role_source": "local", "local": True,
                 "first_seen": now_iso(), "groups": []}
            touch(u, save=False)
            _save_user(u)
        elif _stale(u):
            touch(u)
        return with_defaults(u)


def _stale(u) -> bool:
    return epoch() - _last_touch.get(u["username"], 0) > 60


def touch(u: dict, save: bool = True):
    """last_seen, written at most once a minute per person."""
    with _seen_lock:
        if save and not _stale(u):
            return
        _last_touch[u["username"]] = epoch()
    u["last_seen"] = now_iso()
    if save:
        _save_user({k: v for k, v in u.items()})


def set_role(username: str, role: str, actor: dict) -> dict:
    if role not in OS.ROLES:
        raise ValueError("Role must be viewer, member, admin or owner.")
    with _store.lock:
        u = get(username)
        if not u:
            raise KeyError("No such user")
        owners = [x for x in users() if x.get("role") == "owner" and not x.get("disabled")]
        if u.get("role") == "owner" and role != "owner" and len(owners) <= 1:
            raise ValueError("Relay needs at least one owner. Make someone else owner first.")
        u["role"], u["role_source"] = role, "manual"
        _save_user(u)
        return u


def update_user(username: str, patch: dict, actor: dict) -> dict:
    """Owner edits: role, disabled, budget, display name; 'role_source': 'groups' hands the role back to the mapping."""
    with _store.lock:
        u = get(username)
        if not u:
            raise KeyError("No such user")
        if "role" in patch and patch["role"] != u.get("role"):
            u = set_role(username, patch["role"], actor)
        if patch.get("role_source") == "groups":
            mapped = role_from_groups(u.get("groups") or [], OS.load())
            u["role_source"], u["role"] = ("groups", mapped) if mapped else ("default", OS.load()["auth"].get("default_role") or "viewer")
        if "disabled" in patch:
            if patch["disabled"] and u["username"] == actor.get("username"):
                raise ValueError("You cannot disable yourself.")
            if patch["disabled"] and u.get("role") == "owner" and len([x for x in users() if x.get("role") == "owner" and not x.get("disabled")]) <= 1:
                raise ValueError("Relay needs at least one active owner.")
            u["disabled"] = bool(patch["disabled"])
        if "budget_monthly_usd" in patch:
            u["budget_monthly_usd"] = max(0.0, float(patch["budget_monthly_usd"] or 0))
        if "name" in patch and str(patch["name"]).strip():
            u["name"] = str(patch["name"]).strip()[:120]
        _save_user(u)
        return with_defaults(u)


def delete_user(username: str, actor: dict):
    u = get(username)
    if not u:
        raise KeyError("No such user")
    if u["username"] == actor.get("username"):
        raise ValueError("You cannot remove yourself.")
    if u.get("role") == "owner" and len([x for x in users() if x.get("role") == "owner"]) <= 1:
        raise ValueError("Relay needs at least one owner.")
    _store.update(lambda d: {**d, "users": [x for x in d.get("users") or [] if x.get("username") != u["username"]]})


HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def save_prefs(username: str, prefs: dict) -> dict:
    """A person's own preferences. Secret fields sent back as MASK keep their stored value."""
    from .common import MASK
    with _store.lock:
        u = get(username)
        if not u:
            raise KeyError("No such user")
        old = u["prefs"]
        new = _deep(old, prefs or {})
        n = new["notifications"]
        for ev, chans in list((n.get("events") or {}).items()):
            if ev not in NOTIFY_EVENTS:
                n["events"].pop(ev)
                continue
            n["events"][ev] = [c for c in dict.fromkeys(chans or []) if c in CHANNELS]
        ch, och = n["channels"], old["notifications"]["channels"]
        for name, key in (("slack", "webhook_url"), ("discord", "webhook_url"), ("webhook", "secret")):
            if (ch.get(name) or {}).get(key) == MASK:
                ch[name][key] = (och.get(name) or {}).get(key, "")
            ch[name].pop("has_" + key, None)
        for name, key in (("slack", "webhook_url"), ("discord", "webhook_url"), ("webhook", "url")):
            v = str((ch.get(name) or {}).get(key) or "").strip()
            if v and not re.match(r"^https?://", v):
                raise ValueError(f"{name.title()} address must start with https://")
        chat = str(ch["telegram"].get("chat_id") or "").strip()
        if chat and not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{4,64}", chat):
            raise ValueError("Telegram chat id is a number (message the bot, then use /start) or @channel.")
        email = str(ch["email"].get("address") or "").strip()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("That email address does not look right.")
        q = n["quiet_hours"]
        for k in ("start", "end"):
            if not HHMM.match(str(q.get(k) or "")):
                raise ValueError("Quiet hours use 24-hour HH:MM times.")
        if not HHMM.match(str(n.get("digest_time") or "")):
            raise ValueError("Digest time uses 24-hour HH:MM.")
        if new.get("theme") not in ("system", "light", "dark"):
            new["theme"] = "system"
        if new.get("density") not in ("comfortable", "compact"):
            new["density"] = "comfortable"
        u["prefs"] = new
        _save_user(u)
        return with_defaults(u)


def link_telegram(username: str, telegram_user_id: str):
    with _store.lock:
        u = get(username)
        if u:
            u["telegram_user_id"] = str(telegram_user_id)
            _save_user(u)


def unlink_telegram(username: str):
    with _store.lock:
        u = get(username)
        if u and u.get("telegram_user_id"):
            u.pop("telegram_user_id", None)
            _save_user(u)


def by_telegram(telegram_user_id) -> dict | None:
    if telegram_user_id in (None, ""):
        return None
    for u in users():
        if str(u.get("telegram_user_id") or "") == str(telegram_user_id) and not u.get("disabled"):
            return u
    return None


def active_since(days: int = 120) -> list[dict]:
    cutoff = epoch() - days * 86400
    return [u for u in users() if not u.get("disabled") and parse_iso(u.get("last_seen")) >= cutoff]


def raw_store() -> JsonStore:
    return _store
