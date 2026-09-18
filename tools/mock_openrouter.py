#!/usr/bin/env python3
"""A local stand-in for OpenRouter, for development and end-to-end tests only.

It speaks the three wire formats coding CLIs use against OpenRouter:
  POST /api/v1/chat/completions   OpenAI chat completions (OpenCode, Kilo, Cline, Goose, Aider, Crush, Qwen, Continue)
  POST /api/v1/responses          OpenAI Responses API (Codex)
  POST /api/v1/messages           Anthropic Messages, OpenRouter's "Anthropic skin" (Claude Code)
plus GET /api/v1/models, /api/v1/key, /api/v1/credits and /api/v1/generation?id=…, all streaming and non-streaming.

Its "model" is a script that plays one Relay role well enough for a task to finish: the supervisor plans one work
package and then declares done, the worker writes HELLO_OPENROUTER.md with whatever shell or write tool the CLI
offers and reports, the reviewer passes. Every request is logged (auth header fingerprint, attribution headers, model,
routing preferences) at GET /__mock/log so a test can prove the wiring; POST /__mock/config switches failure modes
(credits, forced status codes) to exercise Relay's error mapping.

    python tools/mock_openrouter.py --port 8899 [--key sk-or-v1-test]

Never point a real Relay at this: it accepts any key it is told to and answers with canned text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FILE = "HELLO_OPENROUTER.md"
CONTENT = "# Hello from OpenRouter\n\nWritten by a Relay agent running through the mock OpenRouter server.\n"
PRICE_IN, PRICE_OUT = 3e-6, 15e-6  # USD per token, like a mid-size paid model

STATE = {
    "key": "",                   # expected key ("" = accept any non-empty bearer)
    "credits": 25.0, "usage": 1.25, "limit": None, "is_free_tier": False,
    "force": {},                 # {"status": 429, "times": 2, "model": "", "path": ""}
    "log": [], "generations": {},
}
LOCK = threading.Lock()

MODELS = [
    {"id": "anthropic/claude-sonnet-4.5", "name": "Anthropic: Claude Sonnet 4.5", "context_length": 1000000,
     "pricing": {"prompt": "0.000003", "completion": "0.000015", "request": "0", "image": "0.0048", "input_cache_read": "0.0000003"},
     "supported_parameters": ["tools", "tool_choice", "reasoning", "include_reasoning", "max_tokens", "temperature", "structured_outputs", "response_format"],
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
    {"id": "openai/gpt-5.1-codex", "name": "OpenAI: GPT-5.1-Codex", "context_length": 400000,
     "pricing": {"prompt": "0.00000125", "completion": "0.00001", "request": "0", "image": "0"},
     "supported_parameters": ["tools", "tool_choice", "reasoning", "include_reasoning", "max_tokens", "structured_outputs", "response_format", "seed"],
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
    {"id": "qwen/qwen3-coder:free", "name": "Qwen: Qwen3 Coder (free)", "context_length": 262144,
     "pricing": {"prompt": "0", "completion": "0", "request": "0", "image": "0"},
     "supported_parameters": ["tools", "tool_choice", "max_tokens", "temperature"],
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    {"id": "meta-llama/llama-3.1-8b-instruct", "name": "Meta: Llama 3.1 8B", "context_length": 131072,
     "pricing": {"prompt": "0.00000002", "completion": "0.00000005", "request": "0", "image": "0"},
     "supported_parameters": ["max_tokens", "temperature"],
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
]


# ----------------------------------------------------------------------------- the scripted "model"
def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "input_text", "output_text"):
                    out.append(str(c.get("text") or ""))
                elif c.get("type") == "tool_result":
                    out.append(_text_of(c.get("content")))
            elif isinstance(c, str):
                out.append(c)
        return "\n".join(out)
    return ""


def _conversation(kind: str, body: dict) -> tuple[str, str, bool]:
    """(all user text joined, the latest user text, whether the newest item is a tool result)."""
    texts, last, tool_last = [], "", False
    if kind == "chat":
        for m in body.get("messages") or []:
            role = m.get("role")
            if role == "user":
                t = _text_of(m.get("content"))
                texts.append(t)
                last, tool_last = t, False
            elif role == "tool":
                tool_last = True
            elif role == "system":
                texts.append(_text_of(m.get("content")))
            elif role == "assistant":
                tool_last = False
    elif kind == "responses":
        inp = body.get("input")
        if isinstance(inp, str):
            inp = [{"type": "message", "role": "user", "content": inp}]
        texts.append(str(body.get("instructions") or ""))
        for it in inp or []:
            typ = it.get("type") or "message"
            if typ == "message" and it.get("role") == "user":
                t = _text_of(it.get("content"))
                texts.append(t)
                if "<environment_context>" not in t and "AGENTS.md" not in t[:200]:
                    last, tool_last = t, False
            elif typ in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                tool_last = True
            elif typ == "message" and it.get("role") == "assistant":
                tool_last = False
    else:  # anthropic
        sysp = body.get("system")
        texts.append(_text_of(sysp) if not isinstance(sysp, str) else sysp)
        for m in body.get("messages") or []:
            c = m.get("content")
            if m.get("role") == "user":
                blocks = c if isinstance(c, list) else [{"type": "text", "text": c}]
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                    tool_last = True
                    continue
                t = "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                texts.append(t)
                last, tool_last = t, False
            elif m.get("role") == "assistant":
                tool_last = False
    return "\n".join(texts), last, tool_last


def _role(all_text: str) -> str:
    if "You are the INDEPENDENT REVIEWER" in all_text:
        return "reviewer"
    if "You are the WORKER" in all_text:
        return "worker"
    if "You are the SUPERVISOR" in all_text:
        return "supervisor"
    return "plain"


def _fence(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def _final_text(role: str, last: str) -> str:
    if role == "supervisor":
        if "You are the SUPERVISOR" not in last:  # anything after the kickoff: a report, a review, a nudge
            return "The file is in place and matches the request.\n\n" + _fence({
                "type": "decision", "decision": "done", "summary": f"{FILE} created",
                "criteria": [{"id": "A1", "status": "met", "evidence": f"`test -f {FILE}` → exit 0; file:1 reads '# Hello from OpenRouter'"}],
                "follow_ups": [], "pr_summary": f"Adds {FILE}, written by an agent routed through OpenRouter."})
        return "Plan: one small work package.\n\n" + _fence({
            "type": "plan", "summary": f"Create {FILE}", "plan": f"1. Create {FILE} with a short greeting.",
            "requirements": [f"Create {FILE} at the repository root"],
            "acceptance": [{"id": "A1", "criterion": f"{FILE} exists at the repository root with a heading",
                            "how_to_verify": f"test: test -f {FILE}", "required": True}],
            "optional": [], "known_files": {"primary": [FILE], "supporting": []}, "findings": [], "constraints": [],
            "unknowns": [], "complexity": {"level": "simple", "reason": "one new file"},
            "instruction": f"Create {FILE} at the repository root containing a '# Hello from OpenRouter' heading and one sentence."})
    if role == "worker":
        return f"Created {FILE}.\n\n" + _fence({
            "type": "report", "status": "complete", "summary": f"Created {FILE}",
            "report": f"- Added {FILE} with a heading and one sentence.\n- Check: `test -f {FILE}` → exit 0 (A1).", "files": [FILE]})
    if role == "reviewer":
        return "The change matches the contract.\n\n" + _fence({"type": "review", "verdict": "PASS", "summary": f"{FILE} is correct", "findings": []})
    if re.search(r"\bpong\b", last, re.I):
        return "pong"
    return "OK"


def _shell_command() -> str:
    return f"printf '%s' {json.dumps(CONTENT)} > {FILE}"


def _pick_tool(tools: list[dict]) -> tuple[str, dict] | None:
    """A tool call that writes the file, in the shape this CLI's tool schema asks for."""
    named = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name") or t.get("name") or ""
        params = fn.get("parameters") or fn.get("input_schema") or t.get("input_schema") or {}
        props = (params.get("properties") or {}) if isinstance(params, dict) else {}
        named.append((name, props, t.get("type")))
    # A shell tool first: the command runs in the worktree, so a relative path is right for every CLI.
    for name, props, _ in named:
        low = name.lower()
        if re.search(r"bash|shell|exec|run_command|run_commands|terminal|command", low):
            for key in ("command", "cmd", "commands", "script"):
                if key in props:
                    typ = (props[key] or {}).get("type")
                    if typ == "array":
                        items = ((props[key] or {}).get("items") or {}).get("type")
                        val = ["bash", "-lc", _shell_command()] if items == "string" or key != "commands" else [_shell_command()]
                        if key == "commands":
                            val = [_shell_command()]
                        return name, {key: val}
                    return name, {key: _shell_command()}
    # Then a write tool; some CLIs insist on absolute paths there, which this server cannot know.
    for name, props, _ in named:
        low = name.lower()
        if re.search(r"^(write|write_file|write_to_file|create_file|file_write|str_replace_based_edit_tool)$", low) or low.endswith("__write"):
            path_key = next((k for k in ("file_path", "filePath", "path", "absolute_path", "filename", "target_file") if k in props), None)
            text_key = next((k for k in ("content", "contents", "text", "file_text", "file_content") if k in props), None)
            if path_key and text_key:
                args = {path_key: FILE, text_key: CONTENT}
                if "command" in props and low == "str_replace_based_edit_tool":
                    args["command"] = "create"
                return name, args
    return None


def brain(kind: str, body: dict) -> dict:
    """{"text": str} or {"tool": (name, args)} for this request."""
    all_text, last, tool_last = _conversation(kind, body)
    role = _role(all_text)
    tools = body.get("tools") or []
    if role == "worker" and not tool_last and not re.search(r"did not end with a valid JSON", last):
        pick = _pick_tool(tools)
        if pick:
            return {"tool": pick, "role": role}
        if not tools:  # aider: no tools, edits come as a whole-file block in the reply
            return {"text": f"{FILE}\n```\n{CONTENT}```\n\n" + _final_text(role, last), "role": role}
    return {"text": _final_text(role, last), "role": role}


# ----------------------------------------------------------------------------- server
def _gen_id() -> str:
    return "gen-" + uuid.uuid4().hex[:24]


def _usage_numbers(body: dict, out_text: str) -> tuple[int, int, float]:
    pin = max(1, len(json.dumps(body)) // 4)
    pout = max(1, len(out_text) // 4)
    model = str(body.get("model") or "")
    free = model.endswith(":free")
    cost = 0.0 if free else round(pin * PRICE_IN + pout * PRICE_OUT, 8)
    return pin, pout, cost


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockOpenRouter/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- helpers
    def _send(self, status: int, obj, headers: dict | None = None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str, metadata: dict | None = None, headers: dict | None = None):
        err = {"code": status, "message": message}
        if metadata:
            err["metadata"] = metadata
        self._send(status, {"error": err}, headers)

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, data: str):
        b = data.encode()
        self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
        self.wfile.flush()

    def _sse(self, obj, event: str | None = None):
        self._chunk((f"event: {event}\n" if event else "") + "data: " + (obj if isinstance(obj, str) else json.dumps(obj)) + "\n\n")

    def _stream_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _auth(self) -> str | None:
        h = self.headers.get("Authorization") or ""
        tok = h[7:].strip() if h.lower().startswith("bearer ") else (self.headers.get("x-api-key") or "").strip()
        return tok or None

    def _check_auth(self) -> bool:
        tok = self._auth()
        if not tok or (STATE["key"] and tok != STATE["key"]):
            self._error(401, "No auth credentials found" if not tok else "User not found.")
            return False
        return True

    def _record(self, path: str, body: dict | None):
        tok = self._auth() or ""
        entry = {"at": time.time(), "method": self.command, "path": path,
                 "auth": ("sha256:" + hashlib.sha256(tok.encode()).hexdigest()[:12]) if tok else "",
                 "auth_prefix": tok[:8], "referer": self.headers.get("HTTP-Referer") or self.headers.get("Referer") or "",
                 "title": self.headers.get("X-Title") or self.headers.get("X-OpenRouter-Title") or "",
                 "user_agent": self.headers.get("User-Agent") or ""}
        if isinstance(body, dict):
            entry.update(model=body.get("model"), stream=bool(body.get("stream")), provider=body.get("provider"),
                         models=body.get("models"), tools=len(body.get("tools") or []), usage_opt=body.get("usage"))
        with LOCK:
            STATE["log"].append(entry)
            del STATE["log"][:-500]
        return entry

    def _forced(self, path: str, model: str) -> bool:
        f = STATE["force"]
        if not f or int(f.get("times") or 0) <= 0:
            return False
        if f.get("model") and f["model"] != model:
            return False
        if f.get("path") and f["path"] not in path:
            return False
        with LOCK:
            f["times"] = int(f.get("times") or 0) - 1
        st = int(f.get("status") or 500)
        msg = {401: "User not found.", 402: "Insufficient credits. Add more using https://openrouter.ai/settings/credits",
               403: "Your input was flagged", 429: f"{model} is temporarily rate-limited upstream. Please retry shortly.",
               502: "Provider returned error", 503: "No endpoints found that can handle the requested parameters."}.get(st, "Error")
        msg = f.get("message") or msg
        meta = {"limit_source": "openrouter_credits"} if st == 402 else ({"provider_name": "MockProvider", "raw": "rate limited"} if st == 429 else None)
        self._error(st, msg, meta, {"Retry-After": "1"} if st == 429 else None)
        return True

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/__mock/log":
            with LOCK:
                return self._send(200, {"log": list(STATE["log"])})
        if path.endswith("/models") and "/api/v1" in path:
            return self._send(200, {"data": MODELS})
        if path.endswith("/key"):
            if not self._check_auth():
                return
            self._record(path, None)
            lim = STATE["limit"]
            return self._send(200, {"data": {"label": "sk-or-v1-mock...", "limit": lim, "limit_remaining": (lim - STATE["usage"]) if lim else None,
                                             "limit_reset": None, "usage": STATE["usage"], "usage_daily": 0.4, "usage_weekly": 1.0, "usage_monthly": STATE["usage"],
                                             "is_free_tier": STATE["is_free_tier"], "include_byok_in_limit": False,
                                             "free_model_daily_requests": {"used": 3, "limit": 1000, "remaining": 997}}})
        if path.endswith("/credits"):
            if not self._check_auth():
                return
            self._record(path, None)
            return self._send(200, {"data": {"total_credits": STATE["credits"], "total_usage": STATE["usage"]}})
        if path.endswith("/generation"):
            if not self._check_auth():
                return
            gid = (parse_qs(u.query).get("id") or [""])[0]
            g = STATE["generations"].get(gid)
            if not g:
                return self._error(404, "Generation not found")
            return self._send(200, {"data": g})
        return self._error(404, f"Not found: {path}")

    # ---- POST
    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raw = self._read_chunked()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if path == "/__mock/config":
            with LOCK:
                for k in ("key", "credits", "usage", "limit", "is_free_tier", "force"):
                    if k in body:
                        STATE[k] = body[k]
                if body.get("clear_log"):
                    STATE["log"].clear()
            return self._send(200, {"ok": True})
        self._record(path, body)
        if not self._check_auth():
            return
        model = str(body.get("model") or "")
        if self._forced(path, model):
            return
        if not model.endswith(":free") and STATE["credits"] - STATE["usage"] <= 0:
            return self._error(402, "Insufficient credits. Add more using https://openrouter.ai/settings/credits", {"limit_source": "openrouter_credits"})
        if path.endswith("/chat/completions"):
            return self._chat(body)
        if path.endswith("/responses"):
            return self._responses(body)
        if path.endswith("/messages/count_tokens"):
            return self._send(200, {"input_tokens": max(1, len(json.dumps(body)) // 4)})
        if path.endswith("/messages"):
            return self._messages(body)
        return self._error(404, f"Not found: {path}")

    def _read_chunked(self) -> bytes:
        out = b""
        while True:
            size = int(self.rfile.readline().strip() or b"0", 16)
            if not size:
                self.rfile.readline()
                return out
            out += self.rfile.read(size)
            self.rfile.readline()

    def _remember(self, gid, body, pin, pout, cost, native_model):
        with LOCK:
            STATE["usage"] = round(STATE["usage"] + cost, 8)
            STATE["generations"][gid] = {"id": gid, "model": native_model, "total_cost": cost, "tokens_prompt": pin,
                                         "tokens_completion": pout, "native_tokens_prompt": pin, "native_tokens_completion": pout,
                                         "provider_name": "MockProvider", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                         "streamed": bool(body.get("stream")), "cancelled": False, "finish_reason": "stop"}

    # chat completions ---------------------------------------------------------------------
    def _chat(self, body):
        gid, model = _gen_id(), str(body.get("model") or "")
        plan = brain("chat", body)
        text = plan.get("text") or ""
        call = plan.get("tool")
        pin, pout, cost = _usage_numbers(body, text + json.dumps(call or ""))
        self._remember(gid, body, pin, pout, cost, model)
        usage = {"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout, "cost": cost,
                 "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}}
        tc = None
        if call:
            tc = {"id": "call_" + uuid.uuid4().hex[:12], "type": "function", "function": {"name": call[0], "arguments": json.dumps(call[1])}}
        if not body.get("stream"):
            msg = {"role": "assistant", "content": text or None}
            if tc:
                msg["tool_calls"] = [tc]
            return self._send(200, {"id": gid, "object": "chat.completion", "created": int(time.time()), "model": model, "provider": "MockProvider",
                                    "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tc else "stop"}], "usage": usage})
        self._stream_start()
        base = {"id": gid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "provider": "MockProvider"}
        self._sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        if tc:
            self._sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, **tc}]}, "finish_reason": None}]})
        else:
            for i in range(0, len(text), 400):
                self._sse({**base, "choices": [{"index": 0, "delta": {"content": text[i:i + 400]}, "finish_reason": None}]})
        self._sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tc else "stop"}]})
        self._sse({**base, "choices": [], "usage": usage})
        self._sse("[DONE]")
        self._stream_end()

    # responses (Codex) ---------------------------------------------------------------------
    def _responses(self, body):
        gid, model = _gen_id(), str(body.get("model") or "")
        plan = brain("responses", body)
        text = plan.get("text") or ""
        call = plan.get("tool")
        pin, pout, cost = _usage_numbers(body, text + json.dumps(call or ""))
        self._remember(gid, body, pin, pout, cost, model)
        usage = {"input_tokens": pin, "output_tokens": pout, "total_tokens": pin + pout, "cost": cost,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
        if call:
            item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:12], "call_id": "call_" + uuid.uuid4().hex[:12],
                    "name": call[0], "arguments": json.dumps(call[1]), "status": "completed"}
        else:
            item = {"type": "message", "id": "msg_" + uuid.uuid4().hex[:12], "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
        resp = {"id": gid, "object": "response", "created_at": int(time.time()), "model": model, "status": "completed",
                "output": [item], "usage": usage}
        if not body.get("stream"):
            return self._send(200, resp)
        self._stream_start()
        seq = [0]

        def ev(typ, **kw):
            seq[0] += 1
            self._sse({"type": typ, "sequence_number": seq[0], **kw}, event=typ)
        ev("response.created", response={**resp, "status": "in_progress", "output": [], "usage": None})
        ev("response.in_progress", response={**resp, "status": "in_progress", "output": [], "usage": None})
        if call:
            ev("response.output_item.added", output_index=0, item={**item, "arguments": "", "status": "in_progress"})
            ev("response.function_call_arguments.delta", output_index=0, item_id=item["id"], delta=item["arguments"])
            ev("response.function_call_arguments.done", output_index=0, item_id=item["id"], arguments=item["arguments"])
            ev("response.output_item.done", output_index=0, item=item)
        else:
            ev("response.output_item.added", output_index=0, item={**item, "content": [], "status": "in_progress"})
            ev("response.content_part.added", output_index=0, item_id=item["id"], content_index=0, part={"type": "output_text", "text": "", "annotations": []})
            ev("response.output_text.delta", output_index=0, item_id=item["id"], content_index=0, delta=text)
            ev("response.output_text.done", output_index=0, item_id=item["id"], content_index=0, text=text)
            ev("response.content_part.done", output_index=0, item_id=item["id"], content_index=0, part=item["content"][0])
            ev("response.output_item.done", output_index=0, item=item)
        ev("response.completed", response=resp)
        self._stream_end()

    # anthropic messages (Claude Code) -------------------------------------------------------
    def _messages(self, body):
        gid, model = _gen_id(), str(body.get("model") or "")
        plan = brain("anthropic", body)
        text = plan.get("text") or ""
        call = plan.get("tool")
        pin, pout, cost = _usage_numbers(body, text + json.dumps(call or ""))
        self._remember(gid, body, pin, pout, cost, model)
        if call:
            block = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": call[0], "input": call[1]}
        else:
            block = {"type": "text", "text": text}
        stop = "tool_use" if call else "end_turn"
        if not body.get("stream"):
            return self._send(200, {"id": gid, "type": "message", "role": "assistant", "model": model, "content": [block],
                                    "stop_reason": stop, "stop_sequence": None,
                                    "usage": {"input_tokens": pin, "output_tokens": pout, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost": cost}})
        self._stream_start()
        self._sse({"type": "message_start", "message": {"id": gid, "type": "message", "role": "assistant", "model": model, "content": [],
                                                        "stop_reason": None, "stop_sequence": None,
                                                        "usage": {"input_tokens": pin, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
                  event="message_start")
        if call:
            self._sse({"type": "content_block_start", "index": 0, "content_block": {**block, "input": {}}}, event="content_block_start")
            self._sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(call[1])}}, event="content_block_delta")
        else:
            self._sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, event="content_block_start")
            self._sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}, event="content_block_delta")
        self._sse({"type": "content_block_stop", "index": 0}, event="content_block_stop")
        self._sse({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                   "usage": {"input_tokens": pin, "output_tokens": pout, "cost": cost}}, event="message_delta")
        self._sse({"type": "message_stop"}, event="message_stop")
        self._stream_end()


def serve(port: int, host: str = "0.0.0.0", key: str = "") -> ThreadingHTTPServer:
    STATE["key"] = key
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--key", default="", help="the only key accepted (default: any non-empty key)")
    a = ap.parse_args()
    print(f"mock OpenRouter on http://{a.host}:{a.port}/api/v1", flush=True)
    serve(a.port, a.host, a.key).serve_forever()
