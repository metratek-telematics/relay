"""What an agent's own CLI can tell us about models and the signed-in account.

Kilo Code and OpenCode list every model they can reach with prices (`models --verbose`),
Kilo reports the account balance (`profile`), and both keep usage totals (`stats`).
Other agents fall back to the editable model list in settings.
"""
from __future__ import annotations

import json
import re
import time

from . import config as C
from .util import DATA_DIR, quiet, read_json, write_json

CACHE_DIR = DATA_DIR / "cache" / "agents"
MODELS_TTL = 6 * 3600

# agent -> how to ask its CLI
SOURCES = {
    "kilo": {"models": ["models", "kilo", "--verbose"], "profile": ["profile"], "stats": ["stats"]},
    "opencode": {"models": ["models", "--verbose"], "stats": ["stats"]},
}


def _run(name: str, args: list[str], cfg: dict, timeout=120) -> str:
    from .agents import adapter
    ad = adapter(name)
    p = quiet([ad.binary, *args], timeout=timeout, env=ad.env(cfg))
    # INFO log lines from the CLI's logger are noise here.
    return "\n".join(l for l in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines() if not re.match(r"^\s*(INFO|DEBUG|WARN)\s+\d{4}-", l))


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
        out.append({
            "id": mid, "name": d.get("name") or mid,
            "input": cost.get("input"), "output": cost.get("output"),
            "free": _is_free(mid, d.get("name") or "", cost),
            "context": (d.get("limit") or {}).get("context"),
            "reasoning": bool(caps.get("reasoning")), "tools": bool(caps.get("toolcall", True)),
            "variants": sorted((d.get("variants") or {}).keys()),
            "status": d.get("status") or "active",
        })
    return out


def models(name: str, cfg: dict, refresh: bool = False) -> dict:
    src = SOURCES.get(name, {})
    configured = list((cfg.get("models") or {}).get(name) or [])
    if "models" not in src:
        return {"agent": name, "source": "settings", "models": [{"id": m, "name": m} for m in configured], "picked": configured}
    cache = CACHE_DIR / f"models-{name}.json"
    data = read_json(cache, None) if not refresh else None
    if not data or time.time() - float(data.get("at") or 0) > MODELS_TTL:
        rows = parse_verbose_models(_run(name, src["models"], cfg))
        if rows:
            data = {"at": time.time(), "models": rows}
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            write_json(cache, data)
        elif not data:
            return {"agent": name, "source": "cli", "models": [], "picked": configured,
                    "error": f"{C.AGENTS[name]['label']} did not list any models. Is it signed in?"}
    rows = [r for r in data["models"] if r.get("status") != "deprecated"]
    rows.sort(key=lambda r: (not r["free"], r["id"]))
    return {"agent": name, "source": "cli", "fetched": data["at"], "models": rows, "picked": configured,
            "free": sum(1 for r in rows if r["free"])}


def _kv_lines(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z][\w /&-]{1,40}):\s+(.+?)\s*$", line)
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


def account(name: str, cfg: dict) -> dict:
    src = SOURCES.get(name, {})
    out = {"agent": name, "profile": [], "usage": []}
    if not src:
        return out
    try:
        if "profile" in src:
            # The e-mail address is not needed to judge the account and stays out of the page.
            out["profile"] = [r for r in _kv_lines(_run(name, src["profile"], cfg, timeout=60)) if r["label"].lower() != "email"]
        if "stats" in src:
            out["usage"] = _box_lines(_run(name, src["stats"], cfg, timeout=60))
    except Exception as e:  # an unreachable account service must not break the page
        out["error"] = str(e)
    return out
