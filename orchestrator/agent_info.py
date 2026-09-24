"""Models, plan and usage for every agent, from the best source each CLI offers.

Each agent answers two questions for the Models & usage dialog:

  models(name)   what it can run: id, name, price per 1M tokens in/out, free, included in the
                 signed-in plan, context window, reasoning. Sources, best first:
                   the CLI itself          kilo/opencode `models --verbose`, cursor-agent `models`
                   the CLI's own cache     ~/.codex/models_cache.json, crush providers.json
                   the vendor API          Copilot /models (premium multipliers, plan policy),
                                           Gemini /models when an API key is in Relay's settings
                   a shared catalog        models.dev (what OpenCode and Kilo use), aider's litellm table
                   Relay settings          the editable list, when nothing else answers
  account(name)  plan or tier, balance, quota windows with reset times, CLI usage totals, and
                 what Relay itself recorded for the agent (turns, tokens, cost for 24h/7d/30d).

Nothing here may hang or break the page: CLIs run with stdin closed, CI=1, no browser, in their
own process group with a timeout; network calls time out; every source is wrapped so one failure
becomes a note instead of an error. Model lists are cached for 6 hours, account data for 5 minutes.
Secrets never leave this module: tokens are only used as request headers and e-mail addresses are dropped.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from .util import DATA_DIR, RUNTIME_DIR, read_json, write_json

CACHE_DIR = DATA_DIR / "cache" / "agents"
MODELS_TTL = 6 * 3600
ACCOUNT_TTL = 5 * 60
CATALOG_URL = "https://models.dev/api.json"

# agent -> CLI subcommands that describe it
SOURCES = {
    "kilo": {"models": ["models", "kilo", "--verbose"], "profile": ["profile"], "stats": ["stats"]},
    "opencode": {"models": ["models", "--verbose"], "stats": ["stats"]},
}

# models.dev providers to offer when an agent has no list of its own, and the model id format its CLI takes.
CATALOG_FALLBACK = {
    "cline": (["anthropic", "openai", "openrouter"], "{provider}:{id}"),
    "goose": (["anthropic", "openai", "google", "openrouter"], "{provider}:{id}"),
    "qwen": (["alibaba"], "{id}"),
    "continue": (["anthropic"], "{id}"),
}
PROVIDER_ALIAS = {"cline": {"google": "gemini"}}

_account_cache: dict[str, tuple[float, dict]] = {}
_status_cache: dict[str, tuple[float, dict]] = {}
_catalog_lock = threading.Lock()
# Which account's config folder this thread reads (orchestrator/accounts.py). Unset means the default
# account, which is Relay's own home folder: exactly what every reading used before accounts existed.
_bound = threading.local()


@contextmanager
def bind(agent: str, account_id: str):
    """Read one account of an agent: its folder stands in for HOME while this block runs, and every
    cache key is that account's own, so two accounts never read each other's plan or limits."""
    from . import accounts
    prev = (getattr(_bound, "id", None), getattr(_bound, "env", None))
    aid = account_id or accounts.DEFAULT_ID
    _bound.id, _bound.env = aid, accounts.env_for(agent, aid)
    try:
        yield
    finally:
        _bound.id, _bound.env = prev


def bound_id() -> str:
    return getattr(_bound, "id", None) or "default"


def _key(name: str) -> str:
    """Cache key of the account being read; the default account keeps the plain agent name it always had."""
    aid = bound_id()
    return name if aid == "default" else f"{name}@{aid}"


# ----------------------------------------------------------------------------- helpers
def _home(env: dict | None = None) -> Path:
    if env is None:
        overlay = getattr(_bound, "env", None) or {}
        if overlay.get("HOME"):
            return Path(overlay["HOME"])
    return Path((env or os.environ).get("HOME") or Path.home())


def _env(name: str, cfg: dict) -> dict:
    from .agents import adapter
    env = adapter(name).env(cfg)
    env.update(getattr(_bound, "env", None) or {})
    # Never wait for a login prompt or open a browser from a web request.
    env.update({"CI": "1", "NO_BROWSER": "1", "NO_OPEN_BROWSER": "1", "BROWSER": "true", "TERM": "dumb",
                "NO_COLOR": "1", "FORCE_COLOR": "0", "GIT_TERMINAL_PROMPT": "0"})
    return env


def _run(name: str, args: list[str], cfg: dict, timeout=60) -> str:
    """Run the agent's CLI non-interactively; stdout+stderr, or '' when it is missing. Killed on timeout."""
    env = _env(name, cfg)
    exe = shutil.which(C.AGENTS[name]["binary"], path=env.get("PATH"))
    if not exe:
        return ""
    work = CACHE_DIR / "cwd"
    work.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.Popen([exe, *args], cwd=str(work), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                             start_new_session=os.name != "nt")
    except OSError:
        return ""
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except Exception:
            pass
        try:
            out, _ = p.communicate(timeout=5)
        except Exception:
            out = ""
    out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out or "")
    # INFO log lines from the CLI's logger are noise here.
    return "\n".join(l for l in out.splitlines() if not re.match(r"^\s*(INFO|DEBUG|WARN)\s+\d{4}-", l))


def _http_json(url: str, headers: dict | None = None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"Relay/{C.BUILD}", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _cached(key: str, ttl: float, fn, refresh=False):
    """(value, fetched_at) from a file cache under CACHE_DIR; a failed refresh keeps serving the stale copy."""
    path = CACHE_DIR / f"{key}.json"
    data = read_json(path, None)
    if data and not refresh and time.time() - float(data.get("at") or 0) < ttl:
        return data["value"], data["at"]
    try:
        value = fn()
    except Exception:
        value = None
    if value:
        now = time.time()
        write_json(path, {"at": now, "value": value})
        return value, now
    return (data["value"], data["at"]) if data else (None, None)


def _cached_status(key, fn, refresh=False):
    at, v = _status_cache.get(key, (0, None))
    if v is None or refresh or time.time() - at > ACCOUNT_TTL:
        v = fn() or {}
        _status_cache[key] = (time.time(), v)
    return v


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _row(mid, name=None, input=None, output=None, free=False, context=None, reasoning=False, **extra):
    r = {"id": mid, "name": name or mid, "input": _num(input), "output": _num(output), "free": bool(free),
         "context": int(context) if context else None, "reasoning": bool(reasoning)}
    r.update({k: v for k, v in extra.items() if v is not None})
    return r


def _tail(path: Path, size=4_000_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - size))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _ts(v) -> float | None:
    """Epoch seconds from epoch seconds/milliseconds or an ISO string."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window_label(minutes) -> str:
    m = int(minutes or 0)
    if m == 300:
        return "5-hour limit"
    if m == 10080:
        return "Weekly limit"
    if m and m % 1440 == 0:
        return f"{m // 1440}-day limit"
    if m and m % 60 == 0:
        return f"{m // 60}-hour limit"
    return f"{m}-minute limit" if m else "Limit"


def _no_email(rows):
    """Personal details (e-mail, name) are not needed to judge an account and stay out of the page."""
    return [r for r in rows if not re.search(r"e-?mail|^name$|^user(name)?$", r["label"], re.I) and "@" not in str(r["value"])]


def _kv_lines(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z][\w /&()+.-]{1,40}):\s+(.+?)\s*$", line)
        if m:
            rows.append({"label": m.group(1).strip(), "value": m.group(2)})
    return rows


def _box_lines(text: str) -> list[dict]:
    """Rows of the boxed tables `stats` prints: │Label        value │"""
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*│\s*(\S.*?\S)\s{2,}(\S.*?)\s*│\s*$", line)
        if m:
            rows.append({"label": m.group(1), "value": m.group(2)})
    return rows


def _signed_in(name, cfg) -> bool:
    from .agents import adapter
    try:
        return adapter(name).signed_in(_env(name, cfg))
    except Exception:
        return False


# ----------------------------------------------------------------------------- shared catalog
def catalog(refresh=False) -> dict:
    """models.dev, the open catalog OpenCode and Kilo read. Cached; falls back to their local copies."""
    with _catalog_lock:
        value, _ = _cached("models-dev", MODELS_TTL, lambda: _http_json(CATALOG_URL, timeout=30), refresh)
    if value:
        return value
    for rel in (".cache/opencode/models.json", ".cache/kilo/models.json"):
        data = read_json(_home() / rel, None)
        if isinstance(data, dict) and data:
            return data
    return {}


def _catalog_rows(providers: list[str], fmt: str, alias: dict | None = None, keep=None) -> list[dict]:
    cat = catalog()
    rows = []
    for pid in providers:
        for mid, d in (((cat.get(pid) or {}).get("models")) or {}).items():
            if "text" not in (((d.get("modalities") or {}).get("output")) or ["text"]) or d.get("tool_call") is False:
                continue
            if re.search(r"embed|tts|image|realtime|live|veo|lyria|search|audio|transcribe|computer-use", mid) or (keep and not keep(mid)):
                continue
            cost = d.get("cost") or {}
            rows.append(_row(fmt.format(provider=(alias or {}).get(pid, pid), id=mid), d.get("name"), cost.get("input"), cost.get("output"),
                             free=bool(cost) and not _num(cost.get("input")) and not _num(cost.get("output")),
                             context=(d.get("limit") or {}).get("context"), reasoning=d.get("reasoning"),
                             status="deprecated" if d.get("status") == "deprecated" else None))
    return rows


def _catalog_price(provider: str, mid: str) -> dict:
    d = (((catalog().get(provider) or {}).get("models")) or {}).get(mid) or {}
    cost = d.get("cost") or {}
    return {"input": cost.get("input"), "output": cost.get("output"), "context": (d.get("limit") or {}).get("context")}


# ----------------------------------------------------------------------------- Relay's own records
PERIODS = (("24 hours", 86400), ("7 days", 7 * 86400), ("30 days", 30 * 86400))


def relay_usage(name: str, tasks: list[dict] | None) -> dict:
    """Turns, tokens and cost Relay recorded for this agent, from each task's turn log."""
    now = time.time()
    periods = [{"label": label, "turns": 0, "tasks": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0,
                "estimated": False, "_t": set()} for label, _ in PERIODS]
    total = {"label": "All time", "turns": 0, "tasks": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0, "estimated": False}
    last = None
    for t in tasks or []:
        m = t.get("metrics") or {}
        a = (m.get("agents") or {}).get(name)
        if a:
            total["tasks"] += 1
            for k in ("turns", "input", "output", "cached"):
                total[k] += int(a.get(k) or 0)
            total["cost_usd"] += float(a.get("cost_usd") or 0)
            total["estimated"] = total["estimated"] or bool(a.get("estimated"))
        for e in m.get("log") or []:
            if e.get("agent") != name:
                continue
            end = float(e.get("end") or 0)
            last = max(last or 0, end)
            for p, (_, span) in zip(periods, PERIODS):
                if now - end <= span:
                    p["turns"] += 1
                    p["_t"].add(t.get("id"))
                    for k in ("input", "output", "cached"):
                        p[k] += int(e.get(k) or 0)
                    p["cost_usd"] += float(e.get("cost_usd") or 0)
                    p["estimated"] = p["estimated"] or bool(e.get("estimated"))
    for p in periods:
        p["tasks"] = len(p.pop("_t"))
        p["cost_usd"] = round(p["cost_usd"], 4)
    total["cost_usd"] = round(total["cost_usd"], 4)
    return {"periods": periods + [total], "last_used": last}


def _raw_logs(limit=30) -> list[Path]:
    try:
        files = [p / "raw.log" for p in RUNTIME_DIR.iterdir() if (p / "raw.log").is_file()]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return []


# ----------------------------------------------------------------------------- Claude
def _claude_status(cfg) -> dict:
    """`claude auth status --json`: login method and subscription type (e-mail and org ids are dropped)."""
    m = re.search(r"\{.*\}", _run("claude", ["auth", "status", "--json"], cfg, timeout=30), re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    return {k: d.get(k) for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType", "orgName")}


def _claude_rate_limits() -> dict | None:
    """The newest rate_limit_event Claude Code streamed in any task: utilization and reset per window.

    A run notes which account took its turns (accounts.mark_run), so a reading only counts for the
    account it was measured on; runs from before accounts existed all used the default one."""
    from . import accounts
    want = bound_id()
    for log in _raw_logs():
        if accounts.run_account(log.parent, "claude") != want:
            continue
        text = _tail(log)
        idx = text.rfind('"rate_limit_event"')
        if idx < 0:
            continue
        line = text[text.rfind("\n", 0, idx) + 1:]
        line = line.split("\n", 1)[0]
        m = re.search(r"\{.*\}", line)
        try:
            info = json.loads(m.group(0)).get("rate_limit_info") or {}
        except (ValueError, AttributeError):
            continue
        return {"info": info, "seen": log.stat().st_mtime}
    return None


def _claude_models(cfg, refresh=False) -> dict:
    status = _cached_status(_key("claude"), lambda: _claude_status(cfg), refresh)
    sub = status.get("authMethod") == "claude.ai" or bool(status.get("subscriptionType"))
    rows = _catalog_rows(["anthropic"], "{id}", keep=lambda mid: mid.startswith("claude-") and not re.search(r"-\d{8}$", mid))
    for alias in ("fable", "opus", "sonnet", "haiku"):
        # An alias runs the family's newest model, so it costs what that model costs.
        family = sorted((r for r in rows if r["id"].startswith(f"claude-{alias}-")), key=lambda r: [int(x) if x.isdigit() else 0 for x in r["id"].split("-")[2:]])
        newest = family[-1] if family else {}
        rows.append(_row(alias, f"{alias.title()} (latest)", newest.get("input"), newest.get("output"), context=newest.get("context"),
                         reasoning=newest.get("reasoning"), note=f"alias for {newest['id']}" if newest else "alias for the newest model"))
    for r in rows:
        r["included"] = sub
    plan = (status.get("subscriptionType") or "").title()
    return {"source": "catalog", "source_label": "models.dev catalog and Claude Code aliases", "models": rows,
            "plan_note": (f"Included in your Claude {plan} plan" if plan else "Included in your Claude subscription") +
                         "; prices are the API equivalent." if sub else "Billed per token to your API key."}


def _claude_account(cfg, out, refresh=False):
    status = _cached_status(_key("claude"), lambda: _claude_status(cfg), refresh)
    if status.get("loggedIn"):
        method = {"claude.ai": "Claude account", "console": "Anthropic Console", "apiKey": "API key"}.get(status.get("authMethod"), status.get("authMethod") or "")
        out["plan"] = [p for p in (
            {"label": "Plan", "value": "Claude " + status["subscriptionType"].title() if status.get("subscriptionType") else method},
            {"label": "Signed in with", "value": method},
        ) if p["value"]]
    elif status:
        out["plan"] = [{"label": "Status", "value": "Not signed in"}]
    rl = _claude_rate_limits()
    if not rl:
        out["notes"].append("No limit reading yet: Claude Code reports its 5-hour and weekly utilization while a task runs.")
        return
    info = rl["info"]
    labels = {"five_hour": "5-hour limit", "seven_day": "Weekly limit", "seven_day_opus": "Weekly Opus limit", "seven_day_sonnet": "Weekly Sonnet limit"}
    windows = info.get("unifiedWindows") or {}
    for key, w in windows.items():
        util = _num(w.get("utilization"))
        out["windows"].append({"label": labels.get(key, key.replace("_", " ").capitalize()),
                               "used": round(util * 100, 1) if util is not None and util <= 1 else util,
                               "resets_at": _ts(w.get("resetsAt")), "as_of": rl["seen"]})
    if not windows and info.get("rateLimitType"):
        out["windows"].append({"label": labels.get(info["rateLimitType"], info["rateLimitType"]), "used": None,
                               "resets_at": _ts(info.get("resetsAt")), "detail": info.get("status"), "as_of": rl["seen"]})
    if info.get("status") and info["status"] != "allowed":
        out["usage"].append({"label": "Limit status", "value": info["status"].replace("_", " ")})
    if info.get("overageStatus"):
        reason = {"org_level_disabled": "turned off for the organization", "out_of_credits": "no credits left"}.get(info.get("overageDisabledReason"), (info.get("overageDisabledReason") or "").replace("_", " "))
        value = "in use" if info.get("isUsingOverage") else ("available" if info["overageStatus"] == "allowed" else "off" + (f", {reason}" if reason else ""))
        out["usage"].append({"label": "Extra usage beyond the plan", "value": value})


# ----------------------------------------------------------------------------- Codex
def _codex_home(env) -> Path:
    return Path(env.get("CODEX_HOME") or _home(env) / ".codex")


def _codex_chatgpt(cfg, refresh=False) -> bool | None:
    text = _cached_status(_key("codex"), lambda: {"text": _run("codex", ["login", "status"], cfg, timeout=20)}, refresh).get("text", "")
    if "ChatGPT" in text:
        return True
    return False if re.search(r"API key", text, re.I) else None


def _codex_models(cfg, refresh=False) -> dict:
    home = _codex_home(_env("codex", cfg))
    cache = read_json(home / "models_cache.json", {}) or {}
    chatgpt = _codex_chatgpt(cfg, refresh)
    try:
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', (home / "config.toml").read_text(encoding="utf-8"), re.M)
        default = m.group(1) if m else ""
    except OSError:
        default = ""
    rows = []
    for d in cache.get("models") or []:
        if d.get("visibility") not in (None, "list"):
            continue
        slug = d.get("slug") or ""
        price = _catalog_price("openai", slug)
        up = d.get("upgrade") or {}
        rows.append(_row(slug, d.get("display_name"), price["input"], price["output"],
                         context=d.get("context_window") or price["context"], reasoning=bool(d.get("supported_reasoning_levels")),
                         included=bool(chatgpt), efforts=[x.get("effort") for x in d.get("supported_reasoning_levels") or []],
                         note=f"retires {up['retirement_at'][:10]}" if up.get("retirement_at") else None,
                         cli_default=True if slug == default else None))
    note = "Included in your ChatGPT plan; prices are the API equivalent." if chatgpt else "Billed per token to your OpenAI API key."
    if rows:
        return {"source": "cli", "source_label": "Codex's model list", "fetched": _ts(cache.get("fetched_at")), "models": rows, "plan_note": note}
    return {"source": "catalog", "source_label": "models.dev catalog", "plan_note": note,
            "models": _catalog_rows(["openai"], "{id}", keep=lambda x: x.startswith("gpt-5") or x.startswith("gpt-6"))}


def _codex_rate_limits(env) -> dict | None:
    """The newest rate_limits snapshot Codex wrote to its session logs (token_count events)."""
    try:
        files = sorted((_codex_home(env) / "sessions").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    except OSError:
        return None
    for f in files:
        for line in reversed(_tail(f, 2_000_000).splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rl = (d.get("payload") or {}).get("rate_limits") or d.get("rate_limits")
            if rl:
                return {"limits": rl, "seen": _ts(d.get("timestamp")) or f.stat().st_mtime}
    return None


def _codex_account(cfg, out, refresh=False):
    chatgpt = _codex_chatgpt(cfg, refresh)
    status_text = _cached_status(_key("codex"), lambda: {"text": _run("codex", ["login", "status"], cfg, timeout=20)}, refresh).get("text", "")
    if re.search(r"not logged in", status_text, re.I):
        out["plan"].append({"label": "Status", "value": "Not signed in"})
    rl = _codex_rate_limits(_env("codex", cfg))
    lim = (rl or {}).get("limits") or {}
    if lim.get("plan_type"):
        out["plan"].append({"label": "Plan", "value": f"ChatGPT {lim['plan_type'].title()}"})
    if chatgpt is not None:
        out["plan"].append({"label": "Signed in with", "value": "ChatGPT" if chatgpt else "API key"})
    if not rl:
        out["notes"].append("No limit reading yet: Codex writes its 5-hour and weekly usage to its session logs after a turn.")
        return
    for key in ("primary", "secondary"):
        w = lim.get(key) or {}
        if w:
            out["windows"].append({"label": _window_label(w.get("window_minutes")), "used": _num(w.get("used_percent")),
                                   "resets_at": _ts(w.get("resets_at")), "as_of": rl["seen"]})
    cr = lim.get("credits") or {}
    if cr.get("unlimited"):
        out["plan"].append({"label": "Credits", "value": "unlimited"})
    elif cr.get("has_credits") or _num(cr.get("balance")):
        out["plan"].append({"label": "Credits", "value": str(cr.get("balance"))})
    if lim.get("rate_limit_reached_type"):
        out["usage"].append({"label": "Limit reached", "value": str(lim["rate_limit_reached_type"]).replace("_", " ")})


# ----------------------------------------------------------------------------- GitHub Copilot
COPILOT_PLANS = {"free_limited_copilot": "Copilot Free", "copilot_pro": "Copilot Pro", "copilot_pro_plus": "Copilot Pro+",
                 "copilot_for_business": "Copilot Business", "copilot_enterprise": "Copilot Enterprise"}


def _gh_token(cfg) -> str:
    """The GitHub token the Copilot CLI signs in with: its env variables, else the gh CLI login."""
    env = _env("copilot", cfg)
    for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if env.get(k):
            return env[k]
    gh = shutil.which("gh", path=env.get("PATH"))
    if not gh:
        return ""
    try:
        p = subprocess.run([gh, "auth", "token"], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _copilot_user(cfg) -> dict:
    """Plan and quota snapshot, as the Copilot CLI reads it; its on-disk cache when GitHub is unreachable."""
    token = _gh_token(cfg)
    if token:
        try:
            return _http_json("https://api.github.com/copilot_internal/user", {"Authorization": f"token {token}"})
        except Exception:
            pass
    try:
        text = (_home(_env("copilot", cfg)) / ".cache/copilot/copilot-user-cache.json").read_text(encoding="utf-8")
        data = json.loads("\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//")))
        entries = sorted((data.get("copilotUserCache") or {}).values(), key=lambda e: e.get("retrievedAt") or "", reverse=True)
        return (entries[0].get("response") or {}) if entries else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _copilot_models(cfg, refresh=False) -> dict:
    token = _gh_token(cfg)
    if not token:
        return {"source": "settings", "error": "GitHub Copilot is not signed in, so its model list is unavailable."}
    user = _cached_status(_key("copilot-user"), lambda: _copilot_user(cfg), refresh)
    api = ((user.get("endpoints") or {}).get("api")) or "https://api.githubcopilot.com"
    data, at = _cached("copilot-models", MODELS_TTL, lambda: _http_json(
        f"{api}/models", {"Authorization": f"Bearer {token}", "Copilot-Integration-Id": "copilot-developer-cli",
                          "X-GitHub-Api-Version": "2025-10-01"}).get("data"), refresh)
    rows = []
    for d in data or []:
        caps = d.get("capabilities") or {}
        if caps.get("type") != "chat" or not (d.get("policy") or d.get("model_picker_category")):
            continue  # embeddings and internal helpers (search, compaction)
        billing = d.get("billing") or {}
        enabled = (d.get("policy") or {}).get("state", "enabled") == "enabled"
        mult = _num(billing.get("multiplier"))
        premium = bool(billing.get("is_premium")) and mult != 0
        supports = caps.get("supports") or {}
        rows.append(_row(d["id"], d.get("name"), context=(caps.get("limits") or {}).get("max_context_window_tokens"),
                         reasoning=bool(supports.get("reasoning_effort") or supports.get("max_thinking_budget")),
                         included=enabled, available=enabled, multiplier=mult if premium else None,
                         note=None if enabled else "not on your plan"))
    if not rows:
        return {"source": "settings", "error": "GitHub Copilot did not return a model list."}
    return {"source": "api", "source_label": "GitHub Copilot model list", "fetched": at, "models": rows,
            "plan_note": "Copilot counts requests against your plan, not tokens: ×N is how many premium requests one prompt uses."}


def _copilot_account(cfg, out, refresh=False):
    user = _copilot_user(cfg)
    _status_cache["copilot-user"] = (time.time(), user)
    if not user:
        out["notes"].append("GitHub Copilot did not return plan details. Is it signed in?")
        return
    sku = user.get("access_type_sku") or ""
    out["plan"].append({"label": "Plan", "value": COPILOT_PLANS.get(sku) or (user.get("copilot_plan") or sku).replace("_", " ").title()})
    reset = _ts(user.get("quota_reset_date_utc") or user.get("quota_reset_date"))
    labels = {"premium_interactions": "Premium requests", "chat": "Chat messages", "completions": "Code completions"}
    for key, q in (user.get("quota_snapshots") or {}).items():
        label = labels.get(key, key.replace("_", " ").capitalize())
        if q.get("unlimited"):
            out["windows"].append({"label": label, "used": None, "detail": "unlimited"})
            continue
        ent = _num(q.get("entitlement")) or 0
        if not ent:
            out["windows"].append({"label": label, "used": None, "detail": "none included in this plan"})
            continue
        left = _num(q.get("remaining")) or 0
        out["windows"].append({"label": label, "used": round(max(0.0, ent - left) / ent * 100, 1), "resets_at": reset,
                               "detail": f"{left:g} of {ent:g} left" + (f", {q['overage_count']} over" if q.get("overage_count") else "")})
    # Premium requests the CLI recorded in its sessions on this server since the period started.
    since = (reset - 31 * 86400) if reset else time.time() - 30 * 86400
    total, sessions = 0.0, 0
    for ev in (_home(_env("copilot", cfg)) / ".copilot" / "session-state").glob("*/events.jsonl"):
        try:
            if ev.stat().st_mtime < since:
                continue
        except OSError:
            continue
        found = [float(x) for x in re.findall(r'"totalPremiumRequests":\s*([\d.]+)', _tail(ev, 1_000_000))]
        if found and max(found):
            total += max(found)
            sessions += 1
    out["usage"].append({"label": "Premium requests used on this server this period", "value": f"{total:g} in {sessions} session{'' if sessions == 1 else 's'}"})


# ----------------------------------------------------------------------------- Gemini
GEMINI_FREE_RE = re.compile(r"Quota exceeded for metric: [\w./]*free_tier[\w]*, limit: (\d+), model: ([\w.-]+)")


def _gemini_auth(cfg) -> str:
    s = read_json(_home(_env("gemini", cfg)) / ".gemini" / "settings.json", {}) or {}
    return (((s.get("security") or {}).get("auth") or {}).get("selectedType")) or s.get("selectedAuthType") or ""


def _gemini_models(cfg, refresh=False) -> dict:
    env = _env("gemini", cfg)
    key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
    auth = _gemini_auth(cfg)
    note = {"gemini-api-key": "Free-tier keys pay nothing but have daily request limits per model; paid-tier keys are billed these prices.",
            "oauth-personal": "Signed in with Google: Gemini Code Assist quotas apply instead of these prices."}.get(auth, "")
    if key:
        data, at = _cached("gemini-models", MODELS_TTL, lambda: _http_json(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", {"x-goog-api-key": key}).get("models"), refresh)
        rows = []
        for d in data or []:
            mid = (d.get("name") or "").split("/")[-1]
            if "generateContent" in (d.get("supportedGenerationMethods") or []) and mid.startswith("gemini") and not re.search(r"embed|tts|image|live|computer-use", mid):
                price = _catalog_price("google", mid)
                rows.append(_row(mid, d.get("displayName"), price["input"], price["output"], context=d.get("inputTokenLimit"), reasoning=d.get("thinking")))
        if rows:
            return {"source": "api", "source_label": "Gemini API model list", "fetched": at, "models": rows, "plan_note": note}
    rows = _catalog_rows(["google"], "{id}", keep=lambda mid: mid.startswith("gemini-"))
    return {"source": "catalog", "source_label": "models.dev catalog", "models": rows, "plan_note": note}


def _next_pacific_midnight(ts: float) -> float:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        tz = timezone(timedelta(hours=-8))
    d = datetime.fromtimestamp(ts, tz)
    return (d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def _gemini_account(cfg, out, refresh=False):
    auth = _gemini_auth(cfg)
    label = {"gemini-api-key": "Gemini API key", "oauth-personal": "Google account", "vertex-ai": "Vertex AI",
             "compute-default-credentials": "Cloud credentials"}.get(auth, auth)
    if label:
        out["plan"].append({"label": "Signed in with", "value": label})
    # A free-tier key shows itself when it runs out: the 429 names the free_tier quota, its limit and the model.
    hits = {}
    for log in _raw_logs(40):
        for m in GEMINI_FREE_RE.finditer(_tail(log, 2_000_000)):
            hits.setdefault(m.group(2), (int(m.group(1)), log.stat().st_mtime))
    if hits:
        out["plan"].insert(0, {"label": "Tier", "value": "Free tier"})
        for model, (limit, seen) in hits.items():
            reset = _next_pacific_midnight(seen)  # Gemini API daily quotas reset at midnight Pacific time
            if reset > time.time():
                out["windows"].append({"label": f"{model} requests today", "used": 100.0, "resets_at": reset, "as_of": seen,
                                       "detail": f"free limit of {limit} a day reached"})
            else:
                out["usage"].append({"label": f"{model} free limit", "value": f"{limit} requests a day"})
    if auth == "gemini-api-key":
        out["notes"].append("The Gemini API does not report a key's tier or remaining quota; a limit shows here once a task hits it. Live usage: aistudio.google.com/usage.")
    elif auth == "oauth-personal":
        out["notes"].append("Gemini Code Assist quotas are only shown inside an interactive gemini session (/stats).")


# ----------------------------------------------------------------------------- Kilo / OpenCode
def _is_free(model_id: str, name: str, cost: dict) -> bool:
    paid = any(float(cost.get(k) or 0) for k in ("input", "output"))
    return not paid and (":free" in model_id or re.search(r"\bfree\b", name, re.I) is not None)


def parse_verbose_models(text: str) -> list[dict]:
    """`<provider>/<model>` on one line, then its JSON description."""
    out = []
    for m in re.finditer(r"^(\S+/\S+)\n(\{.*?\n\})", text, re.M | re.S):
        try:
            d = json.loads(m.group(2))
        except ValueError:
            continue
        cost = d.get("cost") or {}
        caps = d.get("capabilities") or {}
        mid = m.group(1)
        out.append(_row(mid, d.get("name"), cost.get("input"), cost.get("output"), free=_is_free(mid, d.get("name") or "", cost),
                        context=(d.get("limit") or {}).get("context"), reasoning=caps.get("reasoning"),
                        tools=bool(caps.get("toolcall", True)), variants=sorted((d.get("variants") or {}).keys()),
                        status=d.get("status") or "active"))
    return out


def _cli_models(name, cfg, refresh=False) -> dict:
    cache = CACHE_DIR / f"models-{name}.json"
    data = read_json(cache, None)
    if refresh or not data or time.time() - float(data.get("at") or 0) > MODELS_TTL:
        rows = parse_verbose_models(_run(name, SOURCES[name]["models"], cfg, timeout=120))
        if rows:
            data = {"at": time.time(), "models": rows}
            write_json(cache, data)
    if not data:
        return {"source": "settings", "error": f"{C.AGENTS[name]['label']} did not list any models. Is it signed in?"}
    return {"source": "cli", "source_label": f"{C.AGENTS[name]['binary']} models", "fetched": data["at"], "models": data["models"]}


# ----------------------------------------------------------------------------- the rest of the pack
def _cursor_models(cfg, refresh=False) -> dict:
    if not _signed_in("cursor", cfg):
        return {"source": "settings", "error": "Cursor is not signed in; its model list comes from your Cursor account."}

    def fetch():
        rows = []
        for line in _run("cursor", ["models"], cfg, timeout=60).splitlines():
            m = re.match(r"^\s*[-*•]?\s*([a-z0-9][\w.\-\[\]=,]*)\s*(?:[-–—:]\s+(.*?))?\s*(\((?:current|default)[^)]*\))?\s*$", line)
            if m and not re.match(r"^(available|models?|usage|error|tip|loading|no)$", m.group(1), re.I):
                rows.append(_row(m.group(1), m.group(2) or m.group(1), included=True, cli_default=True if m.group(3) else None))
        return rows
    rows, at = _cached("cursor-models", MODELS_TTL, fetch, refresh)
    if not rows:
        return {"source": "settings", "error": "cursor-agent did not list any models."}
    return {"source": "cli", "source_label": "cursor-agent models", "fetched": at, "models": rows,
            "plan_note": "Models draw on your Cursor plan's included usage; the CLI does not print per-token prices."}


def _crush_models(cfg, refresh=False) -> dict:
    path = _home(_env("crush", cfg)) / ".local/share/crush/providers.json"
    if refresh or not path.is_file():
        _run("crush", ["models"], cfg, timeout=60)  # refreshes Crush's provider catalog
    data = read_json(path, []) or []
    subscription = {"copilot", "kimi-coding", "zhipu-coding"}  # billed by that provider's plan, not per token
    rows = []
    for p in data if isinstance(data, list) else []:
        pid = p.get("id") or ""
        for d in p.get("models") or []:
            cin, cout = d.get("cost_per_1m_in"), d.get("cost_per_1m_out")
            zero = not _num(cin) and not _num(cout)
            sub = pid in subscription
            rows.append(_row(f"{pid}/{d.get('id')}", d.get("name"), None if sub else cin, None if sub else cout,
                             free=zero and not sub and re.search(r"free", f"{d.get('id')} {d.get('name')}", re.I) is not None,
                             context=d.get("context_window"), reasoning=d.get("can_reason"),
                             note="on that provider's subscription" if sub else None))
    if rows:
        return {"source": "cli", "source_label": "Crush provider catalog", "models": rows}
    return {"source": "catalog", "source_label": "models.dev catalog",
            "models": _catalog_rows(["anthropic", "openai", "google", "openrouter"], "{provider}/{id}")}


def _aider_models(cfg, refresh=False) -> dict:
    from .installer import AGENTS_DIR
    files = sorted((AGENTS_DIR / "venvs").glob("aider/lib/python*/site-packages/litellm/model_prices_and_context_window_backup.json"))
    data = read_json(files[-1], {}) if files else {}
    if not data:
        return {"source": "catalog", "source_label": "models.dev catalog",
                "models": _catalog_rows(["anthropic", "openai", "google", "deepseek"], "{provider}/{id}")}
    env = _env("aider", cfg)
    keys = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
    keyed = {p for p, k in keys.items() if env.get(k)}
    wanted = keyed or {"anthropic", "openai", "gemini", "deepseek"}
    rows = []
    for mid, d in data.items():
        if not isinstance(d, dict) or d.get("mode") != "chat" or d.get("litellm_provider") not in wanted:
            continue
        if str(d.get("deprecation_date") or "9999") < time.strftime("%Y-%m-%d"):
            continue
        cin, cout = _num(d.get("input_cost_per_token")), _num(d.get("output_cost_per_token"))
        prov = d["litellm_provider"]
        rows.append(_row(mid if "/" in mid or prov in ("anthropic", "openai") else f"{prov}/{mid}", None,
                         round(cin * 1e6, 4) if cin is not None else None, round(cout * 1e6, 4) if cout is not None else None,
                         free=":free" in mid and not cin and not cout, context=d.get("max_input_tokens"), reasoning=d.get("supports_reasoning")))
    return {"source": "cli", "source_label": "Aider's model table (litellm)" + ("" if keyed else ", main providers"), "models": rows,
            "plan_note": "Prices are what the provider charges your API key."}


def _config_models(name, cfg) -> list[str]:
    """Models named in the CLI's own config file, so a configured choice always shows."""
    home = _home(_env(name, cfg))
    found = []
    try:
        if name == "continue":
            found = re.findall(r"^\s*model:\s*['\"]?([^'\"\n#]+)", (home / ".continue/config.yaml").read_text(encoding="utf-8"), re.M)
        elif name == "goose":
            found = re.findall(r"^GOOSE_MODEL:\s*['\"]?([^'\"\n#]+)", (home / ".config/goose/config.yaml").read_text(encoding="utf-8"), re.M)
        elif name == "qwen":
            m = (read_json(home / ".qwen/settings.json", {}) or {}).get("model")
            found = [m.get("name") if isinstance(m, dict) else m] if m else []
    except OSError:
        pass
    return [f.strip() for f in found if isinstance(f, str) and f.strip()]


def _fallback_models(name, cfg, refresh=False) -> dict:
    providers, fmt = CATALOG_FALLBACK[name]
    if name == "goose":
        try:
            m = re.search(r"^GOOSE_PROVIDER:\s*['\"]?([\w-]+)", (_home(_env(name, cfg)) / ".config/goose/config.yaml").read_text(encoding="utf-8"), re.M)
            providers = [{"gemini": "google"}.get(m.group(1), m.group(1))] if m else providers
        except OSError:
            pass
    rows = _catalog_rows(providers, fmt, alias=PROVIDER_ALIAS.get(name))
    ids = {r["id"] for r in rows}
    rows = [_row(m, m, note="set in its config", cli_default=True) for m in _config_models(name, cfg) if m not in ids] + rows
    if not rows:
        return {"source": "settings"}
    return {"source": "catalog", "source_label": f"models.dev catalog ({', '.join(providers)})", "models": rows,
            "plan_note": "Prices are what the provider charges your API key."}


# ----------------------------------------------------------------------------- public
BUILDERS = {"claude": _claude_models, "codex": _codex_models, "copilot": _copilot_models, "gemini": _gemini_models,
            "cursor": _cursor_models, "crush": _crush_models, "aider": _aider_models}


def models(name: str, cfg: dict, refresh: bool = False) -> dict:
    configured = list((cfg.get("models") or {}).get(name) or [])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if name in SOURCES:
            res = _cli_models(name, cfg, refresh)
        elif name in BUILDERS:
            res = BUILDERS[name](cfg, refresh)
        elif name in CATALOG_FALLBACK:
            res = _fallback_models(name, cfg, refresh)
        else:
            res = {"source": "settings"}
    except Exception as e:  # a broken source must not break the dialog
        res = {"source": "settings", "error": f"Could not read models: {e}"}
    if not res.get("models"):
        res.update(source="settings", source_label="Relay settings", models=[_row(m, m) for m in configured])
        res.setdefault("hint", C.AGENTS.get(name, {}).get("models_hint"))
    rows = [r for r in res["models"] if r.get("status") != "deprecated"]
    rows.sort(key=lambda r: (r.get("available") is False, not r.get("cli_default"), not (r.get("free") or r.get("included")), r["id"]))
    return {"agent": name, "picked": configured, **res, "models": rows,
            "free": sum(1 for r in rows if r.get("free")), "included": sum(1 for r in rows if r.get("included"))}


def _stats_rows(name, cfg):
    rows = _box_lines(_run(name, SOURCES[name]["stats"], cfg, timeout=60))
    return [r for r in rows if re.search(r"sessions|messages|days|total cost|avg cost|input|output|cache", r["label"], re.I)]


def _pack_account(name, cfg, out, refresh=False):
    """What the pack CLIs print about the account; only asked once they are signed in."""
    src = SOURCES.get(name, {})
    if "profile" in src:
        # The e-mail address is not needed to judge the account and stays out of the page.
        out["plan"] = _no_email(_kv_lines(_run(name, src["profile"], cfg, timeout=60)))
    if "stats" in src:
        out["usage"] = _stats_rows(name, cfg)
    if name == "amp":
        text = _run("amp", ["usage"], cfg, timeout=60)
        rows = _no_email(_kv_lines(text))
        if rows:
            out["plan"] = rows
        elif text.strip() and not re.search(r"error|log ?in", text, re.I):
            out["usage"] = [{"label": "amp usage", "value": " ".join(text.split())[:300]}]
        else:
            out["notes"].append("amp usage did not answer" + (f": {text.strip()[:160]}" if text.strip() else "."))
    elif name == "cursor":
        rows = _no_email(_kv_lines(_run("cursor", ["about"], cfg, timeout=60)))
        out["plan"] = [r for r in rows if re.search(r"plan|tier|subscription|membership|team|model", r["label"], re.I)] or rows[:6]
        out["notes"].append("Cursor's CLI does not print included-usage counters; see cursor.com/dashboard.")
    elif name == "crush":
        text = _run("crush", ["stats"], cfg, timeout=60)
        out["usage"] = (_box_lines(text) or _kv_lines(text))[:12]
        if not out["usage"]:
            out["notes"].append("Crush has no recorded sessions on this server yet.")
    elif name == "goose":
        out["plan"] = [r for r in _kv_lines(_run("goose", ["info", "-v"], cfg, timeout=30)) if re.search(r"provider|model", r["label"], re.I)]


ACCOUNT = {"claude": _claude_account, "codex": _codex_account, "copilot": _copilot_account, "gemini": _gemini_account}


def account(name: str, cfg: dict, tasks: list[dict] | None = None, refresh: bool = False,
            cached_only: bool = False) -> dict:
    """Plan, limits and usage of one agent — of the account bound with bind(), when one is.

    cached_only: never run a CLI; return the last reading, or one that says plainly it is not known
    yet (the Agents page loads this way, so opening it never waits on a CLI)."""
    key = _key(name)
    hit = _account_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < ACCOUNT_TTL:
        out = dict(hit[1])
    elif cached_only:
        out = dict(hit[1]) if hit else {"agent": name, "plan": [], "windows": [], "usage": [],
                                        "notes": ["Not read yet: use Check limits to ask the CLI."], "unread": True}
    else:
        spec = C.AGENTS.get(name) or {}
        out = {"agent": name, "plan": [], "windows": [], "usage": [], "notes": []}
        signed = _signed_in(name, cfg) if spec.get("auth") else True
        if not signed:
            out["plan"] = [{"label": "Status", "value": "Not signed in"}]
            out["notes"].append(f"Sign in to {spec.get('label', name)} to see its plan and usage.")
            if "stats" in SOURCES.get(name, {}):  # local totals, and free models work without an account
                out["usage"] = _stats_rows(name, cfg)
        else:
            try:
                (ACCOUNT.get(name) or (lambda c, o, r: _pack_account(name, c, o, r)))(cfg, out, refresh)
            except Exception as e:  # an unreachable account service must not break the page
                out["error"] = str(e)
            if not (out["plan"] or out["windows"] or out["usage"] or out["notes"]):
                out["notes"].append(f"{spec.get('label', name)} does not report plan, balance or quota through its CLI.")
        out["fetched"] = time.time()
        _account_cache[key] = (out["fetched"], out)
        out = dict(out)
    out["account_id"] = bound_id()
    # Relay's own records change with every turn and are cheap to count, so they are never cached.
    out["relay"] = relay_usage(name, tasks)
    out["profile"] = out["plan"]  # older pages read `profile`
    return out
