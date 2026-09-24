"""Several sign-ins per agent CLI, and the rules for choosing one for a turn.

A CLI such as Claude Code or Codex is signed in once per configuration folder, so a second
subscription needs a second folder. Relay keeps one account row per sign-in:

    default     Relay's own home folder, the one every installation already has. Nothing about it
                changes: same HOME, same sign-in, same behaviour as before accounts existed.
    extra       DATA_DIR/agents/accounts/<agent>/<id>, used as HOME (plus the CLI's own config
                variable) for the turns that run on it. The person signs in there once, by hand,
                with the command the Agents page prints: these CLIs only log in interactively.

Each row has a name, an enabled flag (a paused account is skipped) and its own limit state, read
from the CLI with that folder bound (agent_info.bind). One account is the default and is tried first.

Choosing an account for a turn (choose):
    1. the account the role's session already runs on, while it still has capacity — a CLI session
       id lives inside one config folder and cannot be resumed from another,
    2. otherwise the default, if enabled, signed in and under its limits,
    3. otherwise the next enabled account with capacity, in the order they were added,
    4. otherwise nothing, and the caller does what it did before: wait for the reset, switch to a
       fallback agent, or fall back to OpenRouter (autopilot.plan_capacity).

Two turns never share a config folder while another account is free: pick() prefers an account no
turn is using. When every account with capacity is busy, turns share one folder exactly as every
turn does today on a single-account installation.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .util import DATA_DIR, read_json, write_json

ACCOUNTS_FILE = DATA_DIR / "state" / "agent_accounts.json"
ACCOUNTS_DIR = DATA_DIR / "agents" / "accounts"
DEFAULT_ID = "default"
DEFAULT_NAME = "Main account"

# Extra config-folder variables a CLI needs beside HOME, relative to the account folder.
HOME_VARS = {
    "claude": {"CLAUDE_CONFIG_DIR": ".claude"},
    "codex": {"CODEX_HOME": ".codex"},
    "gemini": {"GEMINI_DIR": ".gemini"},
}
# What proves a folder holds a finished sign-in, for the CLIs Relay ships with. Pack agents use the
# files in their own config entry (C.AGENTS[name]["auth"]["files"]).
AUTH_FILES = {
    "claude": [".claude/.credentials.json", ".claude.json", ".config/claude/.credentials.json"],
    "codex": [".codex/auth.json"],
    "gemini": [".gemini/oauth_creds.json", ".gemini/google_accounts.json", ".gemini/.env"],
}
# The command that signs a CLI in. Pack agents carry their own under C.AGENTS[name]["login"].
LOGIN = {"claude": "claude   (then /login)", "codex": "codex login", "gemini": "gemini   (then choose Sign in with Google)"}

_lock = threading.RLock()
_inuse: dict[tuple, int] = {}


# ----------------------------------------------------------------------------- store
def supports(agent: str) -> bool:
    """A CLI whose sign-in is per account, so more than one is meaningful."""
    from . import config as C
    spec = C.AGENTS.get(agent)
    if not spec:
        return False
    return bool(spec.get("login") or agent in LOGIN)


def _all() -> dict:
    return read_json(ACCOUNTS_FILE, {}) or {}


def _save(data: dict) -> None:
    write_json(ACCOUNTS_FILE, data)


def _slug(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:24] or "account"
    out, n = base, 2
    while out in taken or out == DEFAULT_ID:
        out, n = f"{base}-{n}", n + 1
    return out


def home(agent: str, account_id: str) -> Path | None:
    """The folder that holds this account's sign-in; None for the default (Relay's own home)."""
    if not account_id or account_id == DEFAULT_ID:
        return None
    return ACCOUNTS_DIR / agent / account_id


def env_for(agent: str, account_id: str) -> dict:
    """Environment that points a CLI at this account's folder; empty for the default."""
    h = home(agent, account_id)
    if h is None:
        return {}
    out = {"HOME": str(h), "XDG_CONFIG_HOME": str(h / ".config"), "XDG_DATA_HOME": str(h / ".local" / "share")}
    for var, rel in (HOME_VARS.get(agent) or {}).items():
        out[var] = str(h / rel)
    return out


def _auth_files(agent: str) -> list[str]:
    from . import config as C
    return list(AUTH_FILES.get(agent) or ((C.AGENTS.get(agent, {}).get("auth") or {}).get("files") or []))


def signed_in(agent: str, account_id: str, cfg: dict | None = None) -> bool:
    """The default account is judged exactly as before (the adapter's own check); an extra account is
    signed in once its folder holds the CLI's credentials file."""
    if account_id == DEFAULT_ID:
        from .agents import adapter
        try:
            ad = adapter(agent)
            if (ad.spec.get("auth") or {}):
                return ad.signed_in(ad.env(cfg or {}))
        except Exception:
            return True
        return True
    h = home(agent, account_id)
    if h is None:
        return False
    return any((h / f).is_file() for f in _auth_files(agent))


def login_command(agent: str, account_id: str) -> str:
    """The exact command the person runs to sign this account in. These CLIs need an interactive
    login (a browser code paste), so Relay prints the command rather than pretending to automate it."""
    from . import config as C
    cmd = (C.AGENTS.get(agent, {}).get("login") or LOGIN.get(agent) or agent).split("   ")[0]
    h = home(agent, account_id)
    if h is None:
        return cmd
    env = " ".join(f"{k}={v}" for k, v in sorted(env_for(agent, account_id).items()))
    return f"{env} {cmd}"


def rows(agent: str, cfg: dict | None = None) -> list[dict]:
    """Every account of this CLI, default first, in the order they were added."""
    data = (_all().get(agent) or {})
    saved = list(data.get("accounts") or [])
    default_id = data.get("default") or DEFAULT_ID
    known = [{"id": DEFAULT_ID, "name": data.get("default_name") or DEFAULT_NAME, "enabled": data.get("default_enabled", True)}]
    for a in saved:
        if a.get("id") and a["id"] != DEFAULT_ID:
            known.append(a)
    out = []
    for a in known:
        aid = a["id"]
        out.append({"agent": agent, "id": aid, "name": a.get("name") or aid, "enabled": a.get("enabled", True) is not False,
                    "is_default": aid == default_id, "home": str(home(agent, aid) or ""), "added": a.get("added"),
                    "signed_in": signed_in(agent, aid, cfg), "busy": busy(agent, aid),
                    "login_command": login_command(agent, aid)})
    out.sort(key=lambda r: (not r["is_default"], r["added"] or 0))
    return out


def get(agent: str, account_id: str, cfg: dict | None = None) -> dict | None:
    return next((r for r in rows(agent, cfg) if r["id"] == account_id), None)


def multiple(agent: str) -> bool:
    """More than the one account every installation starts with."""
    return len((_all().get(agent) or {}).get("accounts") or []) > 0


def add(agent: str, name: str) -> dict:
    if not supports(agent):
        raise ValueError(f"{agent} does not sign in per account")
    with _lock:
        data = _all()
        entry = data.setdefault(agent, {"default": DEFAULT_ID, "accounts": []})
        taken = {a.get("id") for a in entry["accounts"]}
        aid = _slug(name, taken)
        entry["accounts"].append({"id": aid, "name": (name or "").strip() or aid, "enabled": True, "added": time.time()})
        _save(data)
    h = home(agent, aid)
    h.mkdir(parents=True, exist_ok=True)
    for rel in (HOME_VARS.get(agent) or {}).values():
        (h / rel).mkdir(parents=True, exist_ok=True)
    return get(agent, aid) or {}


def update(agent: str, account_id: str, *, name=None, enabled=None, default=None) -> dict:
    with _lock:
        data = _all()
        entry = data.setdefault(agent, {"default": DEFAULT_ID, "accounts": []})
        if account_id == DEFAULT_ID:
            if name is not None:
                entry["default_name"] = str(name).strip() or DEFAULT_NAME
            if enabled is not None:
                entry["default_enabled"] = bool(enabled)
        else:
            row = next((a for a in entry["accounts"] if a.get("id") == account_id), None)
            if not row:
                raise ValueError(f"No account {account_id} for {agent}")
            if name is not None:
                row["name"] = str(name).strip() or account_id
            if enabled is not None:
                row["enabled"] = bool(enabled)
        if default:
            entry["default"] = account_id
        _save(data)
    return get(agent, account_id) or {}


def remove(agent: str, account_id: str, delete_files: bool = True) -> None:
    if account_id == DEFAULT_ID:
        raise ValueError("The first account cannot be removed; pause it instead.")
    with _lock:
        data = _all()
        entry = data.get(agent) or {}
        entry["accounts"] = [a for a in (entry.get("accounts") or []) if a.get("id") != account_id]
        if entry.get("default") == account_id:
            entry["default"] = DEFAULT_ID
        data[agent] = entry
        _save(data)
    if delete_files:
        import shutil
        shutil.rmtree(home(agent, account_id) or "", ignore_errors=True)


# ----------------------------------------------------------------------------- in use
def busy(agent: str, account_id: str) -> bool:
    with _lock:
        return _inuse.get((agent, account_id), 0) > 0


@contextmanager
def hold(agent: str, account_id: str):
    key = (agent, account_id)
    with _lock:
        _inuse[key] = _inuse.get(key, 0) + 1
    try:
        yield
    finally:
        with _lock:
            _inuse[key] = max(0, _inuse.get(key, 0) - 1)


# ----------------------------------------------------------------------------- choosing
def candidates(account_rows: list[dict]) -> list[dict]:
    """Accounts that could run a turn at all: enabled and signed in."""
    return [r for r in account_rows if r.get("enabled") and r.get("signed_in")]


def choose(account_rows: list[dict], state_of, session_account: str = "") -> dict:
    """Which account runs the next turn.

    account_rows: rows() output, default first.
    state_of:     fn(account_id) -> limit_state dict ({"ok", "reason", "resets_at", "kind"}).
    session_account: the account this role's session already runs on, if any.

    Returns {"account": row|None, "switched": bool, "skipped": [{"id","name","reason"}], "reason": str}.
    `reason` is why nothing could run, when nothing could.
    """
    usable = candidates(account_rows)
    skipped = []
    for r in account_rows:
        if r in usable:
            continue
        why = "paused" if not r.get("enabled") else "not signed in"
        skipped.append({"id": r["id"], "name": r["name"], "reason": why})
    # The session's own account first: its conversation only exists inside that config folder.
    order = []
    if session_account:
        order += [r for r in usable if r["id"] == session_account]
    order += [r for r in usable if r["id"] != session_account]
    free = [r for r in order if not r.get("busy")]
    for pool in (free, order):  # a folder is only shared when no free account has capacity
        for r in pool:
            st = state_of(r["id"]) or {}
            if st.get("ok"):
                return {"account": r, "switched": bool(session_account and r["id"] != session_account),
                        "skipped": skipped, "reason": "", "state": st}
            if not any(s["id"] == r["id"] for s in skipped):
                skipped.append({"id": r["id"], "name": r["name"], "reason": st.get("reason") or "no capacity"})
    reason = "; ".join(dict.fromkeys(f"{s['name']}: {s['reason']}" for s in skipped)) or "no account is signed in"
    return {"account": None, "switched": False, "skipped": skipped, "reason": reason, "state": None}


# ----------------------------------------------------------------------------- the page's view
def policy(agent: str, auto, cfg: dict | None = None, known: list[dict] | None = None) -> dict:
    """What Relay does when this agent runs out, in the order it does it, so the page can say it
    plainly instead of leaving it to be inferred."""
    from . import config as C
    steps, others = [], []
    try:
        s = auto.settings() if auto else {}
    except Exception:
        s = {}
    usable = [r for r in (known if known is not None else rows(agent, cfg)) if r["enabled"] and r["signed_in"]]
    if len(usable) > 1:
        steps.append({"kind": "account", "text": f"move to the next account with capacity ({len(usable)} are signed in and running)"})
    fb = []
    for role, entries in (s.get("fallbacks") or {}).items():
        for e in entries or []:
            from .autopilot import parse_fallback_full
            fa, fm, fp = parse_fallback_full(e)
            if fa and fa != agent:
                fb.append(C.AGENTS.get(fa, {}).get("label", fa) + (" on OpenRouter" if fp == "openrouter" else ""))
            elif fa == agent and fp == "openrouter":
                fb.append(C.AGENTS.get(fa, {}).get("label", fa) + " on OpenRouter")
    fb = list(dict.fromkeys(fb))
    if (s.get("limit_action") or "fallback") == "fallback" and fb:
        steps.append({"kind": "fallback", "text": "switch the role to a fallback: " + ", ".join(fb[:4])})
    steps.append({"kind": "wait", "text": "wait for the window to reset, then start the task"})
    others = usable
    return {"steps": steps, "limit_action": s.get("limit_action") or "fallback",
            "threshold": s.get("limit_threshold_percent") or 90, "usable": len(others)}


def overview(agent: str, cfg: dict, auto=None, tasks=None, refresh: bool = False) -> dict:
    """Every account of one agent with its plan, limits, usage and why it cannot run, for the Agents page.

    Readings come from the cache unless refresh is asked for: opening the page must never wait on a CLI.
    """
    from . import agent_info, config as C
    out = []
    for r in rows(agent, cfg):
        row = dict(r)
        try:
            with agent_info.bind(agent, r["id"]):
                info = agent_info.account(agent, cfg, tasks, refresh=refresh, cached_only=not refresh)
        except Exception as e:
            info = {"plan": [], "windows": [], "usage": [], "notes": [f"Could not read this account: {e}"]}
        row["info"] = info
        row["known"] = not info.get("unread")
        if auto is not None:
            try:
                row["state"] = auto.account_state(agent, r["id"], reading=info)
            except Exception as e:
                row["state"] = {"ok": None, "reason": str(e), "resets_at": None, "kind": ""}
            # The built-in CLIs keep no credentials file Relay can point at, so the default account
            # counts as signed in until the CLI itself says otherwise. An extra account is judged by
            # its own folder and keeps the answer signed_in() gave.
            if r["id"] == DEFAULT_ID and re.search(r"not signed in|not logged in", (row["state"] or {}).get("reason") or "", re.I):
                row["signed_in"] = False
        out.append(row)
    return {"agent": agent, "label": C.AGENTS.get(agent, {}).get("label", agent), "accounts": out,
            "policy": policy(agent, auto, cfg, out), "multiple": multiple(agent)}


def mark_run(run_dir, agent: str, account_id: str) -> None:
    """Note beside the run's raw log which account took the turn, so a limit reading scraped from that
    log later (Claude streams its utilization there) is attributed to the right account."""
    try:
        p = Path(run_dir) / "agent-accounts.json"
        data = read_json(p, {}) or {}
        data[agent] = account_id
        write_json(p, data)
    except Exception:
        pass


def run_account(run_dir, agent: str) -> str:
    """Which account a run used; older runs pre-date accounts and were all the default one."""
    return (read_json(Path(run_dir) / "agent-accounts.json", {}) or {}).get(agent) or DEFAULT_ID
