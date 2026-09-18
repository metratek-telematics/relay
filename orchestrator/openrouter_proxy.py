"""Relay's local OpenRouter gateway: every agent turn on OpenRouter talks to it instead of openrouter.ai.

Why a gateway instead of handing each CLI the key:
  * the key never reaches an agent. Agents run arbitrary shell commands; a turn gets a random token that works only
    here, only while its turn runs, and only for model calls;
  * one place applies Relay's policy to every CLI alike: routing and privacy preferences, attribution headers,
    the free-model pace (20 requests a minute), waiting out 429s, moving to the next model when one is rate limited
    or unavailable (the automatic free pick rotates, other roles use the configured fallback models), and the
    monthly spend caps (a request that would pass one is refused with a 402 Relay explains);
  * real cost per turn: OpenRouter reports the cost of each generation in its usage block; what a stream does not
    carry is looked up at /api/v1/generation when the turn ends.

The gateway listens on 127.0.0.1 only, on a random port, inside Relay's process. It forwards /api/v1/chat/completions
(OpenAI chat), /api/v1/responses (Codex), /api/v1/messages (Claude Code, OpenRouter's Anthropic skin) and the model
list; everything else is refused. Streams are passed through as they arrive (chunked), and parsed on the way for
usage, the model that answered and the generation id.
"""
from __future__ import annotations

import http.client
import json
import secrets
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlencode

from . import openrouter as OR

ALLOWED_POST = ("/api/v1/chat/completions", "/api/v1/completions", "/api/v1/responses", "/api/v1/messages",
                "/api/v1/messages/count_tokens")
HOP = {"host", "authorization", "x-api-key", "content-length", "connection", "transfer-encoding", "accept-encoding",
       "keep-alive", "proxy-authorization", "te", "upgrade", "http-referer", "referer", "x-title", "x-openrouter-title"}
TASKS_FN = None          # set by the manager: () -> list of tasks, for the spend caps
_turns: dict = {}
_turns_lock = threading.Lock()
_free_times: deque = deque()
_free_lock = threading.Lock()
_server = {"srv": None, "port": None}
_start_lock = threading.Lock()


# ============================================================================ turns
class Turn:
    """One agent turn's pass through the gateway: who it is, which model it may use, and what it spent."""

    def __init__(self, task_id, project, role, agent, model, rotation=None, auto=False):
        self.token = "sk-or-v1-relay-" + secrets.token_hex(24)
        self.task_id, self.project, self.role, self.agent = task_id, project, role, agent
        self.requested = model
        self.model = model
        self.rotation = [m for m in (rotation or []) if m and m != model]
        self.auto = auto
        self.started = time.time()
        self.records: list[dict] = []
        self.rotations: list[dict] = []
        self.rerouted = 0
        self.active = 0
        self.closed = False
        self.lock = threading.Lock()

    def spent(self) -> float:
        return sum(float(r.get("cost") or 0) for r in self.records)


def open_turn(task_id: str, project: str | None, role: str, agent: str, model: str, rotation=None, auto=False) -> Turn:
    ensure_started()
    t = Turn(task_id, project, role, agent, model, rotation, auto)
    with _turns_lock:
        _turns[t.token] = t
    return t


def get_turn(token: str) -> Turn | None:
    with _turns_lock:
        return _turns.get(token)


def close_turn(token: str, wait: float = 10.0, resolve_costs: bool = True) -> dict:
    """End a turn: its token stops working, missing costs are looked up, and the tally comes back."""
    t = get_turn(token)
    if not t:
        return summarize(None)
    t.closed = True
    deadline = time.time() + wait
    while t.active and time.time() < deadline:
        time.sleep(0.1)
    with _turns_lock:
        _turns.pop(token, None)
    if resolve_costs:
        _resolve_costs(t)
    return summarize(t)


def _resolve_costs(t: Turn):
    """OpenRouter's generation stats for answers whose stream did not carry a cost (they appear after a moment)."""
    key = OR.api_key()
    todo = [r for r in t.records if r.get("status") == 200 and r.get("cost") is None and r.get("id")]
    for attempt in range(3):
        if not todo or not key:
            return
        if attempt:
            time.sleep(1.5)
        left = []
        for r in todo:
            try:
                st, data = OR._request("GET", "/generation?" + urlencode({"id": r["id"]}), key, timeout=10)
            except Exception:
                st, data = 0, {}
            d = (data or {}).get("data") or {}
            if st == 200 and d.get("total_cost") is not None:
                r["cost"] = float(d["total_cost"])
                r["cost_source"] = "generation"
                r["input"] = r.get("input") or int(d.get("native_tokens_prompt") or d.get("tokens_prompt") or 0)
                r["output"] = r.get("output") or int(d.get("native_tokens_completion") or d.get("tokens_completion") or 0)
                r["model_used"] = r.get("model_used") or d.get("model")
                r["provider"] = r.get("provider") or d.get("provider_name")
            else:
                left.append(r)
        todo = left


def summarize(t: Turn | None) -> dict:
    out = {"requests": 0, "ok_requests": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0, "cost_exact": True,
           "models": [], "model_costs": {}, "providers": [], "errors": [], "last_error": None, "rotations": [], "rerouted": 0,
           "seconds": 0.0}
    if not t:
        return out
    rows = None
    for r in t.records:
        out["requests"] += 1
        if r.get("stream_error"):
            out["errors"].append({"status": r.get("status"), "message": r.get("message"), "model_sent": r.get("model_sent")})
        if r.get("status") != 200:
            out["errors"].append({k: r.get(k) for k in ("status", "message", "model_sent", "category")})
            continue
        out["ok_requests"] += 1
        out["input"] += int(r.get("input") or 0)
        out["output"] += int(r.get("output") or 0)
        out["cached"] += int(r.get("cached") or 0)
        mid = r.get("model_used") or r.get("model_sent") or t.model
        cost = r.get("cost")
        if cost is None:
            if rows is None:
                rows = OR.catalog()["models"]
            cost = OR.estimate_cost(r.get("model_sent") or mid, r.get("input") or 0, r.get("output") or 0, r.get("cached") or 0, rows)
            out["cost_exact"] = False
            r["cost_estimated"] = True
        cost = float(cost or 0)
        out["cost_usd"] += cost
        out["model_costs"][mid] = round(out["model_costs"].get(mid, 0.0) + cost, 8)
        if mid not in out["models"]:
            out["models"].append(mid)
        if r.get("provider") and r["provider"] not in out["providers"]:
            out["providers"].append(r["provider"])
    out["cost_usd"] = round(out["cost_usd"], 8)
    failed = [r for r in t.records if r.get("status") != 200]
    if failed and (not out["ok_requests"] or t.records[-1].get("status") != 200):
        last = failed[-1]
        out["last_error"] = {"status": last.get("status"), "message": last.get("message"), "category": last.get("category"),
                             "model": last.get("model_sent")}
    out["rotations"] = list(t.rotations)
    out["rerouted"] = t.rerouted
    out["seconds"] = round(time.time() - t.started, 1)
    out["final_model"] = t.model
    return out


# ============================================================================ server
def ensure_started() -> int:
    with _start_lock:
        if _server["srv"]:
            return _server["port"]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.daemon_threads = True
        _server.update(srv=srv, port=srv.server_address[1])
        threading.Thread(target=srv.serve_forever, name="relay-openrouter-gateway", daemon=True).start()
        return _server["port"]


def base_openai() -> str:
    return f"http://127.0.0.1:{ensure_started()}/api/v1"


def base_anthropic() -> str:
    return f"http://127.0.0.1:{ensure_started()}/api"


def _pace_free(model: str, per_minute: int):
    """Stay under OpenRouter's per-minute limit for free models instead of collecting 429s."""
    if not (model.endswith(":free") or model == OR.FREE_ROUTER):
        return 0.0
    waited = 0.0
    while True:
        with _free_lock:
            now = time.time()
            while _free_times and now - _free_times[0] > 60:
                _free_times.popleft()
            if len(_free_times) < per_minute:
                _free_times.append(now)
                return waited
            pause = 60 - (now - _free_times[0]) + 0.05
        time.sleep(min(pause, 5))
        waited += min(pause, 5)


def _upstream():
    u = urlparse(OR.base_url())
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return conn_cls, u.hostname, u.port, u.path.rstrip("/")  # path: /api/v1


def _sse_usage(rec: dict, obj: dict):
    """Pull usage, cost, model and id out of one parsed stream event (chat, responses or messages)."""
    if not isinstance(obj, dict):
        return
    typ = obj.get("type") or ""
    if typ.startswith("response."):
        resp = obj.get("response") if isinstance(obj.get("response"), dict) else None
        if resp:
            rec["id"] = resp.get("id") or rec.get("id")
            rec["model_used"] = resp.get("model") or rec.get("model_used")
            u = resp.get("usage")
            if isinstance(u, dict):
                rec["input"] = int(u.get("input_tokens") or 0)
                rec["output"] = int(u.get("output_tokens") or 0)
                rec["cached"] = int(((u.get("input_tokens_details") or {}).get("cached_tokens")) or 0)
                if u.get("cost") is not None:
                    rec["cost"] = float(u["cost"])
            if typ == "response.failed" and isinstance(resp.get("error"), dict):
                rec["stream_error"] = resp["error"].get("message")
        return
    if typ == "message_start":
        m = obj.get("message") or {}
        rec["id"] = m.get("id") or rec.get("id")
        rec["model_used"] = m.get("model") or rec.get("model_used")
        u = m.get("usage") or {}
        rec["input"] = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
        rec["cached"] = int(u.get("cache_read_input_tokens") or 0)
        return
    if typ == "message_delta":
        u = obj.get("usage") or {}
        if u.get("output_tokens") is not None:
            rec["output"] = int(u["output_tokens"])
        if u.get("input_tokens"):
            rec["input"] = max(int(rec.get("input") or 0), int(u["input_tokens"]))
        if u.get("cost") is not None:
            rec["cost"] = float(u["cost"])
        return
    if typ == "error" and isinstance(obj.get("error"), dict):
        rec["stream_error"] = obj["error"].get("message")
        return
    # chat completions chunk or a whole non-streamed body (all three formats)
    if obj.get("id") and not rec.get("id"):
        rec["id"] = obj["id"]
    if obj.get("model"):
        rec["model_used"] = obj["model"]
    if obj.get("provider"):
        rec["provider"] = obj["provider"]
    u = obj.get("usage")
    if isinstance(u, dict) and u:
        if "prompt_tokens" in u:
            rec["input"] = int(u.get("prompt_tokens") or 0)
            rec["output"] = int(u.get("completion_tokens") or 0)
            rec["cached"] = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        elif "input_tokens" in u:
            rec["input"] = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
            rec["output"] = int(u.get("output_tokens") or 0)
            rec["cached"] = int(u.get("cache_read_input_tokens") or ((u.get("input_tokens_details") or {}).get("cached_tokens")) or 0)
        if u.get("cost") is not None:
            rec["cost"] = float(u["cost"])
    if isinstance(obj.get("error"), dict):
        rec["stream_error"] = obj["error"].get("message")


def _merge_routing(body: dict, prefs: dict):
    """Relay's routing policy into the request. Privacy settings always win; the rest only fill what the CLI left out."""
    if not prefs:
        return
    cur = body.get("provider") if isinstance(body.get("provider"), dict) else {}
    merged = {**prefs, **cur}
    for k in ("data_collection", "zdr"):
        if k in prefs:
            merged[k] = prefs[k]
    body["provider"] = merged


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RelayOpenRouterGateway/1"

    def log_message(self, fmt, *args):
        pass

    # ---- small helpers
    def _json(self, status: int, obj: dict, headers: dict | None = None):
        data = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _relay_error(self, status: int, message: str, source: str = "relay"):
        self._json(status, {"error": {"code": status, "message": message, "metadata": {"limit_source": source}}})

    def _token(self) -> str:
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def _read_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            out = b""
            while True:
                size = int((self.rfile.readline().split(b";")[0].strip() or b"0"), 16)
                if not size:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- GET: the model list only (some CLIs ask for it)
    def do_GET(self):
        path = urlparse(self.path).path
        t = get_turn(self._token())
        if not t:
            return self._relay_error(401, "Relay: this OpenRouter session is not active (the agent turn ended)")
        if not (path == "/api/v1/models" or path.startswith("/api/v1/models/")):
            return self._relay_error(404, f"Relay's OpenRouter gateway does not forward {path}")
        self._forward(t, "GET", path, None)

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._read_body()
        t = get_turn(self._token())
        if not t:
            return self._relay_error(401, "Relay: this OpenRouter session is not active (the agent turn ended)")
        if path not in ALLOWED_POST:
            return self._relay_error(404, f"Relay's OpenRouter gateway does not forward {path}")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._relay_error(400, "Relay: the request body is not JSON")
        if not isinstance(body, dict):
            return self._relay_error(400, "Relay: the request body is not a JSON object")
        with t.lock:
            t.active += 1
        try:
            self._model_call(t, path, body)
        finally:
            with t.lock:
                t.active -= 1

    # ---- the model call with Relay's policy
    def _model_call(self, t: Turn, path: str, body: dict):
        s = OR.settings()
        if not OR.api_key(s):
            return self._relay_error(401, "Relay: no OpenRouter API key is set (Settings → Providers → OpenRouter)")
        if path.endswith("/count_tokens"):
            return self._forward(t, "POST", path, {**body, "model": t.model}, record=False)
        asked = str(body.get("model") or "")
        if asked and asked != t.model:
            t.rerouted += 1  # side calls (titles, summaries, "small fast" models) run on the role's model, never a surprise one
        candidates = [t.model] + [m for m in t.rotation if m != t.model]
        wait_budget = float(s.get("rate_limit_wait_seconds") or 0)
        waited = 0.0
        last = None
        i = 0
        while i < len(candidates):
            model = candidates[i]
            if not OR.is_free(model) and TASKS_FN:
                cap = None
                try:
                    cap = OR.cap_state(TASKS_FN(), t.project, s, extra=t.spent())
                except Exception:
                    cap = None
                if cap:
                    return self._relay_error(402, f"Relay: {cap['reason']}. Raise the cap in Settings → Providers → OpenRouter or pick a free model.",
                                             "relay_cap")
            waited += _pace_free(model, int(s.get("free_rate_per_minute") or 20))
            out = dict(body)
            out["model"] = model
            if not path.endswith("/messages"):
                _merge_routing(out, OR.routing(s))
            if path.endswith("/chat/completions") and not isinstance(out.get("usage"), dict):
                out["usage"] = {"include": True}
            res = self._forward(t, "POST", path, out, final=False)
            if res is None:
                return  # streamed to the client: done
            status, payload, headers, rec = res
            last = (status, payload, headers)
            if status == 429 and waited < wait_budget:
                retry_after = headers.get("retry-after")
                try:
                    pause = min(30.0, max(1.0, float(retry_after)))
                except (TypeError, ValueError):
                    pause = min(30.0, 2.0 * 2 ** min(4, sum(1 for r in t.records if r.get("status") == 429 and r.get("model_sent") == model) - 1))
                pause = min(pause, max(0.5, wait_budget - waited))
                time.sleep(pause)
                waited += pause
                continue  # same model again
            if status in (429, 404, 502, 503) and i + 1 < len(candidates):
                reason = OR.describe_error(status, rec.get("message") or "", None, model)["message"]
                if t.auto or status != 429:
                    OR.cooldown(model, reason, status)
                nxt = candidates[i + 1]
                t.rotations.append({"from": model, "to": nxt, "status": status, "at": time.time()})
                t.model = nxt
                i += 1
                waited = 0.0
                continue
            if status in (429, 404, 503) and t.auto:
                OR.cooldown(model, OR.describe_error(status, rec.get("message") or "", None, model)["message"], status)
            break
        if last:
            status, payload, headers = last
            self._send_upstream_error(status, payload, headers)

    def _send_upstream_error(self, status, payload, headers):
        extra = {"Retry-After": headers["retry-after"]} if headers.get("retry-after") else None
        self._json(status, payload if isinstance(payload, dict) and payload else {"error": {"code": status, "message": f"OpenRouter answered {status}"}}, extra)

    def _forward(self, t: Turn, method: str, path: str, body, final: bool = True, record: bool = True):
        """Send one request upstream. Success streams straight to the client and returns None; an error returns
        (status, payload, headers, record) when final is False so the caller may retry, else it is sent as is."""
        conn_cls, host, port, base = _upstream()
        up_path = base + path[len("/api/v1"):]
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Authorization"] = f"Bearer {OR.api_key()}"
        headers["Accept-Encoding"] = "identity"
        headers.update(OR.attribution_headers())
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        rec = {"t": time.time(), "endpoint": path.rsplit("/", 1)[-1] if not path.endswith("/completions") else "chat",
               "model_sent": (body or {}).get("model") if isinstance(body, dict) else None, "status": None,
               "input": 0, "output": 0, "cached": 0, "cost": None}
        if path.endswith("/responses"):
            rec["endpoint"] = "responses"
        elif path.endswith("/messages"):
            rec["endpoint"] = "messages"
        try:
            conn = conn_cls(host, port, timeout=900)
            conn.request(method, up_path, body=data, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            rec.update(status=502, message=f"OpenRouter is unreachable: {e}", category="provider_unavailable")
            if record:
                t.records.append(rec)
            payload = {"error": {"code": 502, "message": f"Relay could not reach OpenRouter: {e}"}}
            if final:
                self._json(502, payload)
                return None
            return 502, payload, {}, rec
        rec["status"] = resp.status
        up_headers = {k.lower(): v for k, v in resp.getheaders()}
        if resp.status >= 400:
            raw = resp.read()
            conn.close()
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {"error": {"code": resp.status, "message": raw.decode("utf-8", "replace")[:500]}}
            err = payload.get("error") if isinstance(payload, dict) else None
            meta = err.get("metadata") if isinstance(err, dict) else None
            d = OR.describe_error(resp.status, OR._err_msg(payload), meta if isinstance(meta, dict) else None, str(rec.get("model_sent") or ""))
            rec.update(message=d["message"], category=d["category"])
            if record:
                t.records.append(rec)
            if final:
                self._send_upstream_error(resp.status, payload, up_headers)
                return None
            return resp.status, payload, up_headers, rec
        # success: pass it through as it arrives
        stream = "text/event-stream" in (up_headers.get("content-type") or "")
        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("content-type", "cache-control", "x-generation-id", "openrouter-provider", "request-id", "x-request-id"):
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            conn.close()
            return None
        if up_headers.get("x-generation-id"):
            rec["id"] = up_headers["x-generation-id"]
        buf, whole, client_gone = b"", [], False
        while True:
            try:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            except (OSError, http.client.HTTPException):
                break
            if not chunk:
                break
            if not client_gone:
                try:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    client_gone = True  # the CLI went away (stopped turn); keep reading for the usage
            if stream:
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    line = line.strip()
                    if line.startswith(b"data:"):
                        payload = line[5:].strip()
                        if payload and payload != b"[DONE]":
                            try:
                                _sse_usage(rec, json.loads(payload))
                            except ValueError:
                                pass
            else:
                whole.append(chunk)
        conn.close()
        if not client_gone:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if whole:
            try:
                _sse_usage(rec, json.loads(b"".join(whole)))
            except ValueError:
                pass
        if rec.get("stream_error"):
            rec["message"] = f"OpenRouter stream error: {rec['stream_error']}"
        if record and path != "/api/v1/models" and not path.startswith("/api/v1/models/"):
            t.records.append(rec)
        return None
