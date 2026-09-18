"""Organisation settings: sign-in, role mapping, integrations and budgets.

Stored in DATA_DIR/org/settings.json (mode 0600). Secrets are write-only through the API: the browser gets
MASK plus has_<key> flags, and saving MASK back keeps the stored value.
Deployment can pin the trust boundary with environment variables, which win over the file:

    RELAY_TRUSTED_PROXIES   comma-separated IPs or CIDR networks allowed to assert identity headers
    RELAY_HEADER_AUTH       1 = trust identity headers from any peer (only behind a proxy that strips them)
    RELAY_OWNERS            comma-separated usernames that are always owners
    RELAY_PUBLIC_URL        the address people use (links in Slack, Telegram and email)
"""
from __future__ import annotations

import copy
import ipaddress
import os

from .common import MASK, JsonStore

ROLES = ["viewer", "member", "admin", "owner"]

DEFAULTS = {
    "auth": {
        "trusted_proxies": [],        # e.g. ["172.18.0.0/16"] for the Traefik network
        "header_auth": False,         # trust identity headers from any peer
        "allow_local_fallback": False,  # identity mode: requests without identity act as the local owner (development only)
        "auto_provision": True,       # create a user the first time a person signs in
        "default_role": "viewer",     # role for new people no group maps
        "first_user_owner": True,     # nobody is owner yet: the first person to sign in becomes owner
        "group_roles": {"relay-owners": "owner", "relay-admins": "admin", "relay-members": "member", "relay-viewers": "viewer",
                        "authentik Admins": "admin"},
        "owners": [],
    },
    "integrations": {
        "public_url": "",
        "smtp": {"host": "", "port": 587, "security": "starttls", "username": "", "password": "", "from": ""},
        # mode: polling (default; nothing exposed), webhook (Telegram calls Relay's public URL) or off (send only).
        "telegram": {"bot_token": "", "api_base": "https://api.telegram.org", "webhook_secret": "", "mode": "polling",
                     "groups_enabled": False, "rate_limit_per_minute": 30, "progress_throttle_seconds": 60,
                     "concierge": {"enabled": True, "agent": "", "model": "", "effort": "", "timeout_seconds": 90,
                                   "max_context_chars": 14000, "daily_turn_limit": 200, "per_minute": 6, "memory_turns": 8},
                     "voice": {"enabled": True}},
        "slack": {"webhook_url": ""},
        "discord": {"webhook_url": ""},
        "webhooks": [],               # [{id, name, url, secret, events: [], projects: [], enabled}]
        "webpush": {"public_key": "", "private_key": "", "subject": "mailto:relay@localhost"},
        "rate_limit_per_minute": 20,  # per channel target
        "max_attempts": 5,
        "allow_private_targets": False,  # allow webhook targets on private networks (self-hosted receivers); off blocks SSRF into the LAN
    },
    "budgets": {"org_monthly_usd": 0, "alert_thresholds": [50, 80, 100]},
    # Model providers agents can run on (orchestrator/openrouter.py holds the defaults and the validation).
    "providers": {"openrouter": {}},
}

SECRET_PATHS = [("integrations", "smtp", "password"), ("integrations", "telegram", "bot_token"),
                ("integrations", "telegram", "webhook_secret"), ("integrations", "slack", "webhook_url"),
                ("integrations", "discord", "webhook_url"), ("integrations", "webpush", "private_key"),
                ("providers", "openrouter", "api_key")]

_store = JsonStore("settings.json", DEFAULTS)


def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if isinstance(base.get(k), dict) and isinstance(v, dict) and k != "group_roles" else v
        return out
    return copy.deepcopy(over)


def load() -> dict:
    data = _merge(DEFAULTS, _store.read())
    env = os.environ
    if env.get("RELAY_TRUSTED_PROXIES") is not None:
        data["auth"]["trusted_proxies"] = [x.strip() for x in env["RELAY_TRUSTED_PROXIES"].split(",") if x.strip()]
        data["auth"]["_env_trusted_proxies"] = True
    if env.get("RELAY_HEADER_AUTH") is not None:
        data["auth"]["header_auth"] = env["RELAY_HEADER_AUTH"].strip().lower() in ("1", "true", "yes", "on")
        data["auth"]["_env_header_auth"] = True
    if env.get("RELAY_OWNERS"):
        data["auth"]["owners"] = sorted(set(data["auth"].get("owners") or []) | {x.strip() for x in env["RELAY_OWNERS"].split(",") if x.strip()})
    if env.get("RELAY_PUBLIC_URL"):
        data["integrations"]["public_url"] = env["RELAY_PUBLIC_URL"].strip()
    return data


def _get(d, path):
    for k in path:
        d = (d or {}).get(k)
    return d


def _set(d, path, v):
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = v


def public(data: dict | None = None) -> dict:
    data = copy.deepcopy(data or load())
    for p in SECRET_PATHS:
        v = _get(data, p)
        _set(data, p[:-1] + ("has_" + p[-1],), bool(v))
        _set(data, p, MASK if v else "")
    for w in data["integrations"].get("webhooks") or []:
        w["has_secret"] = bool(w.get("secret"))
        w["secret"] = MASK if w.get("secret") else ""
    return data


def validate_networks(rows) -> list[str]:
    out = []
    for r in rows or []:
        r = str(r).strip()
        if not r:
            continue
        try:
            ipaddress.ip_network(r, strict=False)
        except ValueError:
            raise ValueError(f"“{r}” is not an IP address or network (use 172.18.0.2 or 172.18.0.0/16).")
        out.append(r)
    return out


def save(partial: dict) -> dict:
    """Merge a partial update. MASK for a secret keeps what is stored."""
    def apply(cur):
        cur = _merge(DEFAULTS, cur)
        old = copy.deepcopy(cur)
        for section in ("auth", "integrations", "budgets", "providers"):
            if isinstance(partial.get(section), dict):
                cur[section] = _merge(cur[section], partial[section])
        if isinstance((partial.get("providers") or {}).get("openrouter"), dict):
            # Lists (fallback models, provider order) and the per-project caps are replaced, not merged.
            inc = partial["providers"]["openrouter"]
            for k in ("fallback_models", "project_caps"):
                if k in inc:
                    cur["providers"]["openrouter"][k] = inc[k]
            if isinstance(inc.get("routing"), dict):
                for k in ("order", "ignore"):
                    if k in inc["routing"]:
                        cur["providers"]["openrouter"].setdefault("routing", {})[k] = inc["routing"][k]
        for p in SECRET_PATHS:
            if _get(cur, p) == MASK:
                _set(cur, p, _get(old, p))
        if "webhooks" in (partial.get("integrations") or {}):
            olds = {w.get("id"): w for w in old["integrations"].get("webhooks") or []}
            rows = []
            for w in cur["integrations"].get("webhooks") or []:
                if w.get("secret") == MASK:
                    w["secret"] = (olds.get(w.get("id")) or {}).get("secret", "")
                rows.append(w)
            cur["integrations"]["webhooks"] = rows
        a = cur["auth"]
        a["trusted_proxies"] = validate_networks(a.get("trusted_proxies"))
        if a.get("default_role") not in ROLES:
            raise ValueError("Default role must be viewer, member, admin or owner.")
        if a.get("default_role") == "owner":
            raise ValueError("New people cannot become owners by default.")
        for g, r in (a.get("group_roles") or {}).items():
            if r not in ROLES:
                raise ValueError(f"Group {g}: role must be one of {', '.join(ROLES)}.")
        for k in list(a):
            if k.startswith("_env"):
                a.pop(k)
        from .. import openrouter as OR
        orp = cur["providers"].get("openrouter") or {}
        if orp:
            clean = OR.validate(orp)
            clean["api_key"] = (orp.get("api_key") or "").strip()
            cur["providers"]["openrouter"] = clean
        b = cur["budgets"]
        b["org_monthly_usd"] = max(0.0, float(b.get("org_monthly_usd") or 0))
        b["alert_thresholds"] = sorted({max(1, min(200, int(x))) for x in b.get("alert_thresholds") or [80, 100]})
        i = cur["integrations"]
        i["rate_limit_per_minute"] = max(1, min(600, int(i.get("rate_limit_per_minute") or 20)))
        i["max_attempts"] = max(1, min(10, int(i.get("max_attempts") or 5)))
        tg = i["telegram"]
        if tg.get("mode") not in ("polling", "webhook", "off"):
            raise ValueError("Telegram mode must be polling, webhook or off.")
        tg["rate_limit_per_minute"] = max(1, min(600, int(tg.get("rate_limit_per_minute") or 30)))
        tg["progress_throttle_seconds"] = max(10, min(3600, int(tg.get("progress_throttle_seconds") or 60)))
        cc = tg["concierge"]
        if cc.get("agent") not in ("", "claude", "codex"):
            raise ValueError("The Telegram assistant runs on Claude or Codex (or leave it on automatic).")
        cc["timeout_seconds"] = max(10, min(600, int(cc.get("timeout_seconds") or 90)))
        cc["max_context_chars"] = max(2000, min(60000, int(cc.get("max_context_chars") or 14000)))
        cc["daily_turn_limit"] = max(1, min(5000, int(cc.get("daily_turn_limit") or 200)))
        cc["per_minute"] = max(1, min(60, int(cc.get("per_minute") or 6)))
        cc["memory_turns"] = max(0, min(30, int(cc.get("memory_turns") if cc.get("memory_turns") is not None else 8)))
        return cur
    _store.update(apply)
    return load()


def identity_mode(data: dict | None = None) -> bool:
    """True once Relay is told who people are (a trusted proxy or header auth); False = single-user local mode."""
    a = (data or load())["auth"]
    return bool(a.get("trusted_proxies")) or bool(a.get("header_auth"))


def ip_trusted(ip: str, data: dict | None = None) -> bool:
    a = (data or load())["auth"]
    if a.get("header_auth"):
        return True
    try:
        addr = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    for n in a.get("trusted_proxies") or []:
        try:
            if addr in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def raw_store() -> JsonStore:
    return _store
