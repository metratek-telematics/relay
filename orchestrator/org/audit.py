"""Append-only audit log of every change: who, when, what, which object, and what it looked like before and after.

One JSON object per line in DATA_DIR/org/audit.jsonl. Each entry carries the SHA-256 of the previous entry
and its own, so editing or deleting a line in the middle breaks the chain and /api/org/audit/verify says where.
Secrets are masked before anything is written (common.mask): keys that name a secret and values that look
like one (tokens, webhook URLs, private keys).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading

from .common import ORG_DIR, clamp_text, mask, now_iso
from ..util import new_id

AUDIT_FILE = ORG_DIR / "audit.jsonl"
_lock = threading.Lock()
_tail = {"hash": None}
GENESIS = "0" * 64


def _canon(e: dict) -> str:
    return json.dumps({k: v for k, v in e.items() if k != "hash"}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _last_hash() -> str:
    if _tail["hash"]:
        return _tail["hash"]
    last = GENESIS
    try:
        with AUDIT_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = [l for l in f.read().splitlines() if l.strip()]
            if lines:
                last = json.loads(lines[-1]).get("hash") or GENESIS
    except FileNotFoundError:
        pass
    _tail["hash"] = last
    return last


def flatten(obj, prefix="", out=None, depth=0):
    out = {} if out is None else out
    if isinstance(obj, dict) and depth < 6:
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out, depth + 1)
    elif isinstance(obj, list) and len(obj) > 0 and all(isinstance(x, dict) for x in obj) and depth < 6:
        # Lists of objects with ids/names diff by identity, so a reorder is not "everything changed".
        for i, x in enumerate(obj):
            key = x.get("id") or x.get("name") or x.get("username") or i
            flatten(x, f"{prefix}[{key}]", out, depth + 1)
    else:
        out[prefix] = obj
    return out


def diff(before, after, limit=40) -> list[dict]:
    """Changed leaf values, masked and shortened."""
    if before is None and after is None:
        return []
    b, a = flatten(mask(before or {})), flatten(mask(after or {}))
    rows = []
    for k in sorted(set(b) | set(a)):
        if k.endswith(("updated_at", "last_seen", "last_used_at", "message_count", "process.state")) or b.get(k) == a.get(k):
            continue
        rows.append({"field": k, "before": clamp_text(json.dumps(b.get(k), ensure_ascii=False), 160) if k in b else None,
                     "after": clamp_text(json.dumps(a.get(k), ensure_ascii=False), 160) if k in a else None})
        if len(rows) >= limit:
            rows.append({"field": "…", "before": None, "after": f"{len(set(b) | set(a))} fields in total"})
            break
    return rows


def record(actor: dict | None, action: str, obj: dict | None = None, *, before=None, after=None, request: dict | None = None,
           status: int | None = None, outcome: str = "ok", via: str = "web", detail: str = "") -> dict:
    actor = actor or {}
    e = {
        "id": new_id("au"), "time": now_iso(),
        "actor": {"username": actor.get("username") or "system", "name": actor.get("name") or "", "role": actor.get("role") or ""},
        "via": via, "action": action, "object": obj or {}, "outcome": outcome, "status": status,
        "changes": diff(before, after) if (before is not None or after is not None) else [],
        "request": mask(request) if request else None, "detail": clamp_text(mask(detail), 300),
    }
    with _lock:
        e["prev"] = _last_hash()
        e["hash"] = hashlib.sha256(_canon(e).encode()).hexdigest()
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        try:
            AUDIT_FILE.chmod(0o600)
        except Exception:
            pass
        _tail["hash"] = e["hash"]
    return e


def _iter():
    try:
        with AUDIT_FILE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        yield {"corrupt": True, "raw": line[:200]}
    except FileNotFoundError:
        return


def query(q: str = "", actor: str = "", action: str = "", obj_type: str = "", outcome: str = "", since: str = "", until: str = "",
          project: str = "", limit: int = 200, offset: int = 0) -> dict:
    rows = []
    facets = {"actors": set(), "actions": set(), "types": set()}
    ql = (q or "").lower()
    for e in _iter():
        if e.get("corrupt"):
            continue
        facets["actors"].add((e.get("actor") or {}).get("username"))
        facets["actions"].add(e.get("action"))
        facets["types"].add((e.get("object") or {}).get("type"))
        if actor and (e.get("actor") or {}).get("username") != actor:
            continue
        if action and not str(e.get("action") or "").startswith(action):
            continue
        if obj_type and (e.get("object") or {}).get("type") != obj_type:
            continue
        if outcome and e.get("outcome") != outcome:
            continue
        if project and (e.get("object") or {}).get("project") != project:
            continue
        if since and e.get("time", "") < since:
            continue
        if until and e.get("time", "") > until:
            continue
        if ql and ql not in json.dumps(e, ensure_ascii=False).lower():
            continue
        rows.append(e)
    rows.reverse()
    return {"total": len(rows), "entries": rows[offset: offset + max(1, min(2000, limit))],
            "facets": {k: sorted(x for x in v if x) for k, v in facets.items()}}


def verify() -> dict:
    prev, n = GENESIS, 0
    for e in _iter():
        n += 1
        if e.get("corrupt"):
            return {"ok": False, "entries": n, "broken_at": n, "reason": "unreadable line"}
        if e.get("prev") != prev:
            return {"ok": False, "entries": n, "broken_at": n, "id": e.get("id"), "reason": "an entry before this one was changed or removed"}
        if hashlib.sha256(_canon(e).encode()).hexdigest() != e.get("hash"):
            return {"ok": False, "entries": n, "broken_at": n, "id": e.get("id"), "reason": "this entry was edited"}
        prev = e["hash"]
    return {"ok": True, "entries": n, "head": prev}


def to_csv(entries: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "actor", "role", "via", "action", "object_type", "object_id", "object_name", "project", "outcome", "status", "changes", "detail", "hash"])
    for e in entries:
        o = e.get("object") or {}
        ch = "; ".join(f"{c['field']}: {c.get('before')} → {c.get('after')}" for c in e.get("changes") or [])
        cells = [e.get("time"), (e.get("actor") or {}).get("username"), (e.get("actor") or {}).get("role"), e.get("via"), e.get("action"),
                 o.get("type"), o.get("id"), o.get("name"), o.get("project"), e.get("outcome"), e.get("status"), ch, e.get("detail"), e.get("hash")]
        # Spreadsheet formula injection: a cell starting with = + - @ is text, not a formula.
        w.writerow([("'" + str(c)) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in cells])
    return buf.getvalue()
