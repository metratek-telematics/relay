"""Relay's two-way Telegram assistant.

Transport
    polling   (default) a background getUpdates loop: nothing has to be reachable from the internet. The offset is
              persisted before an update is handled (an approval is never applied twice after a crash), errors back
              off exponentially, and a file lock keeps a second Relay on the same data folder from polling too.
    webhook   Telegram calls /api/org/integrations/telegram/webhook (public, secret-token checked); same handler.
    off       Relay only sends.

Who may act
    A chat is tied to a Relay person with /link CODE (Profile → Notifications). Every action runs as that person, is
    checked against the same role matrix as the web API (rbac.rule_for on the equivalent HTTP route) and audited
    with via="telegram". Unknown chats get one reply explaining how to link and are otherwise ignored. Group chats
    only when an admin allows them. Per-person rate limits.

Talking to Relay
    Notifications carry the task, the actual question / design summary / findings, answer buttons and a link. The
    message ids are remembered, so a plain reply to a notification answers that exact pending question, or becomes
    guidance for the task (resuming it when it has ended, like the web composer). Commands cover status, the inbox,
    tasks, steering, approvals, new tasks and merging; anything else goes to the concierge (concierge.py), a cheap
    tool-less agent turn that answers from live context and may only PROPOSE actions as confirm buttons.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import audit, identity, notify, projects, rbac, settings as OS
from .common import JsonStore, ORG_DIR, clamp_text, mask, now_iso, parse_iso

try:
    import fcntl
except ImportError:  # Windows: no advisory locks; one Relay per data folder is already enforced by the port check
    fcntl = None

LIMIT = 4096
DEFAULT_API = "https://api.telegram.org"
ALLOWED_UPDATES = ["message", "callback_query"]
MODES = ("polling", "webhook", "off")
PHASE_LABEL = {"kickoff": "planning", "design": "design", "dialogue": "implementing", "review": "review", "deliver": "delivering",
               "done": "done", "delivered": "done"}
EVENT_ICON = {"needs_input": "❓", "approval": "🟢", "delivered": "🚀", "failed": "❌", "digest": "📊", "budget": "💰"}

_state = JsonStore("telegram_state.json", {"offset": 0, "greeted": {}, "bot": {}})
_threads = JsonStore("telegram_threads.json", {"messages": {}})
_chats = JsonStore("telegram_chats.json", {"chats": {}})
_confirms = JsonStore("telegram_confirms.json", {"items": {}})

MAX_THREADS = 3000
CONFIRM_TTL = 3600 * 24


# ============================================================================ formatting (pure, unit tested)
def h(s) -> str:
    """Text for parse_mode=HTML: Telegram needs &, < and > escaped (quotes may stay)."""
    return html.escape("" if s is None else str(s), quote=False)


def md_lite(s: str) -> str:
    """Escape, then turn the agents' **bold** and `code` into HTML. Pairs only, so tags always balance."""
    out = h(s)
    out = re.sub(r"`([^`\n]{1,300})`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*\n][^*]{0,300}?)\*\*", r"<b>\1</b>", out)
    return out


def utf16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


_TAG = re.compile(r"<(/?)([a-z][a-z0-9-]*)(?:\s[^>]*)?>", re.I)


def _text_tokens(s: str, piece: int = 1000) -> list[str]:
    out = []
    for line in s.splitlines(keepends=True):
        while len(line) > piece:
            cut = piece
            amp, semi = line.rfind("&", 0, cut), line.rfind(";", 0, cut)
            if amp > semi and cut - amp < 10:  # never split an entity such as &amp;
                cut = amp
            out.append(line[:cut])
            line = line[cut:]
        if line:
            out.append(line)
    return out


def split_html(text: str, limit: int = LIMIT) -> list[str]:
    """Split a parse_mode=HTML message into parts Telegram accepts: each within `limit` UTF-16 units, cut at line
    boundaries where possible, with open tags closed at the end of a part and reopened at the start of the next."""
    if utf16len(text) <= limit:
        return [text]
    tokens: list[tuple] = []
    pos = 0
    for mt in _TAG.finditer(text):
        if mt.start() > pos:
            tokens += [("text", t) for t in _text_tokens(text[pos:mt.start()])]
        tokens.append(("tag", mt.group(0), mt.group(1) == "/", mt.group(2).lower()))
        pos = mt.end()
    if pos < len(text):
        tokens += [("text", t) for t in _text_tokens(text[pos:])]
    parts, stack = [], []
    cur, has_text = "", False

    def closing():
        return "".join(f"</{name}>" for name, _ in reversed(stack))

    for tok in tokens:
        if tok[0] == "text":
            t = tok[1]
            if has_text and utf16len(cur + t + closing()) > limit:
                parts.append(cur + closing())
                cur, has_text = "".join(o for _, o in stack), False
            cur += t
            has_text = has_text or bool(t.strip())
        else:
            _, raw, is_close, name = tok
            if is_close:
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == name:
                        stack.pop(i)
                        break
            else:
                stack.append((name, raw))
            cur += raw
    if has_text:
        parts.append(cur + closing())
    return [p.strip("\n") for p in parts if p.strip()]


def parse_command(text: str, bot_username: str = "") -> tuple[str, bool, str] | None:
    """('say', bang, 'rest') for '/say! 12 text' or '/say@RelayBot 12 text'; None when the text is not a command.
    A command addressed to another bot (/cmd@OtherBot) is not ours."""
    m = re.match(r"^/([A-Za-z_]{1,32})(!?)(?:@([A-Za-z0-9_]{3,64}))?(?:\s+([\s\S]*))?$", (text or "").strip())
    if not m:
        return None
    if m.group(3) and bot_username and m.group(3).lower() != bot_username.lower():
        return None
    return m.group(1).lower(), bool(m.group(2)), (m.group(4) or "").strip()


def split_ref(rest: str) -> tuple[str, str]:
    """'#12 do this' → ('12', 'do this')."""
    m = re.match(r"^#?(\w[\w-]*)\s*([\s\S]*)$", (rest or "").strip())
    return (m.group(1), m.group(2).strip()) if m else ("", "")


def parse_hours(arg: str, default: float = 24) -> float:
    m = re.match(r"^\s*(\d{1,4})\s*([hdw]?)\s*$", (arg or "").lower())
    if not m:
        return default
    n = int(m.group(1))
    return float(max(1, min(24 * 31, n * {"": 1, "h": 1, "d": 24, "w": 168}[m.group(2)])))


def ago(iso) -> str:
    ts = parse_iso(iso)
    if not ts:
        return ""
    s = max(0, time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} d ago"


def one_line(s, n=200) -> str:
    return clamp_text(re.sub(r"\s+", " ", str(s or "")).strip(), n)


def keyboard(rows) -> dict | None:
    rows = [r for r in rows if r]
    return {"inline_keyboard": rows} if rows else None


# ============================================================================ settings
def settings(data: dict | None = None) -> dict:
    tg = dict(((data or OS.load())["integrations"]).get("telegram") or {})
    tg["mode"] = tg.get("mode") if tg.get("mode") in MODES else "polling"
    return tg


def api_base(tg: dict) -> str:
    return (tg.get("api_base") or DEFAULT_API).rstrip("/")


# ============================================================================ Bot API client
class TelegramError(Exception):
    def __init__(self, status, description, retry_after=None):
        super().__init__(f"Telegram {status}: {description}")
        self.status, self.description, self.retry_after = status, description, retry_after


def http_json(url: str, body: bytes | None, headers: dict, timeout: float) -> tuple[int | None, bytes]:
    """(status, body). Status None: no answer at all. Reads the whole body (up to 8 MB), unlike notify.http_post."""
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET",
                                 headers={"User-Agent": "Relay-Telegram/1", **headers})
    try:
        with notify._opener.open(req, timeout=timeout) as r:
            return r.status, r.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(64 * 1024)
        except Exception:
            return e.code, b""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}".encode()[:400]


class Bot:
    def __init__(self, tg: dict, allow_private: bool = False, transport=http_json):
        self.token = tg.get("bot_token") or ""
        self.base = api_base(tg)
        self.allow_private = allow_private
        self.transport = transport

    def _clean(self, s: str) -> str:
        return mask(str(s or "").replace(self.token, "<token>")) if self.token else mask(str(s or ""))

    def _check_target(self):
        ok, why = notify.target_allowed(self.base, self.allow_private or self.base == DEFAULT_API)
        if not ok:
            raise TelegramError(None, why)

    def call(self, method: str, payload: dict | None = None, timeout: float = 20):
        if not self.token:
            raise TelegramError(None, "no bot token")
        self._check_target()
        status, raw = self.transport(f"{self.base}/bot{self.token}/{method}", json.dumps(payload or {}).encode(),
                                     {"Content-Type": "application/json"}, timeout)
        if status is None:
            raise TelegramError(None, self._clean(raw.decode("utf-8", "replace")))
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            data = {"ok": False, "description": raw[:200].decode("utf-8", "replace")}
        if status >= 300 or not data.get("ok"):
            ra = ((data.get("parameters") or {}).get("retry_after"))
            raise TelegramError(status, self._clean(data.get("description") or f"HTTP {status}"), ra)
        return data.get("result")

    def download(self, file_path: str, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        self._check_target()
        status, raw = self.transport(f"{self.base}/file/bot{self.token}/{file_path}", None, {}, 60)
        if status is None or status >= 300:
            raise TelegramError(status, "could not download the file")
        if len(raw) > max_bytes:
            raise TelegramError(413, "file too large")
        return raw


# ============================================================================ state
def thread_key(chat, message_id) -> str:
    return f"{chat}:{message_id}"


def remember_thread(chat, message_id, entry: dict):
    key = thread_key(chat, message_id)

    def fn(d):
        rows = d.setdefault("messages", {})
        rows[key] = {**entry, "time": time.time()}
        if len(rows) > MAX_THREADS:
            for k in sorted(rows, key=lambda k: rows[k].get("time") or 0)[: len(rows) - MAX_THREADS]:
                rows.pop(k)
    _threads.update(fn)


def thread_for(chat, message_id) -> dict | None:
    return (_threads.read().get("messages") or {}).get(thread_key(chat, message_id))


def chat_state(chat) -> dict:
    return ((_chats.read().get("chats") or {}).get(str(chat))) or {}


def update_chat(chat, fn):
    def apply(d):
        rows = d.setdefault("chats", {})
        cur = rows.setdefault(str(chat), {})
        fn(cur)
    _chats.update(apply)


def new_confirm(payload: dict) -> str:
    key = secrets.token_urlsafe(9)

    def fn(d):
        rows = d.setdefault("items", {})
        now = time.time()
        for k in [k for k, v in rows.items() if now - (v.get("created") or 0) > CONFIRM_TTL]:
            rows.pop(k)
        rows[key] = {**payload, "created": now}
    _confirms.update(fn)
    return key


def take_confirm(key: str, consume: bool = True) -> dict | None:
    out = {}

    def fn(d):
        rows = d.setdefault("items", {})
        row = rows.get(key)
        if row and time.time() - (row.get("created") or 0) <= CONFIRM_TTL:
            out["row"] = row
            if consume:
                rows.pop(key, None)
    _confirms.update(fn)
    return out.get("row")


def cb(payload: dict) -> str:
    """callback_data for a button: Telegram allows 64 bytes, so the action itself stays on the server."""
    return "tg:" + new_confirm(payload)


# ============================================================================ single-instance lock
class PollLock:
    def __init__(self, path: Path | None = None):
        self.path = path or (ORG_DIR / "telegram_poll.lock")
        self.fh = None

    def acquire(self) -> bool:
        if self.fh:
            return True
        fh = open(self.path, "a+")
        if fcntl is None:
            self.fh = fh
            return True
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self.fh = fh
        return True

    def release(self):
        if self.fh:
            try:
                if fcntl is not None:
                    fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
            except OSError:
                pass
            self.fh = None


# ============================================================================ the assistant
class Assistant:
    def __init__(self, manager, dispatcher=None, transport=http_json, sync: bool = False, concierge_runner=None, transcriber=None):
        self.m = manager
        self.d = dispatcher
        self.transport = transport
        self.sync = sync
        self.pool = None if sync else ThreadPoolExecutor(max_workers=4, thread_name_prefix="relay-telegram")
        self.status = {"mode": None, "running": False, "last_poll_at": None, "last_update_at": None, "last_error": None,
                       "last_error_at": None, "consecutive_errors": 0, "lock": None, "webhook_cleared": False, "updates": 0}
        self.buckets: dict = {}
        self.concierge_runner = concierge_runner
        self.transcriber = transcriber
        self._stop = threading.Event()
        self._thread = None
        self._lock = PollLock()
        self._progress_seen: dict[str, dict] = {}
        self._progress_out: dict[tuple, dict] = {}
        self._progress_lock = threading.Lock()
        self._bot_key = None

    # ------------------------------------------------------------ plumbing
    def bot(self, data: dict | None = None) -> Bot:
        data = data or OS.load()
        return Bot(settings(data), bool(data["integrations"].get("allow_private_targets")), self.transport)

    def bot_username(self) -> str:
        return ((_state.read().get("bot") or {}).get("username")) or ""

    def send(self, chat, text: str, markup: dict | None = None, reply_to: int | None = None, thread: dict | None = None,
             force_reply: bool = False) -> list[int]:
        """Send HTML text, split at 4096; buttons ride on the last part. Returns the message ids."""
        bot = self.bot()
        ids = []
        parts = split_html(text or "…")
        for i, part in enumerate(parts):
            payload = {"chat_id": chat, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}
            if i == 0 and reply_to:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if i == len(parts) - 1:
                if markup:
                    payload["reply_markup"] = markup
                elif force_reply:
                    payload["reply_markup"] = {"force_reply": True, "selective": True}
            res = bot.call("sendMessage", payload)
            mid = (res or {}).get("message_id")
            if mid:
                ids.append(mid)
                if thread:
                    remember_thread(chat, mid, thread)
        return ids

    def safe_send(self, chat, text, **kw) -> list[int]:
        try:
            return self.send(chat, text, **kw)
        except TelegramError as e:
            self._error(f"sendMessage: {e.description}")
            return []

    def _error(self, text: str):
        self.status.update(last_error=clamp_text(mask(text), 300), last_error_at=now_iso())

    def submit(self, fn, *a):
        if self.sync:
            return fn(*a)
        return self.pool.submit(self._guard, fn, *a)

    def _guard(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:  # a bad update must never stop the assistant
            self._error(f"handler: {type(e).__name__}: {e}")

    # ------------------------------------------------------------ polling
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll_loop, name="relay-telegram-poll", daemon=True)
        self._thread.start()
        threading.Thread(target=self._progress_loop, name="relay-telegram-progress", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _poll_loop(self):
        while not self._stop.is_set():
            wait = self.poll_once()
            if wait:
                self._stop.wait(wait)

    def poll_once(self, poll_timeout: int = 25) -> float:
        """One getUpdates round. Returns how long to wait before the next one."""
        data = OS.load()
        tg = settings(data)
        self.status["mode"] = tg["mode"]
        if not tg.get("bot_token") or tg["mode"] != "polling":
            self.status["running"] = False
            self._lock.release()
            self._bot_key = None
            return 3
        if not self._lock.acquire():
            self.status.update(running=False, lock="Another Relay process on this data folder is polling this bot.")
            return 30
        self.status["lock"] = None
        bot = Bot(tg, bool(data["integrations"].get("allow_private_targets")), self.transport)
        key = (tg.get("bot_token"), api_base(tg))
        try:
            if self._bot_key != key:
                me = bot.call("getMe", {}, timeout=20) or {}
                _state.update(lambda d: {**d, "bot": {"id": me.get("id"), "username": me.get("username"), "name": me.get("first_name")}})
                bot.call("deleteWebhook", {"drop_pending_updates": False}, timeout=20)  # getUpdates refuses while a webhook is set
                self.status["webhook_cleared"] = True
                self._bot_key = key
            offset = int(_state.read().get("offset") or 0)
            if self.status["consecutive_errors"] == 0:
                self.status["running"] = True  # connected; the long poll below may wait up to poll_timeout
            updates = bot.call("getUpdates", {"offset": offset, "timeout": poll_timeout, "allowed_updates": ALLOWED_UPDATES},
                               timeout=poll_timeout + 15) or []
            self.status.update(running=True, last_poll_at=now_iso(), consecutive_errors=0)
            for up in updates:
                uid = int(up.get("update_id") or 0)
                if uid < offset:
                    continue
                offset = uid + 1
                # Saved before handling: after a crash an update is skipped rather than applied twice.
                _state.update(lambda d, o=offset: {**d, "offset": o})
                self.status["last_update_at"] = now_iso()
                self.status["updates"] += 1
                self.submit(self.handle_update, up)
            return 0
        except TelegramError as e:
            n = self.status["consecutive_errors"] = self.status["consecutive_errors"] + 1
            self.status["running"] = False
            if e.status == 401 or e.status == 404:
                self._error("Telegram rejected the bot token (check it in Integrations).")
                self._bot_key = None
                return 60
            if e.status == 409:  # a webhook is set (or another poller runs somewhere else)
                self._error("Telegram reports a conflict: another client is reading this bot's updates, or a webhook is set.")
                self._bot_key = None
            else:
                self._error(e.description or str(e))
            if e.retry_after:
                return float(e.retry_after)
            return notify.backoff(n, base=2, cap=60)

    # ------------------------------------------------------------ entry point for both transports
    def handle_update(self, up: dict):
        if up.get("callback_query"):
            return self.on_callback(up["callback_query"])
        msg = up.get("message")
        if msg:
            return self.on_message(msg)

    # ------------------------------------------------------------ people
    def user_for(self, from_id) -> dict | None:
        u = identity.by_telegram(from_id)
        if u and rbac.level(u.get("role")) >= rbac.level("viewer") and not u.get("disabled"):
            return u
        return None

    def allowed(self, user: dict, method: str, path: str) -> tuple[bool, str]:
        role, label = rbac.rule_for(method, path)
        ok, why = rbac.allowed(user, role, method, path, task_lookup=self.m.store.get)
        return ok, (why if ok else f"{why} ({label})")

    def rate_ok(self, key: str, per_minute: float) -> bool:
        b = self.buckets.get(key)
        if not b:
            b = self.buckets[key] = notify.TokenBucket(per_minute)
        return b.take() == 0

    def audit(self, user, action, obj, detail="", outcome="ok", chat=None):
        audit.record(user, action, obj, via="telegram", outcome=outcome, detail=detail,
                     request={"method": "TELEGRAM", "path": action, "chat": str(chat or "")})

    # ------------------------------------------------------------ messages
    def on_message(self, msg: dict):
        chat_obj = msg.get("chat") or {}
        chat, ctype = chat_obj.get("id"), chat_obj.get("type") or "private"
        sender = msg.get("from") or {}
        from_id = sender.get("id")
        if sender.get("is_bot") or chat is None:
            return
        tg = settings()
        text = (msg.get("text") or msg.get("caption") or "").strip()
        cmd = parse_command(text, self.bot_username())
        if ctype != "private":
            if not tg.get("groups_enabled"):
                if cmd and cmd[0] in ("start", "link", "help"):
                    self.safe_send(chat, "Relay only talks in private chats with its bot. An admin can allow group chats under Integrations → Telegram.")
                return
            me = self.bot_username()
            to_bot = ((msg.get("reply_to_message") or {}).get("from") or {}).get("username", "").lower() == me.lower() if me else False
            mentioned = bool(me) and f"@{me.lower()}" in text.lower()
            if not cmd and not to_bot and not mentioned:
                return  # in a group, only commands, replies to the bot and mentions are for Relay
            if mentioned and me:
                text = re.sub(rf"@{re.escape(me)}\b", "", text, flags=re.I).strip()
        if cmd and cmd[0] == "link":
            return self.cmd_link(chat, ctype, sender, cmd[2])
        user = self.user_for(from_id)
        if cmd and cmd[0] == "start" and not cmd[2]:
            return self.cmd_start(chat, user)
        if cmd and cmd[0] == "start":
            cmd = ("start_task", cmd[1], cmd[2])
        if not user:
            return self.greet_unknown(chat, cmd) if ctype == "private" else None
        if not self.rate_ok(f"u:{from_id}", float(tg.get("rate_limit_per_minute") or 30)):
            if self.rate_ok(f"warn:{from_id}", 1):
                self.safe_send(chat, "Slow down a little: too many messages in a minute. Try again shortly.")
            return
        update_chat(chat, lambda c: c.update(username=user["username"], last_seen=now_iso(), type=ctype))
        if not text and (msg.get("voice") or msg.get("audio")):
            text = self.voice_text(chat, msg, user)
            if not text:
                return
            cmd = parse_command(text, self.bot_username())
        if not text:
            self.safe_send(chat, "I can read text and voice notes. Send /help to see what I can do.")
            return
        if cmd:
            return self.run_command(chat, user, cmd, msg)
        reply = msg.get("reply_to_message") or {}
        if reply.get("message_id"):
            entry = thread_for(chat, reply["message_id"])
            if entry and entry.get("kind") != "concierge":
                return self.on_reply(chat, user, entry, text, msg)
        return self.concierge(chat, user, text, msg)

    def greet_unknown(self, chat, cmd):
        st = _state.read()
        if str(chat) in (st.get("greeted") or {}) and not (cmd and cmd[0] == "help"):
            return
        _state.update(lambda d: {**d, "greeted": {**(d.get("greeted") or {}), str(chat): now_iso()}})
        self.safe_send(chat, LINK_HELP)

    def cmd_start(self, chat, user):
        if user:
            self.safe_send(chat, f"Hi {h(user.get('name') or user['username'])}. This chat is linked to Relay as <b>{h(user['username'])}</b> "
                                 f"({h(user.get('role'))}).\n\n" + HELP_SHORT)
        else:
            self.safe_send(chat, LINK_HELP)

    def cmd_link(self, chat, ctype, sender, code):
        from_id = sender.get("id")
        if ctype != "private":
            self.safe_send(chat, "Link in a private chat with the bot, so your code stays private.")
            return
        if not self.rate_ok(f"link:{from_id}", 5):
            self.safe_send(chat, "Too many link attempts. Wait a minute and try again.")
            return
        u = notify.user_for_link_code(code) if code else None
        if not u:
            self.safe_send(chat, "That code is not valid. In Relay open your avatar → <b>Profile</b> → <b>Notifications</b>, copy the "
                                 "<code>/link …</code> line from the Telegram card and send it here.")
            audit.record({"username": "anonymous"}, "profile.telegram_link", {"type": "profile"}, via="telegram", outcome="denied",
                         detail=f"invalid link code from telegram user {from_id}")
            return
        if u.get("disabled"):
            self.safe_send(chat, "That Relay account is disabled.")
            return
        identity.link_telegram(u["username"], from_id)
        turned_on = enable_telegram_notifications(u["username"], chat)
        audit.record(u, "profile.telegram_link", {"type": "profile", "id": u["username"]}, via="telegram", detail=f"telegram user {from_id}, chat {chat}")
        extra = ("\n\nTelegram is now on for: " + ", ".join(notify.EVENT_LABEL[e].lower() for e in turned_on) + " (change it under Profile → Notifications).") if turned_on else ""
        self.safe_send(chat, f"Linked to Relay as <b>{h(u.get('name') or u['username'])}</b> ({h(u.get('role'))}). "
                             f"Buttons and replies here now act as you and are recorded in the audit log.{extra}\n\n" + HELP_SHORT)

    # ------------------------------------------------------------ voice
    def voice_text(self, chat, msg, user) -> str:
        tg = settings()
        from . import voice
        if not (tg.get("voice") or {}).get("enabled", True):
            self.safe_send(chat, "Voice notes are switched off. Please type your message.")
            return ""
        provider = voice.provider(self.m.cfg())
        if not provider:
            self.safe_send(chat, "I can't transcribe voice notes yet: no OpenAI or Gemini API key is configured in Relay "
                                 "(Anthropic's API has no speech-to-text). Please type your message.")
            return ""
        media = msg.get("voice") or msg.get("audio") or {}
        if int(media.get("duration") or 0) > 300:
            self.safe_send(chat, "That voice note is longer than 5 minutes. Please keep it shorter, or type it.")
            return ""
        try:
            self.typing(chat)
            bot = self.bot()
            f = bot.call("getFile", {"file_id": media.get("file_id")})
            if not isinstance(f, dict) or not f.get("file_path"):
                raise TelegramError(None, "Telegram did not return the file")
            audio = bot.download(f["file_path"])
            text = (self.transcriber or voice.transcribe)(provider, audio, media.get("mime_type") or "audio/ogg")
        except Exception as e:
            self.safe_send(chat, f"I couldn't transcribe that voice note ({h(clamp_text(mask(str(e)), 160))}). Please type it.")
            return ""
        text = (text or "").strip()
        if not text:
            self.safe_send(chat, "I couldn't hear any words in that voice note.")
            return ""
        self.safe_send(chat, f"🎙 <i>{h(clamp_text(text, 600))}</i>", reply_to=msg.get("message_id"))
        return text

    def typing(self, chat):
        try:
            self.bot().call("sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=10)
        except TelegramError:
            pass

    # ------------------------------------------------------------ tasks
    def find_task(self, ref) -> dict | None:
        ref = str(ref or "").strip().lstrip("#")
        if not ref:
            return None
        for t in self.m.store.list():
            if (ref.isdigit() and str(t.get("number")) == ref) or t["id"] == ref:
                return t
        return None

    def task_link(self, t) -> str | None:
        base = notify.base_url()
        return f"{base}/#/task/{t['id']}" if base.startswith("https://") and t else None

    # ------------------------------------------------------------ replies to Relay's messages
    def on_reply(self, chat, user, entry, text, msg):
        tid = entry.get("tid")
        t = self.m.store.get(tid) if tid else None
        mid = msg.get("message_id")
        if entry.get("kind") == "reject_reason":
            return self.reply_result(chat, mid, self.execute(user, {"op": "reject", "tid": tid, "qid": entry.get("qid"), "text": text}, chat))
        if entry.get("kind") == "tool_request":
            self.safe_send(chat, "Use the Approve / Deny buttons on the tool request (admins only).", reply_to=mid)
            return
        if not t:
            self.safe_send(chat, "That task no longer exists.", reply_to=mid)
            return
        pend = t.get("pending") or {}
        if entry.get("qid") and pend.get("id") == entry["qid"]:
            if pend.get("kind") in ("approval", "design_approval"):
                what = "the design" if pend.get("kind") == "design_approval" else "delivery"
                rows = [[{"text": "✅ Approve with this note", "callback_data": cb({"op": "approve", "tid": tid, "qid": pend["id"], "text": text})}],
                        [{"text": "✏️ Request changes", "callback_data": cb({"op": "reject", "tid": tid, "qid": pend["id"], "text": text})}],
                        [{"text": "Cancel", "callback_data": cb({"op": "cancel"})}]]
                self.safe_send(chat, f"Send this to <b>#{t.get('number')}</b> as approval of {what}, or as a change request?\n\n<blockquote>{h(clamp_text(text, 800))}</blockquote>",
                               markup=keyboard(rows), reply_to=mid)
                return
            return self.reply_result(chat, mid, self.execute(user, {"op": "answer", "tid": tid, "qid": pend["id"], "text": text}, chat))
        return self.reply_result(chat, mid, self.execute(user, {"op": "guidance", "tid": tid, "text": text, "mode": "queue"}, chat))

    def reply_result(self, chat, reply_to, result: dict):
        self.safe_send(chat, ("✅ " if result.get("ok") else "⚠️ ") + h(result.get("text") or ""), reply_to=reply_to)

    # ------------------------------------------------------------ the one place actions happen
    def execute(self, user: dict, a: dict, chat=None) -> dict:
        """Run an action as `user`: role check on the equivalent HTTP route, the action, an audit entry.
        Returns {"ok": bool, "text": str}."""
        from . import web as org_web
        op = a.get("op")
        m = self.m
        tid = a.get("tid")
        t = m.store.get(tid) if tid else None
        routes = {"answer": "answer", "approve": "approve", "reject": "reject", "guidance": "guidance", "stop": "stop",
                  "retry": "retry", "start": "start", "resume": "resume"}
        if op in routes:
            if not t:
                return {"ok": False, "text": "That task no longer exists."}
            method, path = "POST", f"/api/tasks/{tid}/{routes[op]}"
        elif op == "create":
            method, path = "POST", "/api/tasks"
        elif op in ("autopilot_pause", "autopilot_resume"):
            method, path = "POST", f"/api/autopilot/{op.split('_')[1]}"
        elif op == "queue_start":
            method, path = "POST", "/api/queue/start"
        elif op == "merge":
            if not t:
                return {"ok": False, "text": "That task no longer exists."}
            method, path = "POST", f"/api/tasks/{tid}/merge"
        elif op in ("tool_approve", "tool_deny"):
            method, path = "POST", f"/api/tools/requests/{a.get('rid')}/{op.split('_')[1]}"
        elif op in ("follow", "unfollow"):
            method, path = "GET", f"/api/tasks/{tid}"
        else:
            return {"ok": False, "text": f"Unknown action {op}."}
        action = {"guidance": "task.guidance", "create": "task.create", "autopilot_pause": "autopilot.pause", "autopilot_resume": "autopilot.resume",
                  "queue_start": "queue.start", "tool_approve": "tool_request.approve", "tool_deny": "tool_request.deny"}.get(op, f"task.{op}")
        obj = {"type": "task", "id": tid, "name": (t or {}).get("name"), "project": projects.project_of_task(t) if t else None} if t else {"type": action.split(".")[0]}
        fresh = identity.get(user["username"]) or user  # the role as it is now, not when the button was sent
        if fresh.get("disabled"):
            return {"ok": False, "text": "Your Relay account is disabled."}
        ok, why = self.allowed(fresh, method, path)
        if not ok:
            if method != "GET":
                self.audit(fresh, action, obj, detail=why, outcome="denied", chat=chat)
            return {"ok": False, "text": why}
        try:
            with org_web.acting_as(fresh, "telegram"):
                text = self._do(fresh, op, a, t, chat)
        except (ValueError, KeyError, RuntimeError) as e:
            msg = str(e).strip("'") or type(e).__name__
            if method != "GET":
                self.audit(fresh, action, obj, detail=msg, outcome="error", chat=chat)
            return {"ok": False, "text": msg}
        if method != "GET":
            if op == "create" and isinstance(text, dict):
                obj = {"type": "task", "id": text["task"]["id"], "name": text["task"].get("name"), "project": text["task"].get("project_id")}
            self.audit(fresh, action, obj, detail=clamp_text(a.get("text") or "", 200), chat=chat)
        return text if isinstance(text, dict) else {"ok": True, "text": text}

    def _do(self, user, op, a, t, chat):
        m = self.m
        tid = t["id"] if t else None
        label = f"#{t.get('number')}" if t else ""
        if op in ("answer", "approve", "reject"):
            pend = t.get("pending") or {}
            if a.get("qid") and pend.get("id") != a["qid"]:
                raise ValueError(f"{label} has moved on: that question was already answered.")
            if not pend:
                raise ValueError(f"{label} is not waiting for anyone.")
            if op == "answer":
                if pend.get("kind") in ("approval", "design_approval"):
                    raise ValueError(f"{label} is waiting for an approval: use /approve {t.get('number')} or /reject {t.get('number')} <why>.")
                if not (a.get("text") or "").strip():
                    raise ValueError("The answer is empty.")
                m.answer(tid, pend["id"], a["text"].strip(), {})
                return f"Answered {label}: {clamp_text(a['text'].strip(), 120)}"
            if pend.get("kind") not in ("approval", "design_approval"):
                raise ValueError(f"{label} is asking a question, not waiting for approval: reply to the question or use /answer {t.get('number')} <text>.")
            if op == "reject" and not (a.get("text") or "").strip():
                raise ValueError("Say what should change: /reject N <why>.")
            what = "design" if pend.get("kind") == "design_approval" else "delivery"
            m.approve(tid, op == "approve", (a.get("text") or "").strip() or ("Approved from Telegram" if op == "approve" else ""))
            return f"{'Approved' if op == 'approve' else 'Changes requested for'} {what} of {label}."
        if op == "guidance":
            res = m.guidance(tid, a.get("text") or "", "next", a.get("mode") or "queue")
            applied = (res or {}).get("applied")
            return {"ok": applied != "finished", "text": {
                "answer": f"Sent to {label} as the answer to its question.",
                "interrupt": f"Interrupted {label}'s current agent turn with your message.",
                "resumed": f"{label} had ended: resumed it with your message.",
                "finished": f"{label} has finished, so no agent will read that. Start a follow-up with /new.",
            }.get(applied, f"Queued for {label}'s next agent turn." + (f" ({res.get('note')})" if (res or {}).get("note") else ""))}
        if op == "stop":
            m.stop(tid)
            return f"Stopping {label}."
        if op == "retry":
            m.retry(tid, fresh=bool(a.get("fresh")))
            return f"{label} queued for retry."
        if op == "start":
            m.start_task(tid)
            return f"{label} started."
        if op == "resume":
            m.resume(tid)
            return f"{label} resumed."
        if op == "create":
            created = m.create_task(a["payload"])
            follow(chat, created["id"], user["username"])
            note = "" if m.scheduler else " The queue is halted: /resume starts it."
            return {"ok": True, "text": f"Queued as #{created.get('number')} {created.get('name')}.{note} You'll get progress updates here (/unfollow {created.get('number')} to stop).",
                    "task": created}
        if op == "autopilot_pause":
            st = m.autopilot.set_paused(True)
            return f"Autopilot paused. {st.get('running', 0)} running task(s) stop after their current agent turn."
        if op == "autopilot_resume":
            st = m.autopilot.set_paused(False)
            return f"Autopilot resumed: {st.get('label') or st.get('state')}."
        if op == "queue_start":
            m.start()
            return "Queue started."
        if op == "merge":
            from .. import mission
            out = mission.merge_change_set(t, method=a.get("method") or "squash")
            merged = [r["name"] for r in out["results"] if r.get("ok")]
            if out["ok"]:
                m.notify("success", f"Merged: {t['name']}", f"{len(merged)} pull request{'s' if len(merged) != 1 else ''} merged in order.", tid, kind="info")
                m.emit_task(tid)
                return f"Merged {label}: {len(merged)} pull request(s) in order."
            failed = next((r for r in out["results"] if not r.get("ok")), {})
            m.notify("error", f"Merge stopped: {t['name']}", f"{failed.get('name', '')}: {failed.get('error', '')}", tid, kind="info")
            m.emit_task(tid)
            return {"ok": False, "text": f"Merge stopped at {failed.get('name', '?')}: {failed.get('error', '')}"}
        if op in ("tool_approve", "tool_deny"):
            from .. import toolbox
            row = toolbox.approve(a["rid"], by=user["username"]) if op == "tool_approve" else toolbox.deny(a["rid"], "Denied from Telegram", by=user["username"])
            if row.get("task"):
                m.timeline(row["task"], "user", f"Tool request {row['status']}", row.get("name") or "")
            return f"Tool request {row.get('name')}: {row.get('status')}."
        if op == "follow":
            follow(chat, tid, user["username"])
            return f"Following {label}: phase changes, finished work packages, verification and delivery arrive here."
        if op == "unfollow":
            unfollow(chat, tid)
            return f"Stopped following {label}."
        raise ValueError(f"Unknown action {op}")

    # ------------------------------------------------------------ buttons
    def on_callback(self, cq: dict):
        data = str(cq.get("data") or "")
        from_id = (cq.get("from") or {}).get("id")
        msgobj = cq.get("message") or {}
        chat = (msgobj.get("chat") or {}).get("id")
        user = self.user_for(from_id)
        answer_text, result = "", None
        if not user:
            answer_text = "Link Telegram to your Relay account first: send /link CODE (Relay → Profile → Notifications)."
        elif not self.rate_ok(f"u:{from_id}", float(settings().get("rate_limit_per_minute") or 30)):
            answer_text = "Too many actions in a minute."
        elif data.startswith("rl:"):  # buttons sent by older builds
            act = notify.take_action(data[3:])
            if not act:
                answer_text = "This button has expired."
            else:
                op = {"approve": "approve", "reject": "reject", "answer": "answer"}.get(act["action"])
                result = self.execute(user, {"op": op, "tid": act["tid"], "qid": act["qid"], "text": act.get("value") or
                                             ("Changes requested from Telegram" if op == "reject" else "")}, chat)
        elif data.startswith("tg:"):
            row = take_confirm(data[3:], consume=False)
            if not row:
                answer_text = "This button has expired."
            elif row.get("user") and row["user"] != user["username"]:
                answer_text = "Only the person who asked can confirm this."
            else:
                result, keep = self.press(user, row, chat, msgobj)
                if not keep:
                    take_confirm(data[3:], consume=True)
        else:
            answer_text = "This button has expired."
        if result is not None:
            answer_text = result.get("text") or ""
        try:
            self.bot().call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": clamp_text(answer_text, 190)}, timeout=10)
        except TelegramError:
            pass
        if result is not None and result.get("done_note") is not False and msgobj.get("message_id") and chat is not None:
            self.mark_done(chat, msgobj, ("✅ " if result.get("ok") else "⚠️ ") + (result.get("text") or ""), keep_buttons=not result.get("ok"))

    def press(self, user, row: dict, chat, msgobj) -> tuple[dict, bool]:
        """A button press. Returns (result, keep_the_button_usable)."""
        op = row.get("op")
        if op == "cancel":
            return {"ok": True, "text": "Cancelled."}, False
        if op == "reject_reason":
            t = self.m.store.get(row.get("tid")) or {}
            if (t.get("pending") or {}).get("id") != row.get("qid"):
                return {"ok": False, "text": "That approval is no longer pending."}, False
            self.safe_send(chat, f"What should change in <b>#{t.get('number')}</b>? Reply to this message.", force_reply=True,
                           thread={"tid": t["id"], "qid": row.get("qid"), "kind": "reject_reason"})
            return {"ok": True, "text": "Reply with what should change.", "done_note": False}, True
        if op == "new_task":
            return self.new_task_preview(chat, user, row.get("repo") or "", row.get("request") or "", reply_to=msgobj.get("message_id")), False
        res = self.execute(user, row, chat)
        return res, not res.get("ok") and op in ("answer", "approve", "reject")

    def mark_done(self, chat, msgobj, note: str, keep_buttons: bool = False):
        """Append the outcome to the message the button was on, keeping its formatting (entities offsets stay valid)."""
        text = msgobj.get("text") or msgobj.get("caption") or ""
        payload = {"chat_id": chat, "message_id": msgobj["message_id"], "text": clamp_text(text + "\n\n" + note, LIMIT),
                   "disable_web_page_preview": True}
        if msgobj.get("entities"):
            payload["entities"] = [e for e in msgobj["entities"] if int(e.get("offset", 0)) + int(e.get("length", 0)) <= utf16len(text)]
        if keep_buttons and msgobj.get("reply_markup"):
            payload["reply_markup"] = msgobj["reply_markup"]
        try:
            self.bot().call("editMessageText", payload, timeout=15)
        except TelegramError:
            pass

    # ------------------------------------------------------------ commands
    def run_command(self, chat, user, cmd, msg):
        name, bang, rest = cmd
        fn = COMMANDS.get(name)
        if not fn:
            self.safe_send(chat, f"I don't know /{h(name)}. Send /help for the list, or just ask in plain words.")
            return
        return fn(self, chat, user, rest, bang, msg)

    def c_help(self, chat, user, rest, bang, msg):
        self.safe_send(chat, HELP_FULL)

    def c_whoami(self, chat, user, rest, bang, msg):
        self.safe_send(chat, f"Relay user <b>{h(user['username'])}</b> ({h(user.get('role'))}). Chat {h(chat)}.")

    def c_unlink(self, chat, user, rest, bang, msg):
        identity.unlink_telegram(user["username"])
        audit.record(user, "profile.telegram_unlink", {"type": "profile", "id": user["username"]}, via="telegram", detail=f"chat {chat}")
        self.safe_send(chat, "Unlinked. This chat no longer acts as you. Send /link CODE to link again.")

    def c_status(self, chat, user, rest, bang, msg):
        from .. import mission
        m = self.m
        st = m.autopilot.status()
        lines = [f"<b>Relay</b> · {h(st.get('label') or st.get('state'))}" + (" · <b>paused</b>" if st.get("paused") else "")]
        spend = f"${float(st.get('today_cost_usd') or 0):.2f} today" + (f" of ${float(st['daily_cap_usd']):.2f} cap" if st.get("daily_cap_usd") else "")
        lines.append(f"Running {st.get('running', 0)} · queued {st.get('queued', 0)} · needs you <b>{st.get('needs_you', 0)}</b> · {spend}")
        running = [t for t in m.store.list() if t["id"] in m.runners or t.get("status") in ("needs_input", "paused")]
        running = [t for t in running if not t.get("archived")]
        if running:
            lines.append("")
        for t in sorted(running, key=lambda t: t.get("number") or 0)[:12]:
            act = mission.last_activity(m.store, t["id"], window=40)
            tool = act.get("activity") or {}
            step = (act.get("said") or {}).get("text") or ""
            if tool.get("summary") and (not step or (tool.get("ts") or "") > ((act.get("said") or {}).get("ts") or "")):
                step = f"{tool.get('tool') or tool.get('category') or tool.get('kind')}: {tool['summary']}"
            lines.append(f"<b>#{t.get('number')}</b> {h(clamp_text(t.get('name'), 60))}\n   {h(phase_of(t))} · {h(one_line(t.get('detail'), 90))}"
                         + (f"\n   <i>{h(one_line(step, 110))}</i>" if step else ""))
        if not running:
            lines.append("Nothing is running.")
        if st.get("needs_you"):
            lines.append("\n/needs shows what waits for you.")
        self.safe_send(chat, "\n".join(lines))

    def c_needs(self, chat, user, rest, bang, msg):
        from .. import toolbox
        items = self.m.autopilot.inbox()
        try:
            items = sorted(items + toolbox.inbox_items(), key=lambda x: x.get("time") or "")
        except Exception:
            pass
        if not items:
            self.safe_send(chat, "Nothing waits for you. 🎉")
            return
        self.safe_send(chat, f"<b>{len(items)} item(s) need you</b>" + (" (showing the oldest 8)" if len(items) > 8 else "") +
                       ". Reply to a question to answer it.")
        for it in items[:8]:
            t = self.m.store.get(it.get("task_id")) if it.get("task_id") else None
            text, markup, thread = render_item(it, t, self.task_link(t) if t else None)
            self.safe_send(chat, text, markup=markup, thread=thread)

    def c_tasks(self, chat, user, rest, bang, msg):
        rows = [t for t in self.m.store.list() if not t.get("archived")]
        f = (rest or "").strip().lower()
        groups = {"running": lambda t: t["id"] in self.m.runners or t.get("status") in ("running", "preparing", "planning", "implementing", "verifying", "reviewing", "delivering"),
                  "active": lambda t: t.get("status") not in ("done", "failed", "stopped", "draft"),
                  "queued": lambda t: t.get("status") == "queued", "done": lambda t: t.get("status") == "done",
                  "failed": lambda t: t.get("status") in ("failed", "stopped", "interrupted"),
                  "needs": lambda t: bool(t.get("pending")), "all": lambda t: True, "draft": lambda t: t.get("status") == "draft"}
        if f in groups:
            rows = [t for t in rows if groups[f](t)]
        elif f:
            rows = [t for t in rows if f in (t.get("name") or "").lower() or f in (t.get("github_repo") or t.get("repo") or "").lower()]
        rows = sorted(rows, key=lambda t: t.get("updated_at") or "", reverse=True)
        if not rows:
            self.safe_send(chat, "No tasks match." + ("" if f else " Create one with /new repo: what to do"))
            return
        lines = [f"<b>Tasks</b>{' · ' + h(f) if f else ''} ({len(rows)})"]
        for t in rows[:20]:
            lines.append(f"{STATUS_ICON.get(t.get('status'), '•')} <b>#{t.get('number')}</b> {h(clamp_text(t.get('name'), 60))} · {h(t.get('status'))}"
                         + (f" · {h(ago(t.get('updated_at')))}" if t.get("updated_at") else ""))
        if len(rows) > 20:
            lines.append(f"…and {len(rows) - 20} more. Filter: /tasks running|queued|needs|done|failed|&lt;text&gt;")
        lines.append("\n/task N for details.")
        self.safe_send(chat, "\n".join(lines))

    def _task_arg(self, chat, rest, usage) -> tuple[dict | None, str]:
        ref, tail = split_ref(rest)
        t = self.find_task(ref) if ref else None
        if not t:
            self.safe_send(chat, (f"No task #{h(ref)}. " if ref else "") + f"Usage: {h(usage)}")
            return None, ""
        return t, tail

    def c_task(self, chat, user, rest, bang, msg):
        t, _ = self._task_arg(chat, rest, "/task N")
        if not t:
            return
        text = render_task(self.m, t)
        rows = task_buttons(t, self.task_link(t), user["username"], chat)
        self.safe_send(chat, text, markup=keyboard(rows), thread={"tid": t["id"], "qid": (t.get("pending") or {}).get("id"), "kind": "task"})

    def c_log(self, chat, user, rest, bang, msg):
        t, _ = self._task_arg(chat, rest, "/log N")
        if not t:
            return
        rows = condensed_log(self.m, t, 12)
        text = f"<b>#{t.get('number')}</b> {h(t.get('name'))} · last messages\n\n" + ("\n\n".join(rows) if rows else "No agent messages yet.")
        self.safe_send(chat, text, thread={"tid": t["id"], "kind": "task"})

    def c_say(self, chat, user, rest, bang, msg):
        t, text = self._task_arg(chat, rest, "/say N your message   (or /say! N … to interrupt the current turn)")
        if not t:
            return
        if not text:
            self.safe_send(chat, "Add the message: /say N your message")
            return
        self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "guidance", "tid": t["id"], "text": text, "mode": "interrupt" if bang else "queue"}, chat))

    def c_answer(self, chat, user, rest, bang, msg):
        t, text = self._task_arg(chat, rest, "/answer N your answer")
        if not t:
            return
        pend = t.get("pending") or {}
        self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "answer", "tid": t["id"], "qid": pend.get("id"), "text": text}, chat))

    def c_approve(self, chat, user, rest, bang, msg):
        t, text = self._task_arg(chat, rest, "/approve N [note]")
        if t:
            self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "approve", "tid": t["id"], "qid": (t.get("pending") or {}).get("id"), "text": text}, chat))

    def c_reject(self, chat, user, rest, bang, msg):
        t, text = self._task_arg(chat, rest, "/reject N what should change")
        if t:
            self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "reject", "tid": t["id"], "qid": (t.get("pending") or {}).get("id"), "text": text}, chat))

    def c_new(self, chat, user, rest, bang, msg):
        m = re.match(r"^([^:\n]{1,200}):\s*([\s\S]+)$", rest or "")
        if not m:
            self.safe_send(chat, "Usage: <code>/new repo: what to do</code>\nFor example: <code>/new relay: add a dark mode toggle to settings</code>")
            return
        res = self.new_task_preview(chat, user, m.group(1).strip(), m.group(2).strip(), reply_to=msg.get("message_id"))
        if not res.get("ok"):
            self.reply_result(chat, msg.get("message_id"), res)

    def new_task_preview(self, chat, user, repo_name: str, request: str, reply_to=None) -> dict:
        ok, why = self.allowed(identity.get(user["username"]) or user, "POST", "/api/tasks")
        if not ok:
            return {"ok": False, "text": why}
        found, candidates, ambiguous = resolve_repo(self.m, repo_name)
        if not found:
            names = ", ".join(sorted({Path(c).name for c in candidates})[:12])
            if ambiguous:
                return {"ok": False, "text": f"“{repo_name}” matches several repositories: {names}. Be more specific."}
            return {"ok": False, "text": f"No repository matches “{repo_name}”." + (f" Known: {names}." if names else "")}
        payload = {"repo": found, "requirements": request, "queue": True, "workflow": default_workflow(self.m.cfg())}
        lines = [f"<b>New task</b> in <b>{h(Path(found).name)}</b>", f"<blockquote>{h(clamp_text(request, 900))}</blockquote>"]
        try:
            wf = self.m.build_workflow(payload)
            payload["workflow"] = {**payload["workflow"], "roles": {r: {k: (wf["roles"].get(r) or {}).get(k, "") for k in ("agent", "model", "effort")}
                                                                   for r in ("supervisor", "worker", "reviewer")}}
            roles = wf["roles"]
            from .. import config as C
            team = " · ".join(f"{r}: {C.AGENTS.get(roles[r]['agent'], {}).get('label', roles[r]['agent'])}{(' ' + roles[r]['model']) if roles[r].get('model') else ''}"
                              for r in ("supervisor", "worker", "reviewer") if (roles.get(r) or {}).get("agent"))
            lines.append(f"Team ({h(wf.get('preset'))}): {h(team)}")
            try:
                pf = self.m.learning.engine.preflight({**payload, "workflow": {"roles": roles}})
                r = pf.get("risk") or {}
                if r:
                    factors = " ".join(one_line(f.get("text") or f.get("evidence") or f.get("id"), 90).rstrip(". ") + "." for f in (r.get("factors") or [])[:2])
                    lines.append(f"Risk: <b>{h(r.get('level'))}</b>" + (f" ({round(float(r.get('p_fail') or 0) * 100)}% chance of trouble)" if r.get("p_fail") is not None else "")
                                 + (f"\n<i>{h(factors)}</i>" if factors else ""))
            except Exception:
                pass
        except ValueError as e:
            return {"ok": False, "text": str(e)}
        if not self.m.scheduler:
            lines.append("The queue is halted, so it will wait until the queue runs.")
        rows = [[{"text": "✅ Queue it", "callback_data": cb({"op": "create", "payload": payload, "user": user["username"]})},
                 {"text": "Cancel", "callback_data": cb({"op": "cancel", "user": user["username"]})}]]
        self.safe_send(chat, "\n".join(lines), markup=keyboard(rows), reply_to=reply_to)
        return {"ok": True, "text": "Confirm to queue it."}

    def c_pause(self, chat, user, rest, bang, msg):
        self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "autopilot_pause"}, chat))

    def c_resume(self, chat, user, rest, bang, msg):
        self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "autopilot_resume"}, chat))

    def c_digest(self, chat, user, rest, bang, msg):
        d = self.m.autopilot.digest(hours=parse_hours(rest))
        self.safe_send(chat, render_digest(d))

    def c_merge(self, chat, user, rest, bang, msg):
        t, _ = self._task_arg(chat, rest, "/merge N")
        if not t:
            return
        fresh = identity.get(user["username"]) or user
        ok, why = self.allowed(fresh, "POST", f"/api/tasks/{t['id']}/merge")
        if not ok:
            self.reply_result(chat, msg.get("message_id"), {"ok": False, "text": why})
            return
        from .. import mission
        try:
            plan = mission.merge_change_set(t, dry_run=True)["plan"]
        except ValueError as e:
            self.reply_result(chat, msg.get("message_id"), {"ok": False, "text": str(e)})
            return
        lines = [f"<b>Merge #{t.get('number')}</b> {h(t.get('name'))}? Pull requests merge in this order (squash), stopping at the first failure:"]
        for i, p in enumerate(plan, 1):
            lines.append(f"{i}. {h(p['name'])} · <a href=\"{h(p.get('url') or '')}\">{h(p['repo'])}#{h(p['number'])}</a>")
        rows = [[{"text": "🔀 Merge now", "callback_data": cb({"op": "merge", "tid": t["id"], "user": user["username"]})},
                 {"text": "Cancel", "callback_data": cb({"op": "cancel", "user": user["username"]})}]]
        self.safe_send(chat, "\n".join(lines), markup=keyboard(rows), reply_to=msg.get("message_id"))

    def c_follow(self, chat, user, rest, bang, msg):
        if not rest.strip():
            rows = [self.m.store.get(tid) for tid in chat_state(chat).get("follows") or []]
            rows = [t for t in rows if t]
            self.safe_send(chat, ("Following: " + ", ".join(f"#{t.get('number')} {h(clamp_text(t.get('name'), 40))}" for t in rows)) if rows
                           else "You follow no tasks. /follow N sends phase changes, work packages, verification and delivery of task N here.")
            return
        t, _ = self._task_arg(chat, rest, "/follow N")
        if t:
            self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "follow", "tid": t["id"]}, chat))

    def c_unfollow(self, chat, user, rest, bang, msg):
        t, _ = self._task_arg(chat, rest, "/unfollow N")
        if t:
            self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": "unfollow", "tid": t["id"]}, chat))

    # ------------------------------------------------------------ concierge
    def concierge(self, chat, user, text, msg):
        from . import concierge as CG
        tg = settings()
        cc = tg.get("concierge") or {}
        if cc.get("enabled", True) is False:
            self.safe_send(chat, "The assistant is switched off. Commands still work: /help")
            return
        if not self.rate_ok(f"ai:{user['username']}", float(cc.get("per_minute") or 6)):
            self.safe_send(chat, "One moment: that's a lot of questions in a minute.")
            return
        if not CG.within_daily_limit(user["username"], int(cc.get("daily_turn_limit") or 200)):
            self.safe_send(chat, "You've reached today's assistant limit. Commands still work: /help")
            return
        stop = threading.Event()

        def keep_typing():
            while not stop.is_set():
                self.typing(chat)
                stop.wait(4.5)
        threading.Thread(target=keep_typing, daemon=True).start()
        try:
            reply = msg.get("reply_to_message") or {}
            entry = thread_for(chat, reply["message_id"]) if reply.get("message_id") else None
            out = CG.answer(self.m, user, chat, text, entry=entry, runner=self.concierge_runner)
        finally:
            stop.set()
        rows = []
        for act in out.get("actions") or []:
            if act.get("op") == "new_task":
                payload = {"op": "new_task", "repo": act.get("repo"), "request": act.get("request"), "user": user["username"]}
            else:
                payload = {**{k: v for k, v in act.items() if k != "label"}, "user": user["username"]}
            rows.append([{"text": clamp_text(act["label"], 60), "callback_data": cb(payload)}])
        text_out = md_lite(out.get("text") or "…")
        if out.get("error"):
            text_out = f"⚠️ {h(out['error'])}"
        if rows:
            text_out += "\n\n<i>Suggested — nothing happens until you press a button:</i>"
        self.safe_send(chat, text_out, markup=keyboard(rows), reply_to=msg.get("message_id"), thread={"kind": "concierge"})

    # ------------------------------------------------------------ notifications (called by notify.Dispatcher)
    def deliver_notification(self, chat, msg: dict) -> tuple[int | None, str, float | None]:
        t = self.m.store.get((msg.get("task") or {}).get("id")) if (msg.get("task") or {}).get("id") else None
        text, markup, thread = render_notification(self.m, msg, t)
        try:
            ids = self.send(chat, text, markup=markup, thread=thread)
            return 200, f"message {ids[-1] if ids else '?'}", None
        except TelegramError as e:
            return e.status, e.description, e.retry_after

    # ------------------------------------------------------------ live progress for followed tasks
    def on_task_event(self, t: dict):
        """Called with every task update (manager emit). Cheap: compares a few fields, queues pings for followers."""
        if not t or not t.get("id"):
            return
        tid = t["id"]
        snap = {"phase": (t.get("checkpoint") or {}).get("phase"), "status": t.get("status"),
                "packages": len(t.get("packages_done") or []) if isinstance(t.get("packages_done"), list) else int(t.get("packages_done") or 0),
                "verification": (t.get("verification") or {}).get("time"), "verification_ok": (t.get("verification") or {}).get("ok")}
        with self._progress_lock:
            prev = self._progress_seen.get(tid)
            self._progress_seen[tid] = snap
        if prev is None:
            return
        notes = progress_notes(prev, snap, t)
        if not notes:
            return
        followers = followers_of(tid)
        if not followers:
            return
        terminal = snap["status"] in ("done", "failed", "stopped")
        with self._progress_lock:
            for chat, username in followers:
                key = (chat, tid)
                box = self._progress_out.setdefault(key, {"notes": [], "last": 0, "username": username})
                box["notes"] = (box["notes"] + notes)[-8:]
                if terminal:
                    box["force"] = True
        if self.sync:
            self.flush_progress(force=True)

    def _progress_loop(self):
        while not self._stop.is_set():
            try:
                self.flush_progress()
            except Exception as e:
                self._error(f"progress: {e}")
            self._stop.wait(5)

    def flush_progress(self, force: bool = False):
        throttle = float(settings().get("progress_throttle_seconds") or 60)
        due = []
        with self._progress_lock:
            for key, box in list(self._progress_out.items()):
                if box["notes"] and (force or box.get("force") or time.time() - box["last"] >= throttle):
                    due.append((key, box["notes"], box["username"]))
                    box.update(notes=[], last=time.time(), force=False)
        for (chat, tid), notes, username in due:
            t = self.m.store.get(tid)
            u = identity.get(username)
            if not t or not u or u.get("disabled"):
                continue
            prefs = ((u.get("prefs") or {}).get("notifications") or {}).get("events") or {}
            status = t.get("status")
            # Delivery and failure already arrive as notifications when the person chose Telegram for them.
            if status == "done" and "telegram" in (prefs.get("delivered") or []):
                notes = [n for n in notes if not n.startswith("🚀")]
            if status == "failed" and "telegram" in (prefs.get("failed") or []):
                notes = [n for n in notes if not n.startswith("❌")]
            if status != "needs_input" or "telegram" in (prefs.get("needs_input") or []):
                notes = [n for n in notes if not n.startswith("❓")]  # answered meanwhile, or the question itself arrives here
            if notes:
                link = self.task_link(t)
                rows = [[{"text": "Open in Relay", "url": link}]] if link else []
                self.safe_send(chat, f"<b>#{t.get('number')}</b> {h(clamp_text(t.get('name'), 80))}\n" + "\n".join(notes),
                               markup=keyboard(rows), thread={"tid": tid, "kind": "progress"})
            if status in ("done", "failed", "stopped"):
                unfollow(chat, tid)


STATUS_ICON = {"running": "▶️", "preparing": "▶️", "planning": "▶️", "implementing": "▶️", "verifying": "▶️", "reviewing": "▶️",
               "delivering": "▶️", "queued": "⏳", "needs_input": "❓", "paused": "⏸", "done": "✅", "failed": "❌", "stopped": "⏹",
               "interrupted": "⚠️", "draft": "📝"}

COMMANDS = {
    "help": Assistant.c_help, "whoami": Assistant.c_whoami, "unlink": Assistant.c_unlink, "status": Assistant.c_status,
    "needs": Assistant.c_needs, "inbox": Assistant.c_needs, "tasks": Assistant.c_tasks, "task": Assistant.c_task, "log": Assistant.c_log,
    "say": Assistant.c_say, "answer": Assistant.c_answer, "approve": Assistant.c_approve, "reject": Assistant.c_reject,
    "new": Assistant.c_new, "pause": Assistant.c_pause, "resume": Assistant.c_resume, "digest": Assistant.c_digest,
    "merge": Assistant.c_merge, "follow": Assistant.c_follow, "unfollow": Assistant.c_unfollow,
}


def _simple_cmd(op, usage):
    def fn(self, chat, user, rest, bang, msg):
        t, _ = self._task_arg(chat, rest, usage)
        if t:
            self.reply_result(chat, msg.get("message_id"), self.execute(user, {"op": op, "tid": t["id"]}, chat))
    return fn


COMMANDS["stop"] = _simple_cmd("stop", "/stop N")
COMMANDS["retry"] = _simple_cmd("retry", "/retry N")
COMMANDS["start_task"] = _simple_cmd("start", "/start N")

LINK_HELP = ("👋 This is <b>Relay</b>'s assistant bot. This chat isn't linked to a Relay account yet, so I can't show or change anything.\n\n"
             "<b>To link it</b>\n"
             "1. Open Relay in your browser.\n"
             "2. Click your avatar → <b>Profile</b> → <b>Notifications</b>.\n"
             "3. In the Telegram card, copy the line <code>/link …</code> and send it here.\n\n"
             "After that you get questions, approvals and deliveries here, can reply to them, and can ask me anything about your tasks.")

HELP_SHORT = ("Ask me anything in plain words (“how is #12 going?”), reply to any Relay message to answer or steer that task, "
              "or use /status, /needs, /tasks. /help lists everything.")

HELP_FULL = """<b>Relay on Telegram</b>
Reply to a question or task message to answer it, or to send guidance to that task. Anything else you write goes to the assistant, which can suggest actions as buttons.

<b>Overview</b>
/status — autopilot, running tasks, needs-you, spend
/needs — everything waiting for you, with buttons
/tasks [running|queued|needs|done|failed|text]
/task N — details · /log N — latest agent messages
/digest [24h|7d]

<b>Steer</b>
/say N text — guidance for the next turn (/say! N interrupts now)
/answer N text · /approve N [note] · /reject N why
/stop N · /retry N · /start N
/new repo: what to do — confirm before it is queued
/merge N — merge a delivered change set (admin)
/pause · /resume — the autopilot
/follow N · /unfollow N — live progress here

/whoami · /unlink"""


# ============================================================================ helpers used by rendering and the router
def phase_of(t: dict) -> str:
    ph = (t.get("checkpoint") or {}).get("phase")
    if t.get("status") in ("done", "failed", "stopped", "queued", "draft", "needs_input", "paused"):
        return {"needs_input": "waiting for you"}.get(t.get("status"), t.get("status"))
    return PHASE_LABEL.get(ph, ph or t.get("status") or "")


def acceptance_progress(t: dict) -> str:
    rows = [c for c in t.get("acceptance") or [] if isinstance(c, dict)]
    if not rows:
        return ""
    met = len([c for c in rows if c.get("status") in ("met", "waived")])
    return f"{met}/{len(rows)} acceptance criteria met"


def condensed_log(m, t: dict, n: int = 12) -> list[str]:
    kinds = ("text", "handoff", "plan", "decision", "review", "question", "user", "error", "approval", "notice")
    rows = [x for x in m.store.messages(t["id"], limit=200) if x.get("kind") in kinds and (x.get("content") or x.get("summary"))]
    out = []
    for x in rows[-n:]:
        who = x.get("role") or "?"
        if x.get("agent") and x.get("agent") != who:
            who += f" · {x['agent']}"
        out.append(f"<b>{h(who)}</b> <i>{h(x.get('kind'))}</i>\n{md_lite(one_line(x.get('summary') or x.get('content'), 280))}")
    return out


def render_task(m, t: dict) -> str:
    lines = [f"{STATUS_ICON.get(t.get('status'), '•')} <b>#{t.get('number')} {h(t.get('name'))}</b>",
             f"{h(t.get('github_repo') or Path(t.get('repo') or '').name)} · {h(t.get('branch_name') or '')}",
             f"Status: <b>{h(t.get('status'))}</b> · phase {h(phase_of(t))}" + (f" · updated {h(ago(t.get('updated_at')))}" if t.get("updated_at") else "")]
    if t.get("detail"):
        lines.append(f"Now: {h(one_line(t.get('detail'), 200))}")
    acc = acceptance_progress(t)
    if acc:
        lines.append(acc)
    v = t.get("verification") or {}
    if v:
        lines.append(f"Verification: {'passed' if v.get('ok') else 'failing'}")
    cost = ((t.get("metrics") or {}).get("total") or {}).get("cost_usd")
    if cost:
        lines.append(f"Cost so far: ~${float(cost):.2f}")
    if t.get("error") and t.get("status") in ("failed", "interrupted"):
        lines.append(f"Error: <blockquote>{h(clamp_text(t['error'], 500))}</blockquote>")
    pend = t.get("pending") or {}
    if pend:
        lines.append(f"\n<b>Waiting for you</b> ({h(pend.get('kind'))}):\n<blockquote>{md_lite(clamp_text(pend.get('question') or '', 700))}</blockquote>")
        if pend.get("kind") == "question":
            lines.append("<i>Reply to this message to answer.</i>")
    prs = [t.get("pr_url")] + [w.get("pr_url") for w in (t.get("repo_worktrees") or {}).values() if isinstance(w, dict)]
    prs = [p for p in prs if p]
    if prs:
        lines.append("Pull requests: " + " · ".join(f"<a href=\"{h(p)}\">{h(p.rsplit('/', 3)[-3] if p.count('/') >= 3 else p)}#{h(p.rsplit('/', 1)[-1])}</a>" for p in prs[:4]))
    events = (t.get("events") or [])[-4:]
    if events:
        lines.append("\n<b>Latest</b>")
        for e in events:
            lines.append(f"• {h(one_line(e.get('title'), 60))}" + (f": {h(one_line(e.get('detail'), 110))}" if e.get("detail") else ""))
    if not pend:
        lines.append("\n<i>Reply to this message to send guidance to the task.</i>")
    return "\n".join(lines)


def task_buttons(t: dict, link: str | None, username: str, chat) -> list:
    rows = []
    pend = t.get("pending") or {}
    if pend.get("kind") in ("approval", "design_approval"):
        rows.append([{"text": "✅ Approve", "callback_data": cb({"op": "approve", "tid": t["id"], "qid": pend["id"]})},
                     {"text": "✏️ Request changes", "callback_data": cb({"op": "reject_reason", "tid": t["id"], "qid": pend["id"]})}])
    elif pend.get("options"):
        for o in list(pend["options"])[:4]:
            rows.append([{"text": clamp_text(o, 60), "callback_data": cb({"op": "answer", "tid": t["id"], "qid": pend["id"], "text": o})}])
    status = t.get("status")
    ctl = []
    if status in ("failed", "stopped", "interrupted"):
        ctl.append({"text": "🔁 Retry", "callback_data": cb({"op": "retry", "tid": t["id"]})})
    if status in ("draft",):
        ctl.append({"text": "▶️ Start", "callback_data": cb({"op": "start", "tid": t["id"]})})
    if status not in ("done", "failed", "stopped", "draft"):
        ctl.append({"text": "🔔 Follow", "callback_data": cb({"op": "follow", "tid": t["id"]})})
    if ctl:
        rows.append(ctl)
    if link:
        rows.append([{"text": "Open in Relay", "url": link}])
    return rows


def render_item(it: dict, t: dict | None, link: str | None) -> tuple[str, dict | None, dict | None]:
    """One Needs-you item (autopilot inbox or tool request) as a message with its buttons."""
    kind = it.get("kind")
    head = f"<b>#{h(it.get('number'))}</b> {h(clamp_text(it.get('task'), 80))}" if it.get("number") else h(it.get("task") or "")
    rows, thread = [], None
    if kind == "tool_request":
        text = (f"🧰 <b>Tool request</b> · {head}\n<b>{h(it.get('name') or it.get('question') or '')}</b>\n"
                f"{h(clamp_text(it.get('why') or it.get('summary') or it.get('question') or '', 500))}")
        rows.append([{"text": "Approve tool", "callback_data": cb({"op": "tool_approve", "rid": it.get("id")})},
                     {"text": "Deny", "callback_data": cb({"op": "tool_deny", "rid": it.get("id")})}])
        thread = {"tid": it.get("task_id"), "rid": it.get("id"), "kind": "tool_request"}
    elif kind in ("approval", "design_approval"):
        what = "Design approval" if kind == "design_approval" else "Approve delivery"
        body = it.get("summary") or it.get("question") or ""
        text = f"🟢 <b>{what}</b> · {head}\n<blockquote>{md_lite(clamp_text(body, 1500))}</blockquote>" + (f"\n<code>{h(clamp_text(it.get('diffstat'), 300))}</code>" if it.get("diffstat") else "")
        text += "\n<i>Approve, request changes, or reply with a note.</i>"
        rows.append([{"text": "✅ Approve", "callback_data": cb({"op": "approve", "tid": it["task_id"], "qid": it.get("id")})},
                     {"text": "✏️ Request changes", "callback_data": cb({"op": "reject_reason", "tid": it["task_id"], "qid": it.get("id")})}])
        thread = {"tid": it["task_id"], "qid": it.get("id"), "kind": kind}
    elif kind in ("question", "escalation"):
        label = "Verification escalation" if kind == "escalation" else f"Question from {it.get('from') or 'the team'}"
        text = f"❓ <b>{h(label)}</b> · {head}\n<blockquote>{md_lite(clamp_text(it.get('question'), 1800))}</blockquote>\n<i>Reply to this message to answer.</i>"
        for o in (it.get("options") or [])[:4]:
            rows.append([{"text": clamp_text(o, 60), "callback_data": cb({"op": "answer", "tid": it["task_id"], "qid": it.get("id"), "text": o})}])
        thread = {"tid": it["task_id"], "qid": it.get("id"), "kind": "question"}
    elif kind == "parked":
        text = f"⏸ <b>Paused by autopilot</b> · {head}\n{h(it.get('question'))}"
        rows.append([{"text": "▶️ Resume", "callback_data": cb({"op": "resume", "tid": it["task_id"]})}])
        thread = {"tid": it["task_id"], "kind": "task"}
    else:
        text = f"⚠️ <b>{h(kind)}</b> · {head}\n{h(it.get('question') or '')}"
        thread = {"tid": it.get("task_id"), "kind": "task"} if it.get("task_id") else None
    if link:
        rows.append([{"text": "Open in Relay", "url": link}])
    return text, keyboard(rows), thread


def render_digest(d: dict) -> str:
    hd = d.get("headline") or {}
    lines = [f"📊 <b>Digest</b> · last {h(d.get('hours'))} h", h(d.get("summary") or "")]
    u = d.get("usage") or {}
    lines.append(f"Spend today ${float(u.get('today_usd') or 0):.2f}" + (f" of ${float(u['daily_cap_usd']):.2f}" if u.get("daily_cap_usd") else ""))
    if d.get("delivered"):
        lines.append("\n<b>Delivered</b>")
        for r in d["delivered"][:6]:
            pr = f" · <a href=\"{h(r['pr_url'])}\">PR</a>" if r.get("pr_url") else ""
            lines.append(f"✅ #{h(r.get('number'))} {h(clamp_text(r.get('name'), 60))}{pr}" + (f" · score {h(r['score'])}" if r.get("score") is not None else ""))
    if d.get("failures"):
        lines.append("\n<b>Failed or stopped</b>")
        for r in d["failures"][:6]:
            lines.append(f"❌ #{h(r.get('number'))} {h(clamp_text(r.get('name'), 60))} · {h(r.get('category_label') or r.get('category'))}")
    if hd.get("needs_you"):
        lines.append(f"\n❓ {hd['needs_you']} item(s) need you: /needs")
    if d.get("next"):
        lines.append("\n<b>Next up</b>")
        for r in d["next"][:4]:
            lines.append(f"⏳ #{h(r.get('number'))} {h(clamp_text(r.get('name'), 60))}")
    return "\n".join(lines)


def render_notification(m, msg: dict, t: dict | None) -> tuple[str, dict | None, dict | None]:
    """A notification as a Telegram message: headline, the task, the actual context, buttons, and the thread entry
    that lets a plain reply act on it."""
    event = msg.get("event")
    icon = EVENT_ICON.get(event, "🔔")
    lines = [f"{icon} <b>{h(msg.get('title') or '')}</b>"]
    rows, thread = [], None
    link = (msg.get("links") or {}).get("task") or notify.primary_link(msg)
    if t:
        lines.append(f"<b>#{t.get('number')}</b> {h(t.get('name'))}" + (f" · <i>{h(t.get('github_repo') or Path(t.get('repo') or '').name)}</i>"))
        thread = {"tid": t["id"], "kind": "task", "event": event}
        pend = t.get("pending") or {}
        if event in ("needs_input", "approval") and pend:
            kind = pend.get("kind") or "question"
            thread.update(qid=pend.get("id"), kind=kind)
            if kind == "design_approval":
                lines.append(f"<blockquote expandable>{md_lite(clamp_text(pend.get('summary') or pend.get('question'), 1800))}</blockquote>")
                lines.append("<i>Approve, request changes, or reply to this message with a note.</i>")
            elif kind == "approval":
                summ = pend.get("summary") or t.get("pr_summary") or ""
                if summ:
                    lines.append(f"<blockquote expandable>{md_lite(clamp_text(summ, 1800))}</blockquote>")
                if pend.get("diffstat"):
                    lines.append(f"<code>{h(clamp_text(pend['diffstat'], 300))}</code>")
                acc = acceptance_progress(t)
                if acc:
                    lines.append(acc)
                lines.append("<i>Approve, request changes, or reply to this message with a note.</i>")
            else:
                label = "Verification escalation" if pend.get("from") == "orchestrator" else f"{pend.get('from') or 'The team'} asks"
                lines.append(f"{h(label)}:\n<blockquote expandable>{md_lite(clamp_text(pend.get('question'), 2500))}</blockquote>")
                lines.append("<i>Reply to this message to answer" + (", or pick an option." if pend.get("options") else ".") + "</i>")
            if kind in ("approval", "design_approval"):
                rows.append([{"text": "✅ Approve", "callback_data": cb({"op": "approve", "tid": t["id"], "qid": pend["id"]})},
                             {"text": "✏️ Request changes", "callback_data": cb({"op": "reject_reason", "tid": t["id"], "qid": pend["id"]})}])
            else:
                for o in list(pend.get("options") or [])[:4]:
                    rows.append([{"text": clamp_text(o, 60), "callback_data": cb({"op": "answer", "tid": t["id"], "qid": pend["id"], "text": o})}])
        elif event == "needs_input":
            reqs = []
            try:
                from .. import toolbox
                reqs = toolbox.requests("pending", task=t["id"])
            except Exception:
                pass
            if reqs:
                r = reqs[-1]
                lines.append(f"🧰 <b>{h(r.get('name'))}</b>: {h(clamp_text(r.get('why') or '', 500))}")
                rows.append([{"text": "Approve tool", "callback_data": cb({"op": "tool_approve", "rid": r["id"]})},
                             {"text": "Deny", "callback_data": cb({"op": "tool_deny", "rid": r["id"]})}])
                thread = {"tid": t["id"], "rid": r["id"], "kind": "tool_request"}
            else:
                if msg.get("body"):
                    lines.append(h(clamp_text(msg["body"], 800)))
                if (t.get("autopilot_parked") or {}) and t.get("status") == "paused":
                    rows.append([{"text": "▶️ Resume", "callback_data": cb({"op": "resume", "tid": t["id"]})}])
        elif event == "failed":
            err = t.get("error") or t.get("detail") or msg.get("body") or ""
            lines.append(f"<blockquote expandable>{h(clamp_text(err, 900))}</blockquote>")
            acc = acceptance_progress(t)
            if acc:
                lines.append(acc)
            lines.append("<i>Reply to this message to resume it with guidance.</i>")
            if t.get("status") in ("failed", "stopped", "interrupted"):
                rows.append([{"text": "🔁 Retry", "callback_data": cb({"op": "retry", "tid": t["id"]})}])
        elif event == "delivered":
            summ = t.get("pr_summary") or t.get("summary") or ""
            if summ:
                lines.append(f"<blockquote expandable>{md_lite(clamp_text(summ, 1200))}</blockquote>")
            acc = acceptance_progress(t)
            if acc:
                lines.append(acc)
            cost = ((t.get("metrics") or {}).get("total") or {}).get("cost_usd")
            if cost:
                lines.append(f"Cost ~${float(cost):.2f}")
            if t.get("pr_url"):
                rows.append([{"text": "Pull request", "url": t["pr_url"]}])
                lines.append(f"<a href=\"{h(t['pr_url'])}\">{h(t['pr_url'])}</a>")
        elif msg.get("body"):
            lines.append(h(clamp_text(msg["body"], 900)))
    else:
        if event == "digest":
            try:
                lines = [render_digest(m.autopilot.digest(hours=24))]
            except Exception:
                lines.append(h(msg.get("body") or ""))
        elif msg.get("body"):
            lines.append(h(clamp_text(msg["body"], 1500)))
    if link and link.startswith("https://"):
        rows.append([{"text": "Open in Relay", "url": link}])
    return "\n".join(lines), keyboard(rows), thread


def progress_notes(prev: dict, snap: dict, t: dict) -> list[str]:
    notes = []
    if snap["phase"] != prev["phase"] and snap["phase"] and snap["phase"] not in ("done", "delivered"):
        notes.append(f"➡️ Phase: {PHASE_LABEL.get(snap['phase'], snap['phase'])}")
    if snap["packages"] > prev["packages"]:
        notes.append(f"📦 Work package {snap['packages']} done")
    if snap["verification"] and snap["verification"] != prev["verification"]:
        notes.append("🧪 Verification " + ("passed" if snap["verification_ok"] else "failing"))
    if snap["status"] != prev["status"]:
        s = snap["status"]
        if s == "done":
            notes.append("🚀 Delivered" + (f": {t.get('pr_url')}" if t.get("pr_url") else ""))
        elif s == "failed":
            notes.append(f"❌ Failed: {one_line(t.get('error') or t.get('detail'), 200)}")
        elif s == "stopped":
            notes.append("⏹ Stopped")
        elif s == "needs_input":
            q = re.sub(r"\*\*|`", "", str((t.get("pending") or {}).get("question") or t.get("detail") or ""))
            notes.append("❓ Waiting for you: " + one_line(q, 160) + " (reply to the question message, or /needs)")
    return [h(n) for n in notes]


def follow(chat, tid, username):
    if chat is None:
        return
    update_chat(chat, lambda c: c.update(follows=list(dict.fromkeys((c.get("follows") or []) + [tid]))[-30:], username=username))


def unfollow(chat, tid):
    update_chat(chat, lambda c: c.update(follows=[x for x in c.get("follows") or [] if x != tid]))


def followers_of(tid) -> list[tuple]:
    return [(int(chat) if re.fullmatch(r"-?\d+", chat) else chat, c.get("username")) for chat, c in (_chats.read().get("chats") or {}).items()
            if tid in (c.get("follows") or []) and c.get("username")]


def default_workflow(cfg: dict) -> dict:
    """The team a new task gets by default: the preset's agents with the configured model and effort only where the
    configured role uses the same agent (a Claude model name means nothing to Codex)."""
    from .. import config as C
    preset = C.preset(cfg.get("workflow_preset"))
    roles = {}
    for r in ("supervisor", "worker", "reviewer"):
        mine = (cfg.get("roles") or {}).get(r) or {}
        agent = ((preset or {}).get("roles") or {}).get(r, {}).get("agent", "") if preset else mine.get("agent", "")
        same = agent and agent == mine.get("agent")
        roles[r] = {"agent": agent or "", "model": (mine.get("model") or "") if same else "", "effort": (mine.get("effort") or "") if same else ""}
    return {"preset": cfg.get("workflow_preset") if preset else "custom", "roles": roles}


def known_repos(m) -> list[str]:
    from .. import config as C, repos
    out = []
    try:
        out += [str(p) for p in repos.candidate_repos()]
    except Exception:
        pass
    out += [str(r) for r in (m.cfg().get("recent_repos") or [])]
    out += [t.get("repo") for t in m.store.list() if t.get("repo")]
    return [p for p in dict.fromkeys(out) if p and Path(p).is_dir()]


def resolve_repo(m, name: str) -> tuple[str | None, list[str], bool]:
    """(path, candidates, ambiguous): exact folder or GitHub name first, then a unique partial match."""
    q = (name or "").strip().lower().rstrip("/")
    if not q:
        return None, [], False
    paths = known_repos(m)
    gh = {}
    for t in m.store.list():
        if t.get("repo") and t.get("github_repo"):
            gh[t["repo"]] = t["github_repo"].lower()
    exact = [p for p in paths if Path(p).name.lower() == q or gh.get(p) == q or (gh.get(p) or "").endswith("/" + q) or p.lower() == q]
    if len(exact) == 1:
        return exact[0], exact, False
    if len(exact) > 1:
        return None, exact, True
    part = [p for p in paths if q in Path(p).name.lower() or q in (gh.get(p) or "")]
    if len(part) == 1:
        return part[0], part, False
    return None, (part or paths), len(part) > 1


def enable_telegram_notifications(username: str, chat) -> list[str]:
    """After linking: the chat receives notifications, and questions, approvals, deliveries and failures reach it
    unless the person already chose Telegram for some events (then their choice stands)."""
    u = identity.get(username)
    if not u:
        return []
    n = u["prefs"]["notifications"]
    events = n.get("events") or {}
    already = any("telegram" in (v or []) for v in events.values())
    patch = {"notifications": {"channels": {"telegram": {"chat_id": str(chat)}}}}
    turned = []
    if not already:
        new_events = {}
        for ev in ("needs_input", "approval", "delivered", "failed"):
            new_events[ev] = list(dict.fromkeys(list(events.get(ev) or []) + ["telegram"]))
            turned.append(ev)
        patch["notifications"]["events"] = {**events, **new_events}
    try:
        identity.save_prefs(username, patch)
    except ValueError:
        return []
    return turned

