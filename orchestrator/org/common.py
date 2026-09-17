"""Shared helpers for the organisation layer: where it stores things, and how secrets are kept out of sight."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..util import DATA_DIR, read_json, write_json

ORG_DIR = DATA_DIR / "org"
ORG_DIR.mkdir(parents=True, exist_ok=True)

MASK = "••••••••"

# Keys whose values never leave the server and never reach the audit log.
SECRET_KEY_RX = re.compile(r"(pass(word)?|secret|token|api[_-]?key|private|webhook_url|authorization|cookie|dsn|credential|signing|smtp_pass|bot_token|p256dh|auth_key)", re.I)
# Values that look like credentials wherever they appear.
SECRET_VALUE_RX = re.compile(
    r"(rly_[0-9a-f]{8}_[A-Za-z0-9_-]{20,}"          # Relay personal access tokens
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"                # Slack tokens
    r"|sk-[A-Za-z0-9_-]{20,}"                       # API keys
    r"|\d{6,12}:[A-Za-z0-9_-]{30,}"                 # Telegram bot tokens
    r"|https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"
    r"|https://(?:discord|discordapp)\.com/api/webhooks/[A-Za-z0-9/_-]+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s) -> float:
    if not s:
        return 0.0
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.astimezone()
        return d.timestamp()
    except Exception:
        return 0.0


def mask_value(v):
    if isinstance(v, str):
        return SECRET_VALUE_RX.sub(MASK, v)
    return v


def mask(obj, key: str = "", depth: int = 0):
    """A copy with secret-looking keys blanked and credential-looking strings replaced, recursively."""
    if depth > 12:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if SECRET_KEY_RX.search(ks) and not ks.startswith("has_") and v not in (None, "", [], {}, False, True) and not isinstance(v, (int, float)):
                out[ks] = MASK
            else:
                out[ks] = mask(v, ks, depth + 1)
        return out
    if isinstance(obj, list):
        return [mask(x, key, depth + 1) for x in obj]
    return mask_value(obj)


class JsonStore:
    """A small JSON document on disk with a lock, cached in memory and re-read when the file changes."""

    def __init__(self, name: str, default):
        self.path: Path = ORG_DIR / name
        self.default = default
        self.lock = threading.RLock()
        self._data = None
        self._mtime = None

    def _fresh(self):
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            m = None
        if self._data is None or m != self._mtime:
            data = read_json(self.path, None)
            self._data = data if data is not None else json.loads(json.dumps(self.default))
            self._mtime = m
        return self._data

    def read(self):
        with self.lock:
            return json.loads(json.dumps(self._fresh()))

    def exists(self) -> bool:
        return self.path.exists()

    def write(self, data):
        with self.lock:
            write_json(self.path, data)
            try:
                self.path.chmod(0o600)
            except Exception:
                pass
            self._data = json.loads(json.dumps(data))
            try:
                self._mtime = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                self._mtime = None
            return data

    def update(self, fn):
        """fn(data) mutates or returns new data; the result is written atomically under the lock."""
        with self.lock:
            data = self.read()
            out = fn(data)
            return self.write(data if out is None else out)


def clamp_text(s, n=200) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def epoch() -> float:
    return time.time()
