"""Notifications beyond the browser tab: email, Slack, Telegram, Discord, signed webhooks and Web Push.

Every Relay notification (manager.notify) becomes an event (needs_input, approval, delivered, failed, digest,
budget). Each person chooses, per event, the channels that reach them; organisation webhooks receive every
event they subscribe to. Deliveries go through one worker thread:

    rate limit   a token bucket per target (channel + address), so a burst of failures cannot flood a chat
    retries      network errors, 408/425/429 and 5xx retry with exponential backoff (Retry-After honoured)
    quiet hours  external channels stay silent inside a person's quiet hours (logged as suppressed)
    log          every delivery, with attempts and the last error, in DATA_DIR/org/deliveries.jsonl

Messages are actionable where the channel allows: Slack and Discord link straight to the item in Relay;
Telegram adds inline buttons (Approve / Request changes, or the agent's answer options) that act as the
linked Relay user; webhooks carry the task, its pending question and links, signed with HMAC-SHA256.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import heapq
import ipaddress
import json
import random
import re
import secrets
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urlparse

from . import identity, projects, settings as OS, webpush
from .common import JsonStore, ORG_DIR, clamp_text, mask, now_iso
from ..util import new_id

EVENT_OF_KIND = {"needs_input": "needs_input", "approval": "approval", "delivered": "delivered", "pr_opened": "delivered",
                 "failed": "failed", "digest": "digest", "budget": "budget"}
EVENT_LABEL = {"needs_input": "Needs input", "approval": "Approval needed", "delivered": "Delivered / pull request",
               "failed": "Failure", "digest": "Digest", "budget": "Budget alert"}
EXTERNAL = ("email", "slack", "telegram", "discord", "webhook", "browser")
DELIVERY_FILE = ORG_DIR / "deliveries.jsonl"
RETRYABLE = {408, 425, 429}

_actions = JsonStore("telegram_actions.json", {"actions": {}})
_subs = JsonStore("webpush_subscriptions.json", {"subscriptions": []})


# ============================================================================ pure helpers (unit tested)
class TokenBucket:
    def __init__(self, per_minute: float, clock=time.monotonic):
        self.capacity = max(1.0, float(per_minute))
        self.rate = self.capacity / 60.0
        self.tokens = self.capacity
        self.clock = clock
        self.at = clock()

    def take(self) -> float:
        """0 when allowed now, else seconds until a token is available."""
        t = self.clock()
        self.tokens = min(self.capacity, self.tokens + (t - self.at) * self.rate)
        self.at = t
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


def backoff(attempt: int, retry_after: float | None = None, base: float = 2.0, cap: float = 300.0, rng=random.random) -> float:
    """Seconds before retry number `attempt` (1-based): base·2^(n-1) with ±20 % jitter, or Retry-After."""
    if retry_after is not None and retry_after >= 0:
        return min(cap, float(retry_after))
    d = min(cap, base * (2 ** max(0, attempt - 1)))
    return round(d * (0.8 + 0.4 * rng()), 3)


def retryable(status: int | None) -> bool:
    return status is None or status in RETRYABLE or status >= 500


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """X-Relay-Signature: sha256=HMAC_SHA256(secret, "<timestamp>.<body>")."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify_signature(secret: str, timestamp: str, body: bytes, header: str, tolerance: int = 300, now: float | None = None) -> bool:
    try:
        if abs((now or time.time()) - int(timestamp)) > tolerance:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), header or "")


def in_quiet_hours(q: dict, at: datetime | None = None) -> bool:
    if not q or not q.get("enabled"):
        return False
    at = at or datetime.now()
    cur = at.hour * 60 + at.minute
    s = [int(x) for x in str(q.get("start") or "22:00").split(":")]
    e = [int(x) for x in str(q.get("end") or "07:00").split(":")]
    a, b = s[0] * 60 + s[1], e[0] * 60 + e[1]
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)


def target_allowed(url: str, allow_private: bool) -> tuple[bool, str]:
    """Outbound URLs must be http(s); private, loopback and link-local hosts only when the organisation allows it."""
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, "Only http(s) addresses can receive notifications."
    if allow_private:
        return True, ""
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, f"{u.hostname} does not resolve."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False, f"{u.hostname} is a private address; an owner can allow private targets under Integrations."
    return True, ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_post(url: str, body: bytes, headers: dict, timeout: float = 10.0) -> tuple[int | None, str, float | None]:
    """(status, short response text, retry_after). Status None means the request never got an answer."""
    req = urllib.request.Request(url, data=body, method="POST", headers={"User-Agent": "Relay-Notifications/1", **headers})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.read(600).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        try:
            ra = float(ra) if ra is not None else None
        except ValueError:
            ra = None
        try:
            txt = e.read(600).decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, ra
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:300], None


# ============================================================================ messages
def base_url() -> str:
    return (OS.load()["integrations"].get("public_url") or _seen_base["url"] or "").rstrip("/")


_seen_base = {"url": ""}


def remember_base(url: str):
    if url and not _seen_base["url"]:
        _seen_base["url"] = url.rstrip("/")


def build_message(manager, n: dict, event: str) -> dict:
    t = manager.store.get(n.get("task_id")) if n.get("task_id") else None
    base = base_url()
    msg = {"id": n.get("id") or new_id("n"), "event": event, "kind": n.get("kind"), "level": n.get("level"),
           "title": n.get("title") or "", "body": n.get("body") or "", "time": n.get("time") or now_iso(), "task": None,
           "links": {"relay": base or None}}
    if t:
        pid = projects.project_of_task(t)
        p = projects.get(pid) or {}
        pending = t.get("pending") or {}
        msg["task"] = {"id": t["id"], "number": t.get("number"), "name": t.get("name"), "status": t.get("status"),
                       "project": {"id": pid, "name": p.get("name")}, "repo": t.get("github_repo") or "",
                       "branch": t.get("branch_name"), "pr_url": t.get("pr_url"), "created_by": t.get("created_by"),
                       "reviewers": t.get("reviewers") or [],
                       "pending": {"id": pending.get("id"), "kind": pending.get("kind"), "question": clamp_text(pending.get("question"), 900),
                                   "options": list(pending.get("options") or [])[:6]} if pending else None,
                       "cost_usd": ((t.get("metrics") or {}).get("total") or {}).get("cost_usd")}
        if base:
            msg["links"]["task"] = f"{base}/#/task/{t['id']}"
            msg["links"]["inbox"] = f"{base}/#/inbox"
    if event == "digest" and base:
        msg["links"]["digest"] = f"{base}/#/digest"
    if event == "budget" and base:
        msg["links"]["usage"] = f"{base}/#/org/usage"
    return msg


def primary_link(msg) -> str | None:
    L = msg.get("links") or {}
    if msg["event"] in ("needs_input", "approval"):
        return L.get("inbox") or L.get("task")
    return L.get("task") or L.get("digest") or L.get("usage") or L.get("relay")


def _headline(msg) -> str:
    t = msg.get("task") or {}
    ref = f"#{t['number']} {t.get('name')}" if t.get("number") else (t.get("name") or "")
    return f"{msg['title']}" + (f" · {ref}" if ref and ref not in msg["title"] else "")


def slack_payload(msg) -> dict:
    t = msg.get("task") or {}
    link = primary_link(msg)
    text = f"*{_headline(msg)}*" + (f"\n{msg['body']}" if msg.get("body") else "")
    q = (t.get("pending") or {}).get("question")
    if q and msg["event"] in ("needs_input", "approval"):
        text += f"\n>{clamp_text(q, 600)}"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]
    ctx = [x for x in [(t.get("project") or {}).get("name"), t.get("repo"), t.get("branch")] if x]
    if ctx:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(ctx)}]})
    buttons = []
    if link:
        label = {"needs_input": "Answer in Relay", "approval": "Review and approve"}.get(msg["event"], "Open in Relay")
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": label}, "url": link, "style": "primary"})
    if t.get("pr_url"):
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Pull request"}, "url": t["pr_url"]})
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    return {"text": _headline(msg), "blocks": blocks}


def discord_payload(msg) -> dict:
    t = msg.get("task") or {}
    color = {"success": 0x3A8F62, "error": 0xC5473F, "warning": 0xB9842D}.get(msg.get("level"), 0x4F6FD0)
    desc = msg.get("body") or ""
    q = (t.get("pending") or {}).get("question")
    if q:
        desc += f"\n> {clamp_text(q, 500)}"
    link = primary_link(msg)
    if link:
        desc += f"\n[{'Answer in Relay' if msg['event'] == 'needs_input' else 'Open in Relay'}]({link})"
    embed = {"title": clamp_text(_headline(msg), 240), "description": desc[:3800], "color": color, "timestamp": msg["time"]}
    if (t.get("project") or {}).get("name"):
        embed["footer"] = {"text": f"{t['project']['name']} · Relay"}
    return {"username": "Relay", "embeds": [embed], "allowed_mentions": {"parse": []}}


def webhook_payload(msg) -> dict:
    return {"type": f"relay.{msg['event']}", **{k: v for k, v in msg.items() if k != "event"}, "event": msg["event"]}


def email_parts(msg) -> tuple[str, str, str]:
    subject = f"[Relay] {_headline(msg)}"
    t = msg.get("task") or {}
    link = primary_link(msg)
    lines = [msg.get("body") or ""]
    q = (t.get("pending") or {}).get("question")
    if q:
        lines += ["", q]
    if link:
        lines += ["", f"Open in Relay: {link}"]
    text = "\n".join(lines).strip() + "\n\n— Relay (change what reaches you under Profile → Notifications)\n"
    import html as H
    btn = f'<p><a href="{H.escape(link)}" style="background:#d97757;color:#fff;padding:9px 16px;border-radius:6px;text-decoration:none;font-weight:600">Open in Relay</a></p>' if link else ""
    html = (f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px;color:#23221f;max-width:560px">'
            f'<h2 style="font-size:17px;margin:0 0 8px">{H.escape(_headline(msg))}</h2><p>{H.escape(msg.get("body") or "")}</p>'
            + (f'<blockquote style="border-left:3px solid #e2ded4;margin:0;padding:4px 12px;color:#5f5a51">{H.escape(q)}</blockquote>' if q else "")
            + f'{btn}<p style="color:#938c80;font-size:12px">Change what reaches you under Profile → Notifications in Relay.</p></div>')
    return subject, text, html


# ============================================================================ Telegram actions
def _action_key(tid: str, qid: str, action: str, value: str = "") -> str:
    key = secrets.token_urlsafe(9)

    def fn(d):
        rows = d.get("actions") or {}
        rows[key] = {"tid": tid, "qid": qid, "action": action, "value": value, "created": time.time()}
        if len(rows) > 800:
            for k in sorted(rows, key=lambda k: rows[k]["created"])[: len(rows) - 800]:
                rows.pop(k)
        d["actions"] = rows
    _actions.update(fn)
    return key


def take_action(key: str) -> dict | None:
    return (_actions.read().get("actions") or {}).get(key)


def telegram_payload(msg, chat_id: str) -> dict:
    t = msg.get("task") or {}
    import html as H
    text = f"<b>{H.escape(_headline(msg))}</b>"
    if msg.get("body"):
        text += f"\n{H.escape(msg['body'])}"
    pend = t.get("pending") or {}
    if pend.get("question") and msg["event"] in ("needs_input", "approval"):
        text += f"\n\n<blockquote>{H.escape(clamp_text(pend['question'], 700))}</blockquote>"
    rows = []
    if t.get("id") and pend.get("id") and msg["event"] == "approval":
        rows.append([{"text": "✅ Approve", "callback_data": "rl:" + _action_key(t["id"], pend["id"], "approve")},
                     {"text": "↩️ Request changes", "callback_data": "rl:" + _action_key(t["id"], pend["id"], "reject")}])
    elif t.get("id") and pend.get("id") and pend.get("options"):
        for o in pend["options"][:4]:
            rows.append([{"text": clamp_text(o, 60), "callback_data": "rl:" + _action_key(t["id"], pend["id"], "answer", o)}])
    link = primary_link(msg)
    if link and link.startswith("https://"):  # Telegram refuses plain-http and private URLs for buttons
        rows.append([{"text": "Open in Relay", "url": link}])
    out = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if rows:
        out["reply_markup"] = {"inline_keyboard": rows}
    return out


# ============================================================================ dispatcher
class Dispatcher:
    def __init__(self, manager, http=http_post, smtp_factory=None, clock=time.monotonic, autostart=True):
        self.m = manager
        self.http = http
        self.smtp_factory = smtp_factory
        self.clock = clock
        self.cv = threading.Condition()
        self.heap: list = []
        self.seq = 0
        self.buckets: dict[str, TokenBucket] = {}
        self.log: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.waiters: dict[str, threading.Event] = {}
        self._load_log()
        self.thread = None
        if autostart:
            self.start()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="relay-notify", daemon=True)
        self.thread.start()

    def _load_log(self):
        try:
            with DELIVERY_FILE.open("r", encoding="utf-8") as f:
                lines = collections.deque(f, maxlen=500)
            for line in lines:
                try:
                    row = json.loads(line)
                    self.log[row["id"]] = row
                except Exception:
                    pass
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ entry points
    def on_notify(self, n: dict):
        event = EVENT_OF_KIND.get(n.get("kind") or "")
        if not event:
            return []
        msg = build_message(self.m, n, event)
        return self.route(msg)

    def route(self, msg: dict, only_user: str | None = None) -> list[str]:
        """Queue deliveries for everyone who asked for this event, plus organisation webhooks."""
        org = OS.load()["integrations"]
        ids, seen = [], set()
        t = msg.get("task") or {}
        pid = (t.get("project") or {}).get("id")
        reviewers = set(t.get("reviewers") or [])
        for u in identity.users():
            if u.get("disabled") or (only_user and u["username"] != only_user):
                continue
            if msg["event"] == "approval" and reviewers and u["username"] not in reviewers and identity.LEVEL.get(u.get("role"), 0) < identity.LEVEL["admin"]:
                continue  # a project's reviewers are asked first; admins still hear about it
            if msg["event"] in ("needs_input", "approval") and identity.LEVEL.get(u.get("role"), 0) < identity.LEVEL["member"]:
                continue  # viewers cannot answer
            if msg["event"] == "budget" and identity.LEVEL.get(u.get("role"), 0) < identity.LEVEL["admin"]:
                continue
            n = u["prefs"]["notifications"]
            if pid and n.get("projects") and pid not in n["projects"]:
                continue
            chans = [c for c in n["events"].get(msg["event"]) or [] if c in EXTERNAL]
            quiet = in_quiet_hours(n.get("quiet_hours") or {})
            for ch in chans:
                target = self._target(u, ch, org)
                if not target:
                    continue
                key = (ch, target.get("key"))
                if key in seen:
                    continue
                seen.add(key)
                ids.append(self.enqueue(ch, target, msg, u["username"], suppressed="quiet hours" if quiet else None))
        if not only_user:
            for w in org.get("webhooks") or []:
                if not w.get("enabled", True) or not w.get("url"):
                    continue
                if w.get("events") and msg["event"] not in w["events"]:
                    continue
                if w.get("projects") and pid not in w["projects"]:
                    continue
                target = {"key": "org:" + w["id"], "url": w["url"], "secret": w.get("secret") or "", "label": w.get("name") or w["url"]}
                ids.append(self.enqueue("webhook", target, msg, None))
        return ids

    def _target(self, u: dict, ch: str, org: dict) -> dict | None:
        c = u["prefs"]["notifications"]["channels"]
        if ch == "email":
            addr = (c.get("email") or {}).get("address") or u.get("email")
            return {"key": addr, "address": addr, "label": addr} if addr and org["smtp"].get("host") else None
        if ch == "slack":
            url = (c.get("slack") or {}).get("webhook_url") or org["slack"].get("webhook_url")
            return {"key": url, "url": url, "label": "Slack (" + ("personal" if (c.get("slack") or {}).get("webhook_url") else "team") + ")"} if url else None
        if ch == "discord":
            url = (c.get("discord") or {}).get("webhook_url") or org["discord"].get("webhook_url")
            return {"key": url, "url": url, "label": "Discord"} if url else None
        if ch == "telegram":
            chat = (c.get("telegram") or {}).get("chat_id")
            return {"key": f"tg:{chat}", "chat_id": chat, "label": f"Telegram {chat}"} if chat and org["telegram"].get("bot_token") else None
        if ch == "webhook":
            w = c.get("webhook") or {}
            return {"key": w["url"], "url": w["url"], "secret": w.get("secret") or "", "label": w["url"]} if w.get("url") else None
        if ch == "browser":
            subs = [s for s in _subs.read().get("subscriptions") or [] if s.get("username") == u["username"]]
            wp = org.get("webpush") or {}
            return {"key": f"push:{u['username']}", "subs": subs, "label": f"{len(subs)} browser(s)"} if subs and wp.get("private_key") else None
        return None

    def enqueue(self, channel: str, target: dict, msg: dict, username: str | None, suppressed: str | None = None) -> str:
        did = new_id("dl")
        row = {"id": did, "time": now_iso(), "channel": channel, "target": mask(target.get("label") or ""), "username": username,
               "event": msg["event"], "title": clamp_text(msg.get("title"), 140), "task_id": (msg.get("task") or {}).get("id"),
               "status": "queued", "attempts": 0, "error": None, "http_status": None}
        with self.cv:
            self.log[did] = row
            while len(self.log) > 500:
                self.log.popitem(last=False)
            if suppressed:
                row.update(status="suppressed", error=suppressed)
                self._persist(row)
                return did
            self.seq += 1
            heapq.heappush(self.heap, (self.clock(), self.seq, {"id": did, "channel": channel, "target": target, "msg": msg}))
            self.cv.notify()
        return did

    def wait(self, did: str, timeout: float = 20) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            row = self.log.get(did) or {}
            if row.get("status") in ("delivered", "failed", "suppressed", "dropped"):
                return row
            time.sleep(0.1)
        return self.log.get(did) or {}

    def recent(self, limit=200, username: str | None = None) -> list[dict]:
        rows = list(self.log.values())[::-1]
        if username:
            rows = [r for r in rows if r.get("username") == username]
        return rows[:limit]

    # ------------------------------------------------------------ worker
    def _loop(self):
        while True:
            with self.cv:
                while not self.heap:
                    self.cv.wait(timeout=30)
                due, _, job = self.heap[0]
                wait = due - self.clock()
                if wait > 0:
                    self.cv.wait(timeout=min(wait, 5))
                    continue
                heapq.heappop(self.heap)
            try:
                self.process(job)
            except Exception as e:  # never kill the worker
                self._finish(job["id"], "failed", f"internal error: {e}")

    def process(self, job: dict):
        org = OS.load()["integrations"]
        row = self.log.get(job["id"])
        if row is None:
            return
        bucket_key = f"{job['channel']}:{job['target'].get('key')}"
        b = self.buckets.get(bucket_key)
        if not b or b.capacity != float(org.get("rate_limit_per_minute") or 20):
            b = self.buckets[bucket_key] = TokenBucket(org.get("rate_limit_per_minute") or 20, clock=self.clock)
        delay = b.take()
        if delay > 0:
            row["status"] = "rate_limited"
            row["error"] = f"rate limited, sending in {delay:.0f}s"
            if self.clock() - job.setdefault("first_limited", self.clock()) > 900:
                return self._finish(job["id"], "dropped", "rate limited for more than 15 minutes")
            return self._requeue(job, delay)
        row["attempts"] += 1
        row["status"] = "sending"
        try:
            status, text, retry_after = self.send(job["channel"], job["target"], job["msg"], org)
        except PermissionError as e:
            return self._finish(job["id"], "failed", str(e))
        row["http_status"] = status
        if status is not None and 200 <= status < 300:
            return self._finish(job["id"], "delivered", None)
        err = f"HTTP {status}: {clamp_text(mask(text), 200)}" if status else clamp_text(mask(text), 200)
        row["error"] = err
        if retryable(status) and row["attempts"] < int(org.get("max_attempts") or 5):
            row["status"] = "retrying"
            d = backoff(row["attempts"], retry_after)
            row["next_attempt_in"] = d
            return self._requeue(job, d)
        self._finish(job["id"], "failed", err)

    def _requeue(self, job, delay):
        with self.cv:
            self.seq += 1
            heapq.heappush(self.heap, (self.clock() + delay, self.seq, job))
            self.cv.notify()

    def _finish(self, did, status, error):
        row = self.log.get(did)
        if not row:
            return
        row.update(status=status, error=error, finished=now_iso())
        row.pop("next_attempt_in", None)
        self._persist(row)

    def _persist(self, row):
        try:
            with DELIVERY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------ channels
    def send(self, channel: str, target: dict, msg: dict, org: dict) -> tuple[int | None, str, float | None]:
        allow_private = bool(org.get("allow_private_targets"))
        if channel in ("slack", "discord", "webhook"):
            ok, why = target_allowed(target["url"], allow_private)
            if not ok:
                raise PermissionError(why)
        if channel == "slack":
            return self.http(target["url"], json.dumps(slack_payload(msg)).encode(), {"Content-Type": "application/json"})
        if channel == "discord":
            return self.http(target["url"], json.dumps(discord_payload(msg)).encode(), {"Content-Type": "application/json"})
        if channel == "webhook":
            body = json.dumps(webhook_payload(msg), ensure_ascii=False).encode()
            ts = str(int(time.time()))
            headers = {"Content-Type": "application/json", "X-Relay-Event": msg["event"], "X-Relay-Delivery": msg["id"], "X-Relay-Timestamp": ts}
            if target.get("secret"):
                headers["X-Relay-Signature"] = sign(target["secret"], ts, body)
            return self.http(target["url"], body, headers)
        if channel == "telegram":
            tg = org["telegram"]
            api = (tg.get("api_base") or "https://api.telegram.org").rstrip("/")
            ok, why = target_allowed(api, allow_private or api == "https://api.telegram.org")
            if not ok:
                raise PermissionError(why)
            return self.http(f"{api}/bot{tg['bot_token']}/sendMessage", json.dumps(telegram_payload(msg, target["chat_id"])).encode(),
                             {"Content-Type": "application/json"})
        if channel == "email":
            return self._email(target["address"], msg, org["smtp"])
        if channel == "browser":
            return self._push(target, msg, org)
        return None, f"unknown channel {channel}", None

    def _email(self, to: str, msg: dict, smtp: dict):
        subject, text, html = email_parts(msg)
        em = EmailMessage()
        em["Subject"], em["From"], em["To"] = subject, smtp.get("from") or smtp.get("username") or "relay@localhost", to
        em.set_content(text)
        em.add_alternative(html, subtype="html")
        try:
            factory = self.smtp_factory or (smtplib.SMTP_SSL if smtp.get("security") == "ssl" else smtplib.SMTP)
            with factory(smtp["host"], int(smtp.get("port") or 587), timeout=15) as s:
                if smtp.get("security") == "starttls":
                    s.starttls(context=ssl.create_default_context())
                if smtp.get("username"):
                    s.login(smtp["username"], smtp.get("password") or "")
                s.send_message(em)
            return 250, "sent", None
        except smtplib.SMTPResponseException as e:
            # 4xx SMTP replies are temporary; treat them like a 503 so they retry.
            return (503 if 400 <= e.smtp_code < 500 else 400), f"SMTP {e.smtp_code}", None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}", None

    def _push(self, target: dict, msg: dict, org: dict):
        wp = org.get("webpush") or {}
        if not webpush.AVAILABLE:
            return 400, "Web Push needs the cryptography package on the server", None
        worst, gone = None, []
        for sub in target.get("subs") or []:
            ok, why = target_allowed(sub["endpoint"], True)
            if not ok:
                continue
            headers = webpush.vapid_headers(sub["endpoint"], wp["private_key"], wp["public_key"], wp.get("subject") or "")
            headers["Content-Length"] = "0"
            status, text, ra = self.http(sub["endpoint"], b"", headers)
            if status in (404, 410):
                gone.append(sub["endpoint"])
                continue
            if status is None or status >= 300:
                worst = (status, text, ra)
        if gone:
            _subs.update(lambda d: {**d, "subscriptions": [s for s in d.get("subscriptions") or [] if s["endpoint"] not in gone]})
        return worst or (201, "pushed", None)


# ============================================================================ Web Push subscriptions
def add_subscription(username: str, sub: dict, ua: str = ""):
    ep = str((sub or {}).get("endpoint") or "")
    if not ep.startswith("https://"):
        raise ValueError("A push subscription needs an https endpoint.")
    row = {"username": username, "endpoint": ep, "keys": (sub or {}).get("keys") or {}, "created_at": now_iso(), "ua": clamp_text(ua, 120)}
    _subs.update(lambda d: {**d, "subscriptions": [s for s in d.get("subscriptions") or [] if s["endpoint"] != ep] + [row]})


def remove_subscription(username: str, endpoint: str):
    _subs.update(lambda d: {**d, "subscriptions": [s for s in d.get("subscriptions") or [] if not (s["endpoint"] == endpoint and s["username"] == username)]})


def subscriptions(username: str) -> list[dict]:
    return [{"endpoint": s["endpoint"][:60] + "…", "created_at": s.get("created_at"), "ua": s.get("ua")} for s in _subs.read().get("subscriptions") or [] if s["username"] == username]


def ensure_vapid() -> dict:
    data = OS.load()
    wp = data["integrations"].get("webpush") or {}
    if wp.get("private_key") and wp.get("public_key"):
        return wp
    keys = webpush.generate_keys()
    OS.save({"integrations": {"webpush": {**wp, **keys}}})
    return keys


def telegram_link_code(username: str) -> str:
    """A short code a person sends the bot (/link CODE) so button presses act as them."""
    return hmac.new(_link_secret().encode(), username.encode(), hashlib.sha256).hexdigest()[:10]


def _link_secret() -> str:
    data = OS.load()
    s = data["integrations"]["telegram"].get("webhook_secret")
    if not s:
        s = secrets.token_urlsafe(24)
        OS.save({"integrations": {"telegram": {"webhook_secret": s}}})
    return s


def user_for_link_code(code: str) -> dict | None:
    code = re.sub(r"[^0-9a-f]", "", (code or "").lower())
    for u in identity.users():
        if hmac.compare_digest(telegram_link_code(u["username"]), code):
            return u
    return None
