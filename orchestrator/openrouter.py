"""OpenRouter as a provider that agents run on.

A team role can run an agent CLI "on OpenRouter": the CLI keeps its own tools and loop, but every model call goes
to OpenRouter with Relay's key instead of the CLI's own sign-in. This module owns what does not depend on a CLI:

  settings     Settings → Model providers, kept in the organisation settings (secrets masked, admin only):
               the API key, how agents connect, attribution headers, routing preferences, fallback models, spend caps.
  catalog      the public /api/v1/models list (cached 6 h), parsed into rows the model browser, the pickers and the
               capacity check share: prices per 1M tokens, context, tools, reasoning, structured outputs, modalities.
  ranking      "recommended for coding" and "Auto · best free model" are computed from those signals (and, for the
               automatic free pick, Relay's own history with each model and short cooldowns after a model failed),
               never from a hard-coded list of names. Every score carries the reasons behind it.
  account      /api/v1/key and /api/v1/credits (limit, usage, free tier, daily free requests) cached 5 minutes,
               plus what Relay recorded per model, role, task and project.
  errors       OpenRouter's status codes turned into sentences a person can act on, and autopsy categories.
  capacity     whether a role can start now: no key, no credits for a paid model, free requests used up for today,
               a spend cap reached. Autopilot uses it to fall back or wait.

The per-CLI launch configuration lives in openrouter_launch.py and the local gateway every turn goes through in
openrouter_proxy.py. The one thing never done here: touching a CLI's own login or config in the owner's home folder.
Everything is per run (environment variables and files in the task's run folder).

RELAY_OPENROUTER_BASE_URL (environment only, not in the UI) points Relay at another OpenRouter-compatible server;
it exists for development against tools/mock_openrouter.py and is shown as a warning whenever it is set.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .util import DATA_DIR, read_json, write_json

PROVIDER = "openrouter"
LABEL = "OpenRouter"
DEFAULT_BASE = "https://openrouter.ai/api/v1"
AUTO_FREE = "openrouter:auto-free"          # the "Auto · best free model" choice
FREE_ROUTER = "openrouter/free"             # OpenRouter's own router over free models
CACHE_DIR = DATA_DIR / "cache" / "openrouter"
MODELS_TTL = 6 * 3600
ACCOUNT_TTL = 5 * 60
COOLDOWN_FILE = CACHE_DIR / "cooldowns.json"

DEFAULTS = {
    "enabled": True,
    "api_key": "",
    "connection": "proxy",            # proxy: agents talk to Relay's local gateway (key never reaches them) · direct
    "attribution": True,              # send HTTP-Referer / X-Title so the account's activity page names Relay
    "app_url": "",                    # HTTP-Referer; "" = Relay's public URL (none: only the title is sent)
    "app_title": "Relay",
    "default_model": AUTO_FREE,       # a role on OpenRouter with no model runs this
    "routing": {
        "sort": "",                   # "" (OpenRouter's balancing) | price | throughput | latency
        "allow_fallbacks": True,      # other providers of the same model when one is down
        "data_collection": "allow",   # deny: only providers that do not store or train on prompts
        "zdr": False,                 # zero data retention endpoints only
        "require_parameters": False,  # only providers that support every parameter sent; strict, some CLIs send extras
        "order": [], "ignore": [],    # provider slugs
    },
    "fallback_models": [],            # tried in order when a model is rate limited or unavailable
    "free_rate_per_minute": 20,       # OpenRouter's limit for :free models; Relay paces requests under it
    "rate_limit_wait_seconds": 60,    # how long one request may wait out 429s before moving on
    "auto_free": {"min_context": 64000},
    "monthly_cap_usd": 0,             # all OpenRouter spend in a calendar month (0 = none)
    "project_caps": {},               # {project id: USD per month}
    "low_credit_usd": 1.0,            # alert when remaining credits fall below this (0 = never)
}
ROUTING_SORTS = ("", "price", "throughput", "latency")

_lock = threading.RLock()
_account_cache: dict = {"at": 0.0, "data": None, "key": ""}


# ============================================================================ settings
def _org():
    from .org import settings as OS
    return OS


def settings(raw: dict | None = None) -> dict:
    """The effective OpenRouter settings (with the secret), defaults filled in."""
    if raw is None:
        try:
            raw = ((_org().load().get("providers") or {}).get(PROVIDER)) or {}
        except Exception:
            raw = {}
    out = copy.deepcopy(DEFAULTS)
    for k, v in (raw or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _capture_env() -> dict:
    """Read the deployment's OpenRouter variables once and remove them from Relay's environment.

    Every agent CLI, check and git command starts from os.environ; a key left there would reach all of them, which is
    exactly what the gateway exists to prevent. The file path goes too, so an agent does not learn where the key lives."""
    out = {"key": "", "source": "", "base": ""}
    k = (os.environ.pop("RELAY_OPENROUTER_API_KEY", "") or "").strip()
    path = (os.environ.pop("RELAY_OPENROUTER_API_KEY_FILE", "") or "").strip()
    if k:
        out.update(key=k, source="environment")
    elif path:
        try:
            with open(path, encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                out.update(key=k, source="file")
        except OSError:
            pass
    out["base"] = (os.environ.pop("RELAY_OPENROUTER_BASE_URL", "") or "").strip().rstrip("/")
    return out


_ENV = _capture_env()


def env_key() -> tuple[str, str]:
    """(key, source) the deployment provided (RELAY_OPENROUTER_API_KEY or RELAY_OPENROUTER_API_KEY_FILE)."""
    return _ENV["key"], _ENV["source"]


def api_key(s: dict | None = None) -> str:
    s = s or settings()
    return (s.get("api_key") or "").strip() or env_key()[0]


def base_url() -> str:
    """Where OpenRouter is. Only the environment can change it (development against the mock server)."""
    return _ENV["base"] or DEFAULT_BASE


def base_overridden() -> bool:
    return base_url() != DEFAULT_BASE


def configured(s: dict | None = None) -> bool:
    s = s or settings()
    return bool(s.get("enabled", True)) and bool(api_key(s))


def attribution_headers(s: dict | None = None) -> dict:
    s = s or settings()
    if not s.get("attribution", True):
        return {}
    url = (s.get("app_url") or "").strip()
    if not url:
        try:
            url = (_org().load().get("integrations") or {}).get("public_url") or ""
        except Exception:
            url = ""
    out = {"X-Title": (s.get("app_title") or "Relay").strip() or "Relay"}
    if url:  # the referer names the app on the account's activity page; without an address the title alone does
        out["HTTP-Referer"] = url
    return out


def routing(s: dict | None = None) -> dict:
    """The `provider` object OpenRouter takes, with only what differs from its defaults."""
    r = (s or settings()).get("routing") or {}
    out = {}
    if r.get("sort") in ("price", "throughput", "latency"):
        out["sort"] = r["sort"]
    if r.get("allow_fallbacks") is False:
        out["allow_fallbacks"] = False
    if r.get("data_collection") == "deny":
        out["data_collection"] = "deny"
    if r.get("zdr"):
        out["zdr"] = True
    if r.get("require_parameters"):
        out["require_parameters"] = True
    for k in ("order", "ignore"):
        vals = [str(x).strip() for x in (r.get(k) or []) if str(x).strip()]
        if vals:
            out[k] = vals
    return out


def validate(section: dict) -> dict:
    """Clean an incoming settings section (called by org settings save)."""
    s = settings(section)
    if s.get("connection") not in ("proxy", "direct"):
        raise ValueError("OpenRouter connection must be proxy or direct.")
    r = s["routing"]
    if r.get("sort") not in ROUTING_SORTS:
        raise ValueError("Routing sort must be price, throughput, latency or empty.")
    if r.get("data_collection") not in ("allow", "deny"):
        raise ValueError("Data collection must be allow or deny.")
    for k in ("order", "ignore"):
        vals = r.get(k) or []
        if isinstance(vals, str):
            vals = re.split(r"[,\s]+", vals)
        r[k] = [x for x in (str(v).strip() for v in vals) if re.match(r"^[\w.\-/]{1,80}$", x)][:30]
    r["allow_fallbacks"], r["zdr"], r["require_parameters"] = bool(r.get("allow_fallbacks", True)), bool(r.get("zdr")), bool(r.get("require_parameters"))
    fb = s.get("fallback_models") or []
    if isinstance(fb, str):
        fb = re.split(r"[\s,]+", fb)
    s["fallback_models"] = [m for m in (str(x).strip() for x in fb) if valid_model_id(m)][:8]
    dm = (s.get("default_model") or "").strip()
    if dm and not (dm == AUTO_FREE or valid_model_id(dm)):
        raise ValueError(f"“{dm}” is not an OpenRouter model id (like anthropic/claude-sonnet-4.5).")
    s["default_model"] = dm
    s["monthly_cap_usd"] = max(0.0, float(s.get("monthly_cap_usd") or 0))
    s["low_credit_usd"] = max(0.0, float(s.get("low_credit_usd") or 0))
    s["free_rate_per_minute"] = max(1, min(600, int(s.get("free_rate_per_minute") or 20)))
    s["rate_limit_wait_seconds"] = max(0, min(600, int(s.get("rate_limit_wait_seconds") if s.get("rate_limit_wait_seconds") is not None else 60)))
    s["auto_free"] = {"min_context": max(8000, min(2_000_000, int((s.get("auto_free") or {}).get("min_context") or 64000)))}
    caps = {}
    for pid, v in (s.get("project_caps") or {}).items():
        try:
            val = max(0.0, float(v or 0))
        except (TypeError, ValueError):
            continue
        if val:
            caps[str(pid)[:80]] = val
    s["project_caps"] = caps
    s["app_title"] = (str(s.get("app_title") or "Relay").strip() or "Relay")[:60]
    s["app_url"] = str(s.get("app_url") or "").strip()[:300]
    if s["app_url"] and not re.match(r"^https?://", s["app_url"]):
        raise ValueError("The app URL for attribution starts with http:// or https://")
    s["enabled"] = bool(s.get("enabled", True))
    s["attribution"] = bool(s.get("attribution", True))
    return s


def valid_model_id(m: str) -> bool:
    return bool(re.match(r"^~?[\w.\-]+/[\w.\-:]+(?:\[[\w=,.\-]+\])?$", m or "")) and len(m) <= 160


def is_auto(model: str) -> bool:
    return (model or "").strip() in (AUTO_FREE, "auto-free")


def public(s: dict | None = None) -> dict:
    """Settings for the browser: the key masked, where it comes from, and the development override if any."""
    from .org.common import MASK
    s = copy.deepcopy(s or settings())
    own = bool((s.get("api_key") or "").strip())
    envk, src = env_key()
    s["has_api_key"] = own or bool(envk)
    s["key_source"] = "settings" if own else src
    s["key_hint"] = _key_hint(s["api_key"] if own else envk)
    s["api_key"] = MASK if own else ""
    s["base_url"] = base_url() if base_overridden() else ""
    return s


def _key_hint(k: str) -> str:
    """sk-or-v1-…abcd: enough to tell two keys apart, never enough to use one."""
    k = (k or "").strip()
    return f"{k[:8]}…{k[-4:]}" if len(k) > 16 else ("set" if k else "")


# ============================================================================ HTTP
def _request(method: str, path: str, key: str = "", body=None, timeout: float = 20, base: str | None = None):
    url = (base or base_url()) + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "Relay"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(attribution_headers())
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        return e.code, payload


# ============================================================================ catalog
def _per_million(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None  # routers price per request dynamically (-1)
    return round(f * 1_000_000, 4)


def _num(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _params_b(mid: str, name: str) -> float | None:
    """Parameter count in billions from the id or name (70b, 8x7b, 550b-a55b); None when not stated."""
    m = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)b\b", f"{mid} {name}", re.I)
    if m:
        return float(m.group(1)) * float(m.group(2))
    m = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)b\b", f"{mid} {name}", re.I)
    return float(m.group(1)) if m else None


def parse_model(d: dict) -> dict | None:
    """One /models entry as Relay's row. Keeps what the pickers, browser and capacity check need."""
    mid = str(d.get("id") or "").strip()
    if not mid:
        return None
    p = d.get("pricing") or {}
    arch = d.get("architecture") or {}
    params = [str(x) for x in d.get("supported_parameters") or []]
    pin, pout = _per_million(p.get("prompt")), _per_million(p.get("completion"))
    router = _num(p.get("prompt")) is not None and _num(p.get("prompt")) < 0
    free = mid.endswith(":free") or (not router and pin == 0 and pout == 0 and not _num(p.get("request")))
    top = d.get("top_provider") or {}
    outs = arch.get("output_modalities") or ([arch["modality"].split("->")[-1]] if arch.get("modality") else ["text"])
    ins = arch.get("input_modalities") or (arch.get("modality", "text->").split("->")[0].split("+") if arch.get("modality") else ["text"])
    exp = d.get("expiration_date")
    row = {
        "id": mid, "name": d.get("name") or mid, "input": pin, "output": pout,
        "request": (_num(p.get("request")) or None) if (_num(p.get("request")) or 0) > 0 else None,  # USD per request
        "image": _num(p.get("image")), "cache_read": _per_million(p.get("input_cache_read")),
        "free": bool(free), "router": bool(router),
        "context": int(d.get("context_length") or top.get("context_length") or 0) or None,
        "max_output": int(top.get("max_completion_tokens") or 0) or None,
        "tools": "tools" in params, "tool_choice": "tool_choice" in params,
        "reasoning": "reasoning" in params or "include_reasoning" in params,
        "structured": "structured_outputs" in params or "response_format" in params,
        "params": params, "modalities_in": [str(x) for x in ins], "modalities_out": [str(x) for x in outs],
        "text_out": "text" in [str(x) for x in outs],
        "created": int(d.get("created") or 0) or None, "expires": exp or None,
        "moderated": bool(top.get("is_moderated")),
        "size_b": _params_b(mid, d.get("name") or ""),
        "description": (str(d.get("description") or "")[:240]).strip(),
    }
    return row


def _fetch_models() -> list[dict]:
    status, data = _request("GET", "/models", timeout=30)
    if status != 200:
        raise RuntimeError(f"OpenRouter /models answered {status}")
    rows = [parse_model(d) for d in (data.get("data") or []) if isinstance(d, dict)]
    return [r for r in rows if r]


_catalog_mem: dict = {"at": 0.0, "rows": None, "next_try": 0.0, "error": None, "loaded": False}
_catalog_fetch = threading.Lock()


def catalog(refresh: bool = False) -> dict:
    """{"models": rows, "fetched": ts, "stale": bool}. Kept in memory, saved to disk, refreshed every 6 hours.

    The gateway and the scheduler ask this on every request, so it never waits on the network while another caller
    is fetching, and a failed fetch waits 10 minutes before the next try (a forced refresh at most once a minute)."""
    path = CACHE_DIR / ("models.json" if not base_overridden() else "models-override.json")
    mem = _catalog_mem
    if not mem["loaded"]:
        mem["loaded"] = True
        cached = read_json(path, None)
        if cached and cached.get("models"):
            mem.update(at=float(cached.get("at") or 0), rows=cached["models"])
    now = time.time()
    due = mem["rows"] is None or now - mem["at"] >= MODELS_TTL or (refresh and now - mem["at"] > 60)
    if due and now >= mem["next_try"] or (refresh and mem["rows"] is None):
        if _catalog_fetch.acquire(blocking=mem["rows"] is None):  # with a copy in hand, never queue behind a fetch
            try:
                rows = _fetch_models()
                if rows:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    write_json(path, {"at": time.time(), "models": rows})
                    mem.update(at=time.time(), rows=rows, error=None, next_try=0.0)
            except Exception as e:
                mem.update(error=str(e), next_try=time.time() + 600)
            finally:
                _catalog_fetch.release()
    if mem["rows"] is None:
        return {"models": [], "fetched": None, "stale": True,
                "error": f"Could not load the OpenRouter model list: {mem['error'] or 'no answer'}"}
    stale = time.time() - mem["at"] >= MODELS_TTL or bool(mem["error"] and refresh)
    out = {"models": mem["rows"], "fetched": mem["at"], "stale": stale}
    if mem["error"] and stale:
        out["error"] = mem["error"]
    return out


def model_row(mid: str, rows: list[dict] | None = None) -> dict | None:
    rows = rows if rows is not None else catalog()["models"]
    return next((r for r in rows if r["id"] == mid), None)


def is_free(mid: str, rows: list[dict] | None = None) -> bool:
    if is_auto(mid) or (mid or "").endswith(":free") or mid == FREE_ROUTER:
        return True
    r = model_row(mid, rows)
    return bool(r and r.get("free"))


def price_of(mid: str, rows: list[dict] | None = None) -> dict:
    r = model_row(mid, rows) or {}
    return {"input": r.get("input"), "output": r.get("output"), "cache_read": r.get("cache_read"), "free": bool(r.get("free"))}


def estimate_cost(mid: str, input_tokens: int, output_tokens: int, cached: int = 0, rows=None) -> float | None:
    """From the catalog price, for turns where OpenRouter did not report a cost. None when the price is unknown."""
    p = price_of(mid, rows)
    if p["free"]:
        return 0.0
    if p["input"] is None or p["output"] is None:
        return None
    cached = min(int(cached or 0), int(input_tokens or 0))
    cr = p["cache_read"] if p["cache_read"] is not None else p["input"]
    return round(((int(input_tokens or 0) - cached) * p["input"] + cached * cr + int(output_tokens or 0) * p["output"]) / 1_000_000, 6)


# ============================================================================ ranking
def _age_days(created, now: float) -> float | None:
    return (now - float(created)) / 86400 if created else None


def capability(r: dict, now: float | None = None) -> tuple[float, list[str]]:
    """What a coding agent gets from a model, from catalog signals only. (points, reasons)."""
    now = now or time.time()
    pts, why = 0.0, []
    ctx = int(r.get("context") or 0)
    if ctx:
        c = max(0.0, min(1.0, math.log2(max(ctx, 32000) / 32000) / 5))  # 32k → 0, 1M → 1
        pts += 3 * c
        why.append(f"{_fmt_ctx(ctx)} context (+{3 * c:.1f})")
    if r.get("reasoning"):
        pts += 1.5
        why.append("reasoning (+1.5)")
    if r.get("structured"):
        pts += 0.75
        why.append("structured outputs (+0.75)")
    if r.get("tool_choice"):
        pts += 0.5
        why.append("tool_choice (+0.5)")
    age = _age_days(r.get("created"), now)
    if age is not None:
        if age <= 60:
            pts += 1.5
            why.append(f"released {int(age)} days ago (+1.5)")
        elif age <= 180:
            pts += 1
            why.append(f"released {int(age)} days ago (+1)")
        elif age <= 365:
            pts += 0.5
            why.append("released within a year (+0.5)")
        elif age > 730:
            pts -= 1
            why.append("over two years old (−1)")
    words = f"{r['id'].split('/')[-1]} {r.get('name', '')}".lower()
    if re.search(r"\b(pro|max|ultra|opus|large|plus)\b", words):
        pts += 1
        why.append("a family's larger tier (+1)")
    elif re.search(r"\b(mini|lite|nano|small|tiny|haiku|micro)\b", words):
        pts -= 0.75
        why.append("a family's lighter tier (−0.75)")
    size = r.get("size_b")
    if size is not None:
        if size < 12:
            pts -= 2.5
            why.append(f"small model, {size:g}B (−2.5)")
        elif size < 30:
            pts -= 0.75
            why.append(f"{size:g}B parameters (−0.75)")
        elif size >= 100:
            pts += 1
            why.append(f"{size:g}B parameters (+1)")
    if re.search(r"cod(e|er|ing)|dev|swe", f"{r['id']} {r.get('name', '')}", re.I):
        pts += 1
        why.append("built for code (+1)")
    if re.search(r"safety|guard|moderation|embed|vision-only|ocr", f"{r['id']} {r.get('name', '')}", re.I):
        pts -= 5
        why.append("not a general model (−5)")
    return pts, why


def _fmt_ctx(n: int) -> str:
    return f"{round(n / 1e6, 1):g}M" if n >= 1_000_000 else f"{round(n / 1000)}k"


def usable_for_agents(r: dict, min_context: int = 64000) -> tuple[bool, str]:
    if r.get("router") and r["id"] != FREE_ROUTER:
        return False, "a router with dynamic pricing"
    if not r.get("tools"):
        return False, "no tool calling (coding agents need it)"
    if not r.get("text_out", True):
        return False, "no text output"
    if int(r.get("context") or 0) < min_context:
        return False, f"context under {_fmt_ctx(min_context)}"
    if r.get("expires"):
        try:
            if datetime.fromisoformat(str(r["expires"]).replace("Z", "+00:00")).timestamp() < time.time() + 3 * 86400:
                return False, "retiring within days"
        except ValueError:
            pass
    return True, ""


def recommended_for_coding(rows: list[dict], limit: int = 12, now: float | None = None) -> list[dict]:
    """Tool-capable models with a large context and a sensible price, best first, each with its reasons."""
    out = []
    for r in rows:
        ok, _ = usable_for_agents(r, 100_000)
        if not ok or r.get("router") or r.get("free") or ":" in r["id"]:
            continue  # variants (:batch, :exacto …) are listed through their base model
        pts, why = capability(r, now)
        blended = ((r.get("input") or 0) * 3 + (r.get("output") or 0)) / 4  # a coding turn reads far more than it writes
        if blended <= 0:
            continue
        # Value, not cheapness: the very cheapest models are usually the smallest ones.
        for lo, hi, d in ((0, 0.3, 0.5), (0.3, 3, 1.5), (3, 8, 0.75), (8, 15, 0), (15, 1e9, -1.5)):
            if lo <= blended < hi:
                pts += d
                why.append(f"${blended:.2f}/M blended ({d:+g})")
                break
        out.append({"id": r["id"], "name": r.get("name"), "score": round(pts, 2), "why": why, "blended": round(blended, 4)})
    out.sort(key=lambda x: (-x["score"], x["blended"], x["id"]))
    picked, per_vendor = [], {}
    for x in out:  # at most two per vendor, so the shortlist is a real choice
        v = x["id"].lstrip("~").split("/")[0]
        if per_vendor.get(v, 0) >= 2:
            continue
        per_vendor[v] = per_vendor.get(v, 0) + 1
        picked.append(x)
        if len(picked) >= limit:
            break
    return picked


# ---- cooldowns: short-lived memory of models that just failed, so the automatic pick moves on
def _cooldowns() -> dict:
    data = read_json(COOLDOWN_FILE, {}) or {}
    now = time.time()
    return {m: c for m, c in data.items() if isinstance(c, dict) and float(c.get("until") or 0) > now - 86400}


def cooldown(model: str, reason: str, status: int | None = None) -> dict:
    """Keep `model` out of the automatic pick for a while. Rate limits back off 2, 4, 8 … 60 minutes; a model that is
    gone or has no provider waits 6 hours."""
    if not model:
        return {}
    with _lock:
        data = _cooldowns()
        cur = data.get(model) or {}
        n = int(cur.get("count") or 0) + 1 if float(cur.get("until") or 0) > time.time() - 3600 else 1
        span = 6 * 3600 if status in (404, 410) else (min(3600, 120 * 2 ** (n - 1)) if status == 429 else min(3 * 3600, 600 * n))
        data[model] = {"until": time.time() + span, "reason": reason[:200], "status": status, "count": n, "at": time.time()}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        write_json(COOLDOWN_FILE, data)
        return data[model]


def cooling(model: str) -> dict | None:
    c = _cooldowns().get(model)
    return c if c and float(c.get("until") or 0) > time.time() else None


def clear_cooldown(model: str):
    with _lock:
        data = _cooldowns()
        if data.pop(model, None) is not None:
            write_json(COOLDOWN_FILE, data)


def model_history(tasks: list[dict] | None, since_days: int = 60) -> dict:
    """What Relay saw each OpenRouter model do: turns, failed turns, rate limits (last 24 h) and finished tasks.

    Read from every task's turn log (rows carry `or_models` and `or_error`) and the task's final status."""
    out: dict = {}
    now = time.time()
    for t in tasks or []:
        used = set()
        for row in ((t.get("metrics") or {}).get("log") or []):
            if row.get("provider") != PROVIDER or now - float(row.get("end") or 0) > since_days * 86400:
                continue
            models = row.get("or_models") or ([row.get("model")] if row.get("model") else [])
            for mid in models:
                h = out.setdefault(mid, {"turns": 0, "failed_turns": 0, "rate_limited_24h": 0, "failed_24h": 0, "tasks": 0, "tasks_ok": 0})
                h["turns"] += 1
                used.add(mid)
                if row.get("or_error"):
                    h["failed_turns"] += 1
                    if now - float(row.get("end") or 0) < 86400:
                        h["failed_24h"] += 1
                        if int(row.get("or_status") or 0) == 429:
                            h["rate_limited_24h"] += 1
        if t.get("status") in ("done", "failed"):
            for mid in used:
                out[mid]["tasks"] += 1
                out[mid]["tasks_ok"] += 1 if t.get("status") == "done" else 0
    return out


def rank_free(rows: list[dict], history: dict | None = None, min_context: int = 64000, now: float | None = None,
              cooldowns: dict | None = None, limit: int = 8) -> list[dict]:
    """The automatic free pick: free, tool-calling, text, enough context; ranked by capability, Relay's own record with
    the model and recent trouble. Models cooling down after a failure sort last. OpenRouter's free router closes the list."""
    now = now or time.time()
    history = history or {}
    cds = cooldowns if cooldowns is not None else {m: c for m, c in _cooldowns().items() if float(c.get("until") or 0) > now}
    ranked = []
    for r in rows:
        if not r.get("free") or r["id"] == FREE_ROUTER:
            continue
        ok, _ = usable_for_agents(r, min_context)
        if not ok:
            continue
        pts, why = capability(r, now)
        h = history.get(r["id"]) or {}
        if h.get("tasks", 0) >= 2:
            rate = h["tasks_ok"] / h["tasks"]
            d = round((rate - 0.5) * 4, 2)
            pts += d
            why.append(f"Relay history: {h['tasks_ok']}/{h['tasks']} tasks delivered ({d:+g})")
        elif h.get("turns", 0) >= 3:
            rate = 1 - h["failed_turns"] / h["turns"]
            d = round((rate - 0.7) * 3, 2)
            pts += d
            why.append(f"Relay history: {h['turns'] - h['failed_turns']}/{h['turns']} turns ok ({d:+g})")
        if h.get("rate_limited_24h"):
            d = min(3.0, 1.0 * h["rate_limited_24h"])
            pts -= d
            why.append(f"rate limited {h['rate_limited_24h']}× today (−{d:g})")
        elif h.get("failed_24h"):
            d = min(3.0, 1.5 * h["failed_24h"])
            pts -= d
            why.append(f"failed {h['failed_24h']}× today (−{d:g})")
        cd = cds.get(r["id"])
        entry = {"id": r["id"], "name": r.get("name"), "score": round(pts, 2), "why": why, "context": r.get("context")}
        if cd:
            entry["cooldown_until"] = cd.get("until")
            entry["cooldown_reason"] = cd.get("reason")
        ranked.append(entry)
    ranked.sort(key=lambda x: (bool(x.get("cooldown_until")), -x["score"], x["id"]))
    out = ranked[:limit]
    router = next((r for r in rows if r["id"] == FREE_ROUTER), None)
    if router is not None or not rows:
        cd = cds.get(FREE_ROUTER)
        out.append({"id": FREE_ROUTER, "name": (router or {}).get("name") or "OpenRouter free router", "score": None,
                    "why": ["OpenRouter's own router over free models: the last resort when every pick above is busy"],
                    "context": (router or {}).get("context"), **({"cooldown_until": cd["until"], "cooldown_reason": cd.get("reason")} if cd else {})})
    return out


def auto_free_candidates(tasks=None, s: dict | None = None, rows=None) -> list[dict]:
    s = s or settings()
    rows = rows if rows is not None else catalog()["models"]
    return rank_free(rows, model_history(tasks), int((s.get("auto_free") or {}).get("min_context") or 64000))


def pick_free(tasks=None, s: dict | None = None, exclude=(), rows=None) -> tuple[str, list[str], dict | None]:
    """(model, rotation order after it, the chosen ranking entry). Skips models cooling down and `exclude`."""
    cands = [c for c in auto_free_candidates(tasks, s, rows) if c["id"] not in set(exclude)]
    ready = [c for c in cands if not c.get("cooldown_until")]
    order = ready + [c for c in cands if c.get("cooldown_until")]
    if not order:
        return FREE_ROUTER, [], None
    first = order[0]
    return first["id"], [c["id"] for c in order[1:] if not c.get("cooldown_until")], first


# ============================================================================ account
def fetch_account(key: str | None = None) -> dict:
    """/key and /credits for a key. Raises nothing: failures come back as {"ok": False, "error"}."""
    key = key if key is not None else api_key()
    if not key:
        return {"ok": False, "error": "No OpenRouter API key is set.", "status": None}
    try:
        st, k = _request("GET", "/key", key)
    except Exception as e:
        return {"ok": False, "error": f"OpenRouter is unreachable: {e}", "status": None}
    if st != 200:
        return {"ok": False, "status": st, "error": describe_error(st, _err_msg(k), (k.get("error") or {}).get("metadata"))["message"]}
    kd = k.get("data") or {}
    out = {"ok": True, "status": 200,
           "limit": _num(kd.get("limit")), "limit_remaining": _num(kd.get("limit_remaining")), "limit_reset": kd.get("limit_reset"),
           "usage": _num(kd.get("usage")) or 0.0, "usage_daily": _num(kd.get("usage_daily")), "usage_weekly": _num(kd.get("usage_weekly")),
           "usage_monthly": _num(kd.get("usage_monthly")), "is_free_tier": bool(kd.get("is_free_tier")),
           "free_requests": kd.get("free_model_daily_requests") if isinstance(kd.get("free_model_daily_requests"), dict) else None}
    try:
        st2, c = _request("GET", "/credits", key)
        if st2 == 200:
            cd = c.get("data") or {}
            tc, tu = _num(cd.get("total_credits")), _num(cd.get("total_usage"))
            out.update(total_credits=tc, total_usage=tu, credits_remaining=round((tc or 0) - (tu or 0), 6) if tc is not None else None)
        else:
            out["credits_error"] = _err_msg(c) or f"HTTP {st2}"
    except Exception as e:
        out["credits_error"] = str(e)
    out["checked_at"] = time.time()
    return out


def account(refresh: bool = False) -> dict:
    key = api_key()
    with _lock:
        hit = _account_cache
        if refresh and hit["data"] is not None and hit["key"] == key and time.time() - hit["at"] < 30:
            refresh = False  # anyone may press Refresh; OpenRouter is asked at most twice a minute
        if not refresh and hit["data"] is not None and hit["key"] == key and time.time() - hit["at"] < ACCOUNT_TTL:
            return dict(hit["data"])
    data = fetch_account(key)
    with _lock:
        _account_cache.update(at=time.time(), data=data, key=key)
    return dict(data)


def account_cached() -> dict | None:
    """The last account reading without waiting on the network (the scheduler runs twice a second); a stale or missing
    reading is refreshed in the background. None until the first reading arrives."""
    key = api_key()
    hit = _account_cache
    fresh = hit["data"] is not None and hit["key"] == key and time.time() - hit["at"] < ACCOUNT_TTL
    if not fresh and key and not hit.get("refreshing"):
        hit["refreshing"] = True

        def run():
            try:
                account(refresh=True)
            finally:
                hit["refreshing"] = False
        threading.Thread(target=run, name="relay-openrouter-account", daemon=True).start()
    return dict(hit["data"]) if hit["data"] is not None and hit["key"] == key else None


def invalidate_account():
    _account_cache["at"] = 0.0


def spendable(acc: dict) -> float | None:
    """How many dollars paid models may still spend: the lower of credits left and the key's own limit. None = unknown."""
    vals = [v for v in (acc.get("credits_remaining"), acc.get("limit_remaining") if acc.get("limit") is not None else None) if v is not None]
    return min(vals) if vals else None


# ============================================================================ errors
ERROR_TEXT = {
    400: ("bad_request", "OpenRouter rejected the request (400 bad request)"),
    401: ("provider_auth", "OpenRouter rejected the API key (401 unauthorized): check Settings → Model providers"),
    402: ("provider_credits", "OpenRouter: out of credits (402 payment required); add credits at openrouter.ai/settings/credits, raise the key's limit, or pick a :free model"),
    403: ("provider_blocked", "OpenRouter refused the request (403): a moderation flag or a guardrail on the key"),
    404: ("model_unavailable", "OpenRouter: this model is not available (404 not found); pick another model"),
    408: ("provider_timeout", "OpenRouter timed out (408)"),
    413: ("bad_request", "OpenRouter: the request is too large for this model's context (413)"),
    429: ("rate_limit", "OpenRouter rate limit (429 too many requests)"),
    500: ("provider_unavailable", "OpenRouter had an internal error (500)"),
    502: ("provider_unavailable", "OpenRouter: the model's provider is down or returned an invalid answer (502)"),
    503: ("provider_unavailable", "OpenRouter: no provider available for this model with your routing settings (503)"),
}


def _err_msg(payload) -> str:
    if isinstance(payload, dict):
        e = payload.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or "")
        if isinstance(e, str):
            return e
        return str(payload.get("message") or "")
    return str(payload or "")


def describe_error(status: int | None, message: str = "", metadata: dict | None = None, model: str = "") -> dict:
    """{"category", "message", "retry"} for an OpenRouter error: what happened and what to do about it."""
    status = int(status or 0)
    cat, text = ERROR_TEXT.get(status, ("provider_error", f"OpenRouter error ({status or 'no status'})"))
    meta = metadata or {}
    detail = (message or "").strip()
    if status == 402:
        src = str(meta.get("limit_source") or "")
        if src == "openrouter_key_limit":
            text = "OpenRouter: this API key reached its spending limit (402); raise the key's limit on openrouter.ai/settings/keys or pick a :free model"
        elif src.startswith("relay"):
            text = detail or "Relay's OpenRouter spend cap is reached (402)"
            cat = "provider_budget"
            detail = ""
    if re.search(r"key limit exceeded", detail, re.I):
        return {"category": "provider_credits", "status": status, "retry": False, "config": True,
                "message": f"OpenRouter: this API key reached its spending limit ({status}); raise the key's limit on "
                           "openrouter.ai/settings/keys or pick a :free model"}
    if status == 429 and re.search(r"per[- ]day|daily|free-models-per", detail, re.I):
        # The account's free requests for today are gone: every free model shares that quota, so waiting or rotating
        # to another free model cannot help before midnight UTC.
        return {"category": "provider_quota", "status": status, "retry": False, "config": False, "quota": True,
                "message": "OpenRouter: today's free-model requests are used up (429); they reset at 00:00 UTC. "
                           "Buying 10 credits raises the limit from 50 to 1000 a day, or pick a paid model"}
    if status == 429 and model.endswith(":free"):
        text += "; free models allow 20 requests a minute and 50 a day (1000 a day once the account bought 10 credits)"
    if status == 503 and (settings().get("routing") or {}).get("data_collection") == "deny":
        text += "; data collection is set to deny, which rules out some providers"
    m = re.search(r"support the provided '?([\w.]+)'?", detail)
    if status == 404 and m:
        # Not the model's fault: no provider takes a parameter the CLI sent. Another model would fail the same way.
        text = (f"OpenRouter: no provider for this model supports the '{m.group(1)}' parameter this agent sends (404); "
                "turn off 'Only providers that support every parameter' in Settings → Model providers, or pick another model")
        return {"category": "provider_params", "message": text, "status": status, "retry": False, "config": True, "params": True}
    if status == 404 and re.search(r"data policy|privacy", detail, re.I):
        text = "OpenRouter: no endpoint matches your data policy (404); relax the privacy settings or pick another model"
        cat = "provider_unavailable"
    if meta.get("provider_name"):
        text += f" · provider {meta['provider_name']}"
    if detail and detail.lower() not in text.lower():
        text += f": {detail[:300]}"
    return {"category": cat, "message": text, "status": status,
            "retry": status in (408, 429, 500, 502, 503), "config": status in (401, 402, 403)}


# ============================================================================ spend (from Relay's own turn records)
def _month_start(ts: float | None = None) -> float:
    d = datetime.fromtimestamp(ts or time.time())
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _next_month(ts: float | None = None) -> float:
    d = datetime.fromtimestamp(ts or time.time()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return d.replace(year=d.year + (d.month == 12), month=1 if d.month == 12 else d.month + 1).timestamp()


def turns(tasks: list[dict] | None):
    """(task, turn row) for every turn that ran on OpenRouter."""
    for t in tasks or []:
        for row in ((t.get("metrics") or {}).get("log") or []):
            if row.get("provider") == PROVIDER:
                yield t, row


LEDGER_DIR = DATA_DIR / "openrouter"
_ledger_lock = threading.Lock()
_ledger_cache: dict = {}


def ledger_add(cost: float, task: str = "", project: str | None = None, role: str = "", agent: str = "", model: str = "",
               estimated: bool = False, ts: float | None = None):
    """Append one charge to this month's ledger. The spend caps read the ledger, not task records: a deleted task, a
    trimmed turn log, an interrupted turn, a retrospective or an agent test still counts."""
    if not cost:
        return
    ts = ts or time.time()
    row = {"t": round(ts, 3), "cost": round(float(cost), 8), "task": task, "project": project or "", "role": role,
           "agent": agent, "model": model, "est": bool(estimated)}
    path = LEDGER_DIR / f"ledger-{datetime.fromtimestamp(ts):%Y-%m}.jsonl"
    with _ledger_lock:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def ledger_rows(month_ts: float | None = None) -> list[dict]:
    path = LEDGER_DIR / f"ledger-{datetime.fromtimestamp(month_ts or time.time()):%Y-%m}.jsonl"
    try:
        st = path.stat()
    except OSError:
        return []
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _ledger_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    _ledger_cache[str(path)] = (key, rows)
    return rows


def ledger_spend(since: float, project: str | None = None) -> float:
    return round(sum(float(r.get("cost") or 0) for r in ledger_rows(since)
                     if float(r.get("t") or 0) >= since and (not project or r.get("project") == project)), 6)


def spend(tasks, since: float, project: str | None = None) -> float:
    """Spend since a moment this month: the ledger when it has entries, else the task records (older installs)."""
    if since >= _month_start() and ledger_rows(since):
        return ledger_spend(since, project)
    return _task_spend(tasks, since, project)


def _task_spend(tasks, since: float, project: str | None = None) -> float:
    total = 0.0
    ids = None
    for t, row in turns(tasks):
        if float(row.get("end") or 0) < since:
            continue
        if project:
            from .org import projects
            if ids is None:
                ids = {p["id"] for p in projects.all_projects()}
            if projects.project_of_task(t, ids) != project:
                continue
        total += float(row.get("cost_usd") or 0)
    return round(total, 6)


def cap_state(tasks, project: str | None, s: dict | None = None, extra: float = 0.0) -> dict | None:
    """The OpenRouter spend cap that blocks new work, if any: {"reason", "until", "cap", "spent"}."""
    s = s or settings()
    start = _month_start()
    org_cap = float(s.get("monthly_cap_usd") or 0)
    if org_cap:
        spent = spend(tasks, start) + extra
        if spent >= org_cap:
            return {"reason": f"OpenRouter monthly cap reached (${spent:.2f} of ${org_cap:.2f})", "until": _next_month(), "cap": org_cap, "spent": spent}
    pcap = float((s.get("project_caps") or {}).get(project or "", 0) or 0) if project else 0
    if pcap:
        spent = spend(tasks, start, project) + extra
        if spent >= pcap:
            return {"reason": f"OpenRouter cap for this project reached (${spent:.2f} of ${pcap:.2f} this month)", "until": _next_month(),
                    "cap": pcap, "spent": spent}
    return None


def usage_report(tasks: list[dict] | None) -> dict:
    """Spend today and this month, by model, role, agent, project and task, from Relay's own turn records."""
    from .org import projects
    ids = {p["id"] for p in projects.all_projects()}
    names = {p["id"]: p["name"] for p in projects.all_projects()}
    now = time.time()
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    month = _month_start(now)
    def blank():
        return {"cost_usd": 0.0, "turns": 0, "requests": 0, "input": 0, "output": 0, "estimated": False}
    out = {"today": blank(), "month": blank(), "by_model": {}, "by_role": {}, "by_agent": {}, "by_project": {}, "by_task": {}}
    for t, row in turns(tasks):
        end = float(row.get("end") or 0)
        if end < month:
            continue
        c = float(row.get("cost_usd") or 0)
        buckets = [out["month"]]
        if end >= today:
            buckets.append(out["today"])
        models = row.get("or_models") or [row.get("model") or "?"]
        per = row.get("or_model_costs") or {}
        for mid in models:
            b = out["by_model"].setdefault(mid, blank())
            b["cost_usd"] += float(per.get(mid, c / len(models)))
            b["turns"] += 1
        pid = projects.project_of_task(t, ids)
        for key, val in (("by_role", row.get("role") or "?"), ("by_agent", row.get("agent") or "?"), ("by_project", pid or "none"), ("by_task", t["id"])):
            buckets.append(out[key].setdefault(val, blank()))
        for b in buckets:
            b["cost_usd"] += c
            b["turns"] += 1
            b["requests"] += int(row.get("or_requests") or 0)
            b["input"] += int(row.get("input") or 0)
            b["output"] += int(row.get("output") or 0)
            b["estimated"] = b["estimated"] or bool(row.get("estimated"))
    def rows(d, label=None):
        return sorted(({"key": k, **({"label": label(k)} if label else {}), **{kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
                       for k, v in d.items()), key=lambda r: -r["cost_usd"])
    tasks_by_id = {t["id"]: t for t in tasks or []}
    return {"today": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out["today"].items()},
            "month": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out["month"].items()},
            "by_model": rows(out["by_model"]), "by_role": rows(out["by_role"]), "by_agent": rows(out["by_agent"]),
            "by_project": rows(out["by_project"], lambda k: names.get(k, "No project" if k == "none" else k)),
            "by_task": rows(out["by_task"], lambda k: (tasks_by_id.get(k) or {}).get("name") or k)[:15]}


def budget_meters(tasks, s: dict | None = None) -> list[dict]:
    """OpenRouter caps as budget meters for the Usage page and its alerts (orchestrator/org/usage.py)."""
    s = s or settings()
    out = []
    start = _month_start()
    days = (datetime.fromtimestamp(_next_month()) - datetime.fromtimestamp(start)).days
    elapsed = max(1.0, (time.time() - start) / 86400)

    def meter(key, label, cap, spent):
        pct = round(spent / cap * 100, 1) if cap else 0
        fc = round(spent / elapsed * days, 2)
        return {"kind": "openrouter", "key": key, "label": label, "budget_usd": cap, "spent_usd": round(spent, 2), "pct": pct,
                "forecast_usd": fc, "forecast_pct": round(fc / cap * 100, 1) if cap else 0, "state": "over" if pct >= 100 else "warn" if pct >= 80 else "ok",
                "hard_cap": True}
    if float(s.get("monthly_cap_usd") or 0):
        out.append(meter("openrouter", "OpenRouter (all projects)", float(s["monthly_cap_usd"]), spend(tasks, start)))
    if s.get("project_caps"):
        from .org import projects
        for pid, cap in s["project_caps"].items():
            p = projects.get(pid)
            if p and float(cap or 0):
                out.append(meter(f"openrouter:{pid}", f"OpenRouter · {p['name']}", float(cap), spend(tasks, start, pid)))
    return out


# ============================================================================ capacity (autopilot)
def capacity_state(model: str, project: str | None = None, tasks=None, acc: dict | None = None, s: dict | None = None) -> dict:
    """Can a role on OpenRouter with this model start now? Same shape as autopilot.limit_state."""
    s = s or settings()
    if not s.get("enabled", True):
        return {"ok": False, "reason": "OpenRouter is turned off in Settings → Model providers", "resets_at": None, "kind": "unavailable"}
    if not api_key(s):
        return {"ok": False, "reason": "OpenRouter has no API key (Settings → Model providers)", "resets_at": None, "kind": "unavailable"}
    cap = cap_state(tasks, project, s)
    free = is_free(model or s.get("default_model") or AUTO_FREE)
    if cap and not free:
        return {"ok": False, "reason": cap["reason"], "resets_at": cap["until"], "kind": "budget"}
    if acc is None:
        return {"ok": True, "reason": "", "resets_at": None, "kind": ""}  # no reading yet: do not hold the queue for it
    if not acc.get("ok"):
        if acc.get("status") == 401:
            return {"ok": False, "reason": acc.get("error") or "OpenRouter rejected the API key", "resets_at": None, "kind": "unavailable"}
        return {"ok": True, "reason": "", "resets_at": None, "kind": ""}  # unreachable: do not block, the turn will tell
    if free:
        fr = acc.get("free_requests") or {}
        if fr and int(fr.get("remaining") if fr.get("remaining") is not None else 1) <= 0:
            reset = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
            return {"ok": False, "reason": f"OpenRouter free requests used up for today ({fr.get('used')}/{fr.get('limit')})",
                    "resets_at": reset, "kind": "limit"}
        return {"ok": True, "reason": "", "resets_at": None, "kind": ""}
    left = spendable(acc)
    if left is not None and left <= 0:
        return {"ok": False, "reason": f"OpenRouter has no credits left for the paid model {model}", "resets_at": None, "kind": "balance"}
    return {"ok": True, "reason": "", "resets_at": None, "kind": ""}


# ============================================================================ agent support
# How each CLI runs on OpenRouter (verified against the CLI; see docs/OPENROUTER.md), or why it cannot.
SUPPORT = {
    "claude":   {"ok": True, "how": "Anthropic-compatible endpoint (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, a private config folder per run)",
                 "note": "Built for Anthropic models; other models may not follow Claude Code's tool protocol."},
    "codex":    {"ok": True, "how": "custom model provider on the Responses API (-c model_provider, env_key)"},
    "opencode": {"ok": True, "how": "built-in openrouter provider (OPENCODE_CONFIG_CONTENT, model openrouter/<id>)", "native_routing": True},
    "kilo":     {"ok": True, "how": "built-in openrouter provider (KILO_CONFIG_CONTENT, model openrouter/<id>)", "native_routing": True},
    "cline":    {"ok": True, "how": "OpenAI-compatible provider in a per-run data folder (-P openai-compatible)"},
    "goose":    {"ok": True, "how": "GOOSE_PROVIDER=openrouter with OPENROUTER_HOST and OPENROUTER_API_KEY"},
    "aider":    {"ok": True, "how": "litellm openrouter/<id> with OPENROUTER_API_BASE, routing in a model settings file", "native_routing": True},
    "crush":    {"ok": True, "how": "OpenAI-compatible provider in a per-run global config (CRUSH_GLOBAL_CONFIG)", "native_routing": True},
    "qwen":     {"ok": True, "how": "--auth-type openai with OPENAI_BASE_URL, OPENAI_API_KEY and OPENAI_MODEL"},
    "continue": {"ok": True, "how": "per-run config.yaml with provider openrouter (--config)", "native_routing": True},
    "copilot":  {"ok": True, "how": "bring-your-own-key provider (COPILOT_PROVIDER_BASE_URL, offline mode: no GitHub sign-in needed)"},
    "gemini":   {"ok": False, "why": "Gemini CLI only speaks Google's Gemini API; OpenRouter offers OpenAI- and Anthropic-style APIs."},
    "amp":      {"ok": False, "why": "Amp picks its own models on its own service; it has no custom provider setting."},
    "cursor":   {"ok": False, "why": "Cursor's agent only runs on Cursor's backend; it cannot use another provider."},
}


def supports(agent: str) -> bool:
    return bool((SUPPORT.get(agent) or {}).get("ok"))


def support_table() -> dict:
    return {a: dict(v) for a, v in SUPPORT.items()}


def model_label(model: str) -> str:
    return "Auto · best free model" if is_auto(model) else (model or "")
