"""Personal settings: one person's own defaults, on top of the organisation's.

Relay has a single shared configuration (orchestrator/config.py). Some of it describes how *this*
person likes to work — the default team, the workflow gates a new task starts from — and some of it
is infrastructure that spends money or grants access, which stays organisation-wide and owner/admin
only. This module is the one place that knows the difference and the one place that resolves it.

    precedence:  what the task itself says  →  the person's own setting  →  the organisation default

`effective(cfg, user)` applies the middle step; the outer two are applied by the caller
(orchestrator/manager.py build_workflow reads the task's own workflow over the resolved defaults).
Nothing re-derives this in the browser: the settings a browser is given already come resolved
(orchestrator/org/web.py `_config_for`).

A person's overrides live in their existing preferences record (orchestrator/org/identity.py,
prefs["settings"]), so there is no second store. Only the keys in KEYS may be written there, and
every value is validated here before it is stored, so a request body can never reach a shared
setting through the personal endpoint. No secret is a personal key, so nothing secret is stored or
logged by this module.
"""
from __future__ import annotations

import copy

from . import config as C

# What a person may decide for themselves. Everything not listed stays shared: provider keys and
# OpenRouter, spend caps and budgets, connectors, repositories, the redeploy command and its master
# switch, sign-in and role mapping, and every other key in config.DEFAULTS.
KEYS = (
    "workflow_preset", "roles", "max_turns", "max_review_rounds", "verify_mode",
    "approval_before_delivery", "allow_agent_questions", "team_mode",
    "design_mode", "design_approval", "auto_detect_verification",
    "ui_notifications", "ui_sound", "ui_notify_events",
)

LABELS = {
    "workflow_preset": "Default team",
    "roles": "Agents, models and efforts",
    "max_turns": "Turn budget",
    "max_review_rounds": "Review rounds",
    "verify_mode": "Verification",
    "approval_before_delivery": "Approval before delivery",
    "allow_agent_questions": "Agent questions",
    "team_mode": "Team mode",
    "design_mode": "Design step",
    "design_approval": "Design approval",
    "auto_detect_verification": "Detect verification commands",
    "ui_notifications": "Desktop alerts",
    "ui_sound": "Sound",
    "ui_notify_events": "Which events may alert you",
}

CHOICES = {
    "verify_mode": ("each_report", "before_review", "off"),
    "team_mode": ("auto", "solo", "team"),
    "design_mode": ("auto", "always", "never"),
    "design_approval": ("auto", "on", "off"),
}
BOOLS = ("approval_before_delivery", "allow_agent_questions", "auto_detect_verification", "ui_notifications", "ui_sound")
NUMBERS = {"max_turns": (1, 200), "max_review_rounds": (1, 20)}

_actor_provider = None


def set_actor_provider(fn):
    """The organisation layer tells this module how to find who is acting (request or Telegram)."""
    global _actor_provider
    _actor_provider = fn


def actor() -> dict | None:
    if not _actor_provider:
        return None
    try:
        return _actor_provider()
    except Exception:
        return None


def _user(user) -> dict | None:
    """A user dict, a username, or None for 'whoever is acting right now'."""
    if user is None:
        user = actor()
    if isinstance(user, str):
        from .org import identity
        return identity.get(user)
    return user or None


def resolve_user(user=None) -> dict | None:
    """Who a resolution is for: a user dict, a username, or None for whoever is acting right now."""
    return _user(user)


def overrides(user=None) -> dict:
    """The keys this person has decided for themselves. Unknown keys are ignored, never trusted."""
    u = _user(user)
    if not u:
        return {}
    stored = ((u.get("prefs") or {}).get("settings") or {})
    return {k: copy.deepcopy(v) for k, v in stored.items() if k in KEYS}


def effective(cfg: dict, user=None) -> dict:
    """The organisation's settings with this person's own values on top. Other keys are untouched."""
    own = overrides(user)
    if not own:
        return cfg
    out = dict(cfg)
    for k, v in own.items():
        if k == "roles":
            roles = copy.deepcopy(cfg.get("roles") or {})
            for r, rv in (v or {}).items():
                roles[r] = {**(roles.get(r) or {}), **rv}
            out["roles"] = roles
        else:
            out[k] = copy.deepcopy(v)
    return out


def sources(cfg: dict, user=None) -> dict:
    """Where each personal-capable value in effect comes from: "yours" or "organisation"."""
    own = overrides(user)
    return {k: ("yours" if k in own else "organisation") for k in KEYS}


def view(cfg: dict, user=None) -> dict:
    """Everything a settings page needs to say what is in effect and where it comes from."""
    org = {k: copy.deepcopy(cfg.get(k)) for k in KEYS}
    eff = effective(cfg, user)
    return {"keys": list(KEYS), "labels": dict(LABELS), "personal": overrides(user),
            "organisation": org, "effective": {k: copy.deepcopy(eff.get(k)) for k in KEYS},
            "sources": sources(cfg, user)}


# ---------------------------------------------------------------------------- validation
def _role_value(r: str, v) -> dict:
    if not isinstance(v, dict):
        raise ValueError(f"The {r} role must be an agent, a model and an effort.")
    out = {}
    agent = str(v.get("agent") or "").strip()
    if agent and agent not in C.AGENTS:
        raise ValueError(f"Unknown agent “{agent}” for {r}.")
    out["agent"] = agent
    out["model"] = str(v.get("model") or "").strip()[:200]
    effort = str(v.get("effort") or "").strip().lower()
    allowed = (C.AGENTS.get(agent) or {}).get("efforts") or []
    if effort and allowed and effort not in allowed:
        raise ValueError(f"Effort “{effort}” is not valid for {agent} (choose {', '.join(allowed)}).")
    out["effort"] = effort if allowed else ""
    provider = str(v.get("provider") or "").strip().lower()
    if provider not in ("", "openrouter"):
        raise ValueError(f"Unknown provider “{provider}” for {r} (use openrouter or leave it empty).")
    out["provider"] = provider
    return out


def clean(patch: dict) -> dict:
    """Validate one patch of personal settings. A value of None clears the key back to the organisation's.

    Raises ValueError naming the key when it is not a personal setting, so nobody can reach a shared
    setting — a provider key, a spend cap, the redeploy command — through the personal endpoint.
    """
    out = {}
    for k, v in (patch or {}).items():
        if k not in KEYS:
            raise ValueError(f"“{k}” is an organisation setting. Only an owner or admin can change it, in Settings.")
        if v is None:
            out[k] = None
            continue
        if k == "roles":
            if not isinstance(v, dict):
                raise ValueError("Roles must be given per role.")
            out[k] = {r: _role_value(r, rv) for r, rv in v.items() if r in C.ROLES}
        elif k == "workflow_preset":
            pid = str(v or "").strip()
            if pid and pid != "custom" and not C.preset(pid):
                raise ValueError(f"Unknown team preset “{pid}”.")
            out[k] = pid or "custom"
        elif k in CHOICES:
            if str(v) not in CHOICES[k]:
                raise ValueError(f"{LABELS[k]} must be one of {', '.join(CHOICES[k])}.")
            out[k] = str(v)
        elif k in BOOLS:
            out[k] = bool(v)
        elif k in NUMBERS:
            lo, hi = NUMBERS[k]
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{LABELS[k]} must be a number.")
            out[k] = max(lo, min(hi, n))
        elif k == "ui_notify_events":
            if not isinstance(v, dict):
                raise ValueError("Alert events must be given per event.")
            known = C.DEFAULTS.get("ui_notify_events") or {}
            out[k] = {e: bool(x) for e, x in v.items() if e in known}
        else:  # pragma: no cover - every key above is covered
            raise ValueError(f"“{k}” cannot be set personally.")
    return out


def save(username: str, patch: dict) -> dict:
    """Apply a validated patch to one person's overrides. Returns what they now override."""
    from .org import identity
    return identity.save_settings(username, clean(patch))


def clear(username: str, keys=None) -> dict:
    """Drop one, several or every personal override, back to the organisation default."""
    from .org import identity
    if keys is None:
        return identity.save_settings(username, None)
    bad = [k for k in keys if k not in KEYS]
    if bad:
        raise ValueError(f"“{bad[0]}” is not a personal setting.")
    return identity.save_settings(username, {k: None for k in keys})
