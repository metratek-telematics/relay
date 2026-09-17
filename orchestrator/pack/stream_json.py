"""Agent pack: CLIs that speak (or nearly speak) Claude Code's stream-json.

  qwen    Qwen Code       `qwen -o stream-json -y`            Claude-compatible; `error` is an object, no cost
  amp     Amp             `amp --stream-json -x`              Claude-compatible; no usage on the result
  cursor  Cursor Agent    `cursor-agent -p --output-format stream-json`
                          Claude-like envelope, but tool calls are separate `tool_call` events and
                          failures are plain stderr text with exit 1 (no JSON result).

Every prompt goes in on stdin: prompts can be large or start with "-", which the CLIs' option
parsers would otherwise read as a flag.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..agents import AgentAdapter, ClaudeAdapter, TurnContext, _summarize_input, _tool_category, _try_json
from ..util import truncate, windows_cli

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Tool names these CLIs use that the shared _tool_category does not know.
_EXTRA_CATEGORIES = {
    "grep_search": "search", "glob": "search", "list_files": "search", "codebase_search": "search",
    "edit_file": "edit", "create_file": "edit", "undo_edit": "edit", "format_file": "edit", "delete": "edit",
    "oracle": "agent", "librarian": "agent", "web_fetch": "web", "read_web_page": "web",
    "todo_write": "plan", "todo_read": "plan", "update_todos": "plan", "create_plan": "plan", "read_todos": "plan",
    "sem_search": "search", "ls": "search", "read_lints": "read", "mcp": "mcp",
}


def _category(tool: str) -> str:
    cat = _tool_category(tool)
    if cat == "tool":
        cat = _EXTRA_CATEGORIES.get((tool or "").lower(), "tool")
    return cat


def _err_text(err) -> str:
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or json.dumps(err, ensure_ascii=False))
    return str(err or "")


class _StreamJsonAdapter(ClaudeAdapter):
    """Claude stream-json parsing, without Claude's environment, flags or install hint."""

    name = ""

    def env(self, cfg):  # ClaudeAdapter.env strips API keys and sets Claude-only variables
        return AgentAdapter.env(self, cfg)

    def install_hint(self):
        return AgentAdapter.install_hint(self)

    def _effort(self, effort: str) -> str:
        e = (effort or "").strip()
        return e if e and e in (self.spec.get("efforts") or []) else ""

    def parse_line(self, line, ctx):
        try:
            return self._parse(line, ctx)
        except Exception as e:  # a malformed line must never break the turn
            s = (line or "").strip()
            return [{"kind": "log", "text": truncate(f"{s}  [unparsed: {e}]", 2000)}] if s else []

    def _plain(self, line, ctx):
        """Non-JSON output (stderr is merged into stdout). Startup failures are only printed here."""
        s = _ANSI.sub("", line or "").strip()
        if not s:
            return []
        ctx.__dict__.setdefault("_plain_log", []).append(s)
        if re.match(r"^(error\b|⚠)", s, re.I) and not ctx.error:
            ctx.error = f"{self.label}: {truncate(s.lstrip('⚠ ').strip(), 400)}"
        return [{"kind": "log", "text": s}]

    def finalize(self, ctx, rc, run_dir, session):
        if rc not in (0, None) and not ctx.error:
            logs = ctx.__dict__.get("_plain_log") or []
            ctx.error = f"{self.label} exited with code {rc}" + (": " + truncate("\n".join(logs[-6:]), 600) if logs else "")
        return super().finalize(ctx, rc, run_dir, session)

    def _parse(self, line, ctx):
        obj = _try_json(line)
        if not isinstance(obj, dict):
            return self._plain(line, ctx)
        typ = obj.get("type")
        if typ == "result":
            return self._result(obj, ctx)
        if typ in ("system", "assistant", "user"):
            out = ClaudeAdapter.parse_line(self, json.dumps(obj), ctx)
            for ev in out:
                if ev.get("kind") == "tool_use":
                    ev["category"] = _category(ev.get("tool"))
            return out
        return []  # stream_event (partial deltas, goal_state), rate limits, hooks

    def _result(self, obj, ctx):
        ctx.session_id = obj.get("session_id") or ctx.session_id
        sub = str(obj.get("subtype") or "")
        failed = bool(obj.get("is_error")) or sub.startswith("error")
        ctx.result_ok = not failed
        if obj.get("result") and not failed:
            ctx.result_text = str(obj["result"])
        if obj.get("total_cost_usd") is not None:
            ctx.usage["cost_usd"] = float(obj.get("total_cost_usd") or 0)
        u = obj.get("usage") or {}
        if isinstance(u, dict) and u:
            ctx.usage["input"] = max(ctx.usage["input"], int(u.get("input_tokens") or 0))
            ctx.usage["output"] = max(ctx.usage["output"], int(u.get("output_tokens") or 0))
            ctx.usage["cached"] = max(ctx.usage["cached"], int(u.get("cache_read_input_tokens") or 0))
        out = []
        if failed:
            msg = _err_text(obj.get("error")) or str(obj.get("result") or "") or sub or "error"
            ctx.error = f"{self.label} ended with {sub or 'error'}: {truncate(msg, 400)}"
            out.append({"kind": "error", "text": msg})
        out.append({"kind": "usage", **ctx.usage})
        out.append({"kind": "result", "ok": ctx.result_ok, "text": ctx.result_text, "error": ctx.error,
                    "num_turns": obj.get("num_turns"), "duration_ms": obj.get("duration_ms")})
        return out


# ----------------------------------------------------------------------------- Qwen Code
class QwenAdapter(_StreamJsonAdapter):
    name = "qwen"

    _AUTH_BY_KEY = (("OPENAI_API_KEY", "openai"), ("ANTHROPIC_API_KEY", "anthropic"), ("GEMINI_API_KEY", "gemini"))

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("QWEN_CODE_SUPPRESS_YOLO_WARNING", "1")
        return env

    @staticmethod
    def _settings(env) -> dict:
        home = Path(env.get("QWEN_HOME") or (Path(env.get("HOME") or Path.home()) / ".qwen"))
        try:
            d = json.loads((home / "settings.json").read_text(encoding="utf-8", errors="replace"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _auth_type(self, env) -> str:
        s = self._settings(env)
        selected = ((s.get("security") or {}).get("auth") or {}).get("selectedType") or s.get("selectedAuthType")
        if selected or env.get("QWEN_DEFAULT_AUTH_TYPE"):
            return ""  # the user already chose one
        for key, kind in self._AUTH_BY_KEY:
            if env.get(key):
                return kind
        return ""

    def signed_in(self, env) -> bool:
        auth = self.spec.get("auth") or {}
        if any(env.get(k) for k in auth.get("env") or []):
            return True
        s = self._settings(env)
        selected = ((s.get("security") or {}).get("auth") or {}).get("selectedType") or s.get("selectedAuthType")
        # Qwen OAuth was discontinued, so a chosen API provider is the only working login.
        return bool(selected) and selected not in ("qwen-oauth",)

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        env = self.env(cfg)
        # Qwen has no effort flag (only the model.reasoningEffort setting), so `effort` is ignored.
        args = ["-o", "stream-json", "-y"]
        extra = list(cfg.get("qwen_extra_args") or [])
        auth_type = self._auth_type(env)
        if auth_type and "--auth-type" not in extra:
            args += ["--auth-type", auth_type]
        if model:
            args += ["-m", model]
        if session.get("id") and session.get("turns", 0) > 0:
            args += ["--resume", session["id"]]
        else:
            session["id"] = session.get("id") or self.new_session_id()
            args += ["--session-id", session["id"]]
        args += extra
        return windows_cli(self.binary, args), env, prompt, session


# ----------------------------------------------------------------------------- Amp
class AmpAdapter(_StreamJsonAdapter):
    name = "amp"

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("AMP_SKIP_UPDATE_CHECK", "1")
        return env

    def signed_in(self, env) -> bool:
        if env.get("AMP_API_KEY"):
            return True
        data = Path(env.get("XDG_DATA_HOME") or (Path(env.get("HOME") or Path.home()) / ".local" / "share"))
        try:
            return "apiKey" in (data / "amp" / "secrets.json").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        env = self.env(cfg)
        if not self.signed_in(env):
            # Without a key every amp command starts a device login and waits forever.
            raise RuntimeError("Amp is not signed in. Add AMP_API_KEY under Configure, "
                               f"or run `{self.spec.get('login') or 'amp login'}` in a terminal first.")
        # Amp has no model flag: its mode picks the model, so effort maps to --mode.
        common = ["--stream-json", "--dangerously-allow-all", "--no-notifications", "--no-color", "--no-ide",
                  "--no-archive-after-execute"]
        eff = self._effort(effort)
        if eff:
            common += ["-m", eff]
        common += list(cfg.get("amp_extra_args") or [])
        if session.get("id") and session.get("turns", 0) > 0:
            args = ["threads", "continue", session["id"], *common, "-x"]
        else:
            args = [*common, "-x"]  # bare -x: the prompt is read from stdin
        return windows_cli(self.binary, args), env, prompt, session


# ----------------------------------------------------------------------------- Cursor Agent
_CURSOR_TOOL_NAMES = {
    "shell": "Shell", "read": "Read", "edit": "Edit", "write": "Write", "delete": "Delete", "glob": "Glob",
    "grep": "Grep", "ls": "LS", "readLints": "read_lints", "mcp": "mcp", "semSearch": "sem_search",
    "webSearch": "WebSearch", "fetch": "WebFetch", "task": "Task", "updateTodos": "update_todos",
    "readTodos": "read_todos", "createPlan": "create_plan",
}


def _cursor_tool(tool_call) -> tuple[str, dict, dict | None]:
    """(tool name, args, result-or-None) from a proto-JSON ToolCall oneof like {"shellToolCall": {...}}."""
    if not isinstance(tool_call, dict):
        return "tool", {}, None
    key = next((k for k in tool_call if k.endswith("ToolCall") and isinstance(tool_call[k], dict)), None)
    if not key:
        key = next((k for k in tool_call if isinstance(tool_call[k], dict)), None)
    if not key:
        return "tool", {}, None
    body = tool_call[key]
    base = key[:-len("ToolCall")] if key.endswith("ToolCall") else key
    name = _CURSOR_TOOL_NAMES.get(base) or base
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    if base == "mcp":
        name = f"mcp.{args.get('providerIdentifier') or args.get('serverIdentifier') or 'mcp'}.{args.get('toolName') or args.get('name') or 'tool'}"
    res = body.get("result") if isinstance(body.get("result"), dict) else None
    return name, args, res


def _cursor_result(result: dict) -> tuple[bool, str]:
    case = next((k for k, v in result.items() if isinstance(v, dict)), None)
    if not case:
        return True, ""
    val = result[case]
    ok = case == "success"
    if "exitCode" in val and val.get("exitCode") not in (0, None):
        ok = False
    if val.get("isError"):
        ok = False
    parts = []
    if "stdout" in val or "stderr" in val:
        parts = [val.get("interleavedOutput") or "\n".join(x for x in (val.get("stdout"), val.get("stderr")) if x)]
        if case != "success" and val.get("exitCode") not in (None, 0):
            parts.append(f"exit code {val.get('exitCode')}")
    else:
        for k in ("content", "error", "errorMessage", "reason", "message", "diffString", "clientVisibleError"):
            if isinstance(val.get(k), str) and val[k]:
                parts.append(val[k])
                break
        if not parts and isinstance(val.get("files"), list):
            parts.append("\n".join(str(f) for f in val["files"]))
        if not parts and isinstance(val.get("content"), list):  # MCP content items
            parts.append("\n".join(str((c.get("text") or {}).get("text", "")) if isinstance(c, dict) else str(c)
                                   for c in val["content"]))
        if not parts and val:
            parts.append(json.dumps(val, ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    if not ok and case != "success" and not text:
        text = case
    return ok, text


class CursorAdapter(_StreamJsonAdapter):
    name = "cursor"

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("NO_OPEN_BROWSER", "1")
        return env

    def signed_in(self, env) -> bool:
        if env.get("CURSOR_API_KEY") or env.get("CURSOR_AUTH_TOKEN"):
            return True
        cfg_home = Path(env.get("XDG_CONFIG_HOME") or (Path(env.get("HOME") or Path.home()) / ".config"))
        try:
            d = json.loads((cfg_home / "cursor" / "auth.json").read_text(encoding="utf-8", errors="replace"))
            return isinstance(d, dict) and any(d.get(k) for k in ("accessToken", "refreshToken", "apiKey"))
        except (OSError, ValueError):
            return False

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        args = ["-p", "--output-format", "stream-json", "--force", "--trust", "--approve-mcps",
                "--workspace", str(cwd)]
        eff = self._effort(effort)
        mdl = (model or "").strip()
        if mdl and eff:
            if mdl.endswith("]") and "[" in mdl:
                head, params = mdl[:-1].split("[", 1)
                kept = [p for p in params.split(",") if p.strip() and not p.strip().startswith("effort=")]
                mdl = f"{head}[{','.join(kept + [f'effort={eff}'])}]"
            else:
                mdl = f"{mdl}[effort={eff}]"
        if mdl:
            args += ["--model", mdl]
        if session.get("id") and session.get("turns", 0) > 0:
            args += ["--resume", session["id"]]
        else:
            session["id"] = session.get("id") or self.new_session_id()
            args += ["--new-session-id", session["id"]]
        args += list(cfg.get("cursor_extra_args") or [])
        # No positional prompt: print mode reads the prompt from stdin when stdin is not a TTY.
        return windows_cli(self.binary, args), self.env(cfg), prompt, session

    def _parse(self, line, ctx):
        obj = _try_json(line)
        if not isinstance(obj, dict):
            return self._plain(line, ctx)
        typ = obj.get("type")
        sub = obj.get("subtype")
        if typ == "system":
            if sub == "init":
                ctx.session_id = obj.get("session_id") or ctx.session_id
                ctx.model = obj.get("model") or ctx.model
                return [{"kind": "session", "session_id": ctx.session_id, "model": ctx.model}]
            return []
        if typ == "assistant":
            out = []
            for block in (obj.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip():
                    ctx.texts.append(block["text"])
                    out.append({"kind": "text", "text": block["text"]})
            return out
        if typ == "thinking":
            if sub == "delta":
                ctx.__dict__["_cursor_think"] = ctx.__dict__.get("_cursor_think", "") + str(obj.get("text") or "")
            elif sub == "completed":
                t = ctx.__dict__.pop("_cursor_think", "")
                if t.strip():
                    return [{"kind": "thinking", "text": t}]
            return []
        if typ == "tool_call":
            tid = obj.get("call_id") or f"cc_{len(ctx.tools)}"
            name, args, res = _cursor_tool(obj.get("tool_call"))
            out = []
            if tid not in ctx.tools:
                ctx.tools[tid] = {"tool": name, "at": time.time()}
                ctx.tool_calls += 1
                out.append({"kind": "tool_use", "id": tid, "tool": name, "category": _category(name),
                            "input": args, "summary": _summarize_input(name, args)})
            if sub == "completed":
                ok, text = _cursor_result(res) if res else (True, "")
                out.append({"kind": "tool_result", "id": tid, "ok": ok, "output": truncate(text, 12000),
                            "duration": time.time() - ctx.tools[tid]["at"]})
            return out
        if typ == "result":
            ctx.session_id = obj.get("session_id") or ctx.session_id
            failed = bool(obj.get("is_error")) or str(sub or "").startswith("error")
            ctx.result_ok = not failed
            if not failed and (ctx.texts or obj.get("result")):
                # `result` glues the text segments together with no separator; keep them apart.
                ctx.result_text = "\n\n".join(t.strip() for t in ctx.texts if t.strip()) or str(obj["result"])
            u = obj.get("usage") or {}
            if isinstance(u, dict) and u:
                ctx.usage["input"] = int(u.get("inputTokens") or 0)
                ctx.usage["output"] = int(u.get("outputTokens") or 0)
                ctx.usage["cached"] = int(u.get("cacheReadTokens") or 0)
            out = []
            if failed:
                msg = _err_text(obj.get("error")) or str(obj.get("result") or "") or str(sub or "error")
                ctx.error = f"{self.label} ended with {sub or 'error'}: {truncate(msg, 400)}"
                out.append({"kind": "error", "text": msg})
            out.append({"kind": "usage", **ctx.usage})
            out.append({"kind": "result", "ok": ctx.result_ok, "text": ctx.result_text, "error": ctx.error,
                        "duration_ms": obj.get("duration_ms")})
            return out
        if typ in ("retry", "connection"):
            detail = obj.get("message") or obj.get("error") or obj.get("reason") or ""
            return [{"kind": "log", "text": f"cursor {typ} {sub or ''} {_err_text(detail)}".strip()}]
        return []  # user echo, interaction_query, task notifications


ADAPTERS = {
    "qwen": QwenAdapter(),
    "amp": AmpAdapter(),
    "cursor": CursorAdapter(),
}
