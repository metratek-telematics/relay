"""Agent adapters.

Each adapter knows how to:
  * check that its CLI is installed and usable,
  * build the command for a turn (fresh session or resumed session),
  * parse its structured output stream into normalized events,
  * summarize the turn (final text, session id, usage, cost).

Normalized events emitted by parse_line():
  {"kind":"session","session_id":..., "model":...}
  {"kind":"text","text":...}                      complete text block
  {"kind":"text_delta","text":...}                partial text (Gemini)
  {"kind":"thinking","text":...}
  {"kind":"tool_use","id":..., "tool":..., "input":{...}, "summary":...}
  {"kind":"tool_result","id":..., "ok":bool, "output":..., "summary":...}
  {"kind":"usage", "input":n, "output":n, "cached":n, "cost_usd":x}
  {"kind":"result","ok":bool,"text":...,"error":...}
  {"kind":"log","text":...}                       non-JSON noise (stderr etc.)
  {"kind":"error","text":...}
"""
from __future__ import annotations

import json
import os
import re

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as C
from .util import quiet, truncate, which, windows_cli

_health_cache: dict = {"at": 0, "data": None}


# ----------------------------------------------------------------------------- helpers
def _try_json(line: str):
    s = line.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _summarize_input(tool: str, inp) -> str:
    """One-line human summary of a tool call."""
    if not isinstance(inp, dict):
        return truncate(str(inp or ""), 160)
    t = (tool or "").lower()
    for key in ("command", "cmd"):
        if key in inp and isinstance(inp[key], (str, list)):
            c = pretty_command(inp[key])
            return truncate(c.splitlines()[0] if c else "", 180)
    for key in ("file_path", "path", "notebook_path", "absolute_path", "file"):
        if key in inp and isinstance(inp[key], str):
            extra = ""
            if "pattern" in inp:
                extra = f"  /{inp['pattern']}/"
            return truncate(inp[key] + extra, 180)
    if "pattern" in inp:
        return truncate(f"{inp.get('pattern')}  in {inp.get('path') or inp.get('glob') or '.'}", 180)
    if "query" in inp:
        return truncate(str(inp["query"]), 180)
    if "url" in inp:
        return truncate(str(inp["url"]), 180)
    if "prompt" in inp and t in ("task", "agent"):
        return truncate(str(inp.get("description") or inp["prompt"]), 180)
    if "description" in inp:
        return truncate(str(inp["description"]), 180)
    try:
        return truncate(json.dumps(inp, ensure_ascii=False), 180)
    except Exception:
        return truncate(str(inp), 180)


def _tool_category(tool: str) -> str:
    t = (tool or "").lower()
    if t in ("bash", "powershell", "shell", "run_shell_command", "command_execution", "execute", "exec"):
        return "shell"
    if t in ("read", "read_file", "read_many_files", "notebookread", "cat"):
        return "read"
    if t in ("edit", "write", "multiedit", "notebookedit", "write_file", "replace", "file_change", "apply_patch", "create_file"):
        return "edit"
    if t in ("grep", "glob", "search_file_content", "glob_files", "list_directory", "ls", "find"):
        return "search"
    if t in ("webfetch", "websearch", "web_fetch", "google_web_search", "web_search"):
        return "web"
    if t in ("task", "agent", "sub_agent"):
        return "agent"
    if t in ("todowrite", "todo_list", "write_todos"):
        return "plan"
    if t.startswith("mcp"):
        return "mcp"
    return "tool"



_SHELL_WRAPPERS = [
    re.compile(r'^"?(?:[A-Za-z]:\\[^"]*?[\\/])?powershell\.exe"?\s+(?:-NoProfile\s+)?(?:-ExecutionPolicy\s+\S+\s+)?-Command\s+', re.I),
    re.compile(r'^(?:powershell|pwsh)(?:\.exe)?\s+(?:-NoProfile\s+)?-Command\s+', re.I),
    re.compile(r'^(?:cmd(?:\.exe)?)\s+/[dD]?\s*/[cC]\s+', re.I),
    re.compile(r"^(?:/usr/bin/)?(?:bash|sh|zsh)\s+-l?c\s+", re.I),
]


def pretty_command(cmd) -> str:
    """Strip shell-wrapper prefixes and outer quotes so the UI shows the real command."""
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(x) for x in cmd)
    s = str(cmd or "").strip()
    for rx in _SHELL_WRAPPERS:
        s2 = rx.sub("", s)
        if s2 != s:
            s = s2.strip()
            break
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.strip()


def local_defaults(name: str) -> dict:
    """Best-effort read of the CLI's own configured default model / effort."""
    home = Path.home()
    out = {"default_model": None, "default_effort": None}
    try:
        if name == "codex":
            p = home / ".codex" / "config.toml"
            if p.exists():
                txt = p.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'^\s*model\s*=\s*"([^"]+)"', txt, re.M)
                e = re.search(r'^\s*model_reasoning_effort\s*=\s*"([^"]+)"', txt, re.M)
                out["default_model"] = m.group(1) if m else None
                out["default_effort"] = e.group(1) if e else None
        elif name == "claude":
            p = home / ".claude" / "settings.json"
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                out["default_model"] = d.get("model") or (d.get("env") or {}).get("ANTHROPIC_MODEL")
                out["default_effort"] = d.get("effortLevel")
        elif name == "gemini":
            p = home / ".gemini" / "settings.json"
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                mdl = d.get("model")
                out["default_model"] = (mdl.get("name") if isinstance(mdl, dict) else mdl) or (d.get("general") or {}).get("model")
    except Exception:
        pass
    return out


class TurnContext:
    """Accumulates state while one agent turn streams."""

    def __init__(self):
        self.session_id = None
        self.model = None
        self.texts: list[str] = []
        self.current_text = ""          # for delta-based adapters
        self.tools: dict = {}
        self.usage = {"input": 0, "output": 0, "cached": 0, "cost_usd": 0.0}
        self.result_text = None
        self.result_ok = None
        self.error = None
        self.started = time.time()
        self.tool_calls = 0


class AgentAdapter:
    name = ""
    supports_resume = True

    @property
    def label(self):
        return C.AGENTS[self.name]["label"]

    @property
    def binary(self):
        return C.AGENTS[self.name]["binary"]

    # ---- health ---------------------------------------------------------
    def health(self, cfg) -> dict:
        path = which(self.binary)
        info = {"agent": self.name, "label": self.label, "installed": bool(path), "path": path,
                "version": None, "ok": False, "error": None, "hint": None}
        if not path:
            info["error"] = f"{self.binary} CLI not found in PATH"
            info["hint"] = self.install_hint()
            return info
        try:
            p = quiet(windows_cli(self.binary, list(self.version_args)), timeout=40, env=self.env(cfg))
            out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
            info["version"] = next((l.strip() for l in out if re.search(r"\d+\.\d+", l)), (out[0] if out else "")).strip()
            info["ok"] = p.returncode == 0
            if p.returncode != 0:
                info["error"] = truncate(" ".join(out), 300)
        except Exception as e:
            info["error"] = str(e)
        info.update(local_defaults(self.name))
        info["efforts"] = list(C.AGENTS[self.name].get("efforts") or [])
        self.extra_health(info, cfg)
        return info

    version_args = ["--version"]

    @property
    def spec(self) -> dict:
        return C.AGENTS[self.name]

    def extra_health(self, info, cfg):
        """Pack agents: `--version` works without any login, so look for the credentials too."""
        auth = self.spec.get("auth")
        if not auth or not info["ok"]:
            return
        signed = self.signed_in(self.env(cfg))
        info["signed_in"] = signed
        if not signed:
            info["ok"] = False
            info["error"] = "Not signed in"
            info["hint"] = f"Run `{self.spec.get('login')}` in a terminal, or add an API key under Configure."

    def signed_in(self, env) -> bool:
        auth = self.spec.get("auth") or {}
        if any(env.get(k) for k in auth.get("env") or []):
            return True
        home = Path(env.get("HOME") or Path.home())
        return any((home / f).is_file() for f in auth.get("files") or [])

    def install_hint(self) -> str:
        inst = self.spec.get("install") or {}
        if inst:
            return f"Install {self.label} from the Agents page, then run `{self.spec.get('login')}` once to sign in."
        return ""

    # ---- environment ----------------------------------------------------
    def env(self, cfg) -> dict:
        env = os.environ.copy()
        for k, v in (cfg.get("agent_env", {}).get(self.name) or {}).items():
            if v is None or v == "":
                env.pop(k, None)
            else:
                env[str(k)] = str(v)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["NO_COLOR"] = "1"
        env["FORCE_COLOR"] = "0"
        env["CI"] = env.get("CI", "1")
        return env

    # ---- turn -----------------------------------------------------------
    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def build(self, prompt: str, cwd: Path, cfg: dict, model: str, session: dict | None, run_dir: Path, role: str, effort: str = ""):
        """Return (argv, env, stdin_text, session_dict)."""
        raise NotImplementedError

    def parse_line(self, line: str, ctx: TurnContext) -> list[dict]:
        raise NotImplementedError

    def finalize(self, ctx: TurnContext, rc: int, run_dir: Path, session: dict) -> dict:
        text = ctx.result_text
        if not text:
            text = "\n\n".join(t for t in ctx.texts if t and t.strip()).strip()
        if ctx.current_text.strip() and (not text or ctx.current_text.strip() not in text):
            text = (text + "\n\n" + ctx.current_text).strip() if text else ctx.current_text.strip()
        ok = (rc == 0) and (ctx.result_ok is not False) and not ctx.error
        return {
            "ok": ok, "text": text or "", "error": ctx.error if not ok else None,
            "session_id": ctx.session_id or session.get("id"), "model": ctx.model,
            "usage": ctx.usage, "duration": time.time() - ctx.started, "tool_calls": ctx.tool_calls,
        }


# ----------------------------------------------------------------------------- Claude
class ClaudeAdapter(AgentAdapter):
    name = "claude"

    def install_hint(self):
        return "Install Claude Code: npm install -g @anthropic-ai/claude-code, then run `claude` once to log in."

    def env(self, cfg):
        env = super().env(cfg)
        if cfg.get("prefer_claude_subscription_auth", True):
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDECODE", None)  # allow nesting when launched from inside Claude Code
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        if cfg.get("token_lean_agent_context", True):
            env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        cap = int(cfg.get("token_claude_max_output_tokens") or 0)
        if cap > 0:  # per-response output cap (Claude Code's own setting)
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(cap)
        if cfg.get("subagent_cheap_enabled", True):
            sub = (cfg.get("subagent_models", {}) or {}).get("claude", "").strip()
            if sub:
                env["CLAUDE_CODE_SUBAGENT_MODEL"] = sub
        return env

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        args = ["-p", "--output-format", "stream-json", "--verbose"]
        if effort:
            args += ["--effort", effort]
        if cfg.get("claude_permission", "bypass") == "acceptEdits":
            args += ["--permission-mode", "acceptEdits"]
        else:
            args += ["--dangerously-skip-permissions"]
        mt = int(cfg.get("claude_max_turns_per_call") or 0)
        if mt > 0:
            args += ["--max-turns", str(mt)]
        if model:
            args += ["--model", model]
        if session.get("id") and session.get("turns", 0) > 0:
            args += ["--resume", session["id"]]
        else:
            session["id"] = session.get("id") or self.new_session_id()
            args += ["--session-id", session["id"]]
            args += ["--name", f"Relay {role} {session['id'][:8]}"]
        args.append("--forward-subagent-text")
        for d in cfg.get("extra_dirs") or []:  # a multi-repository task's other worktrees
            args += ["--add-dir", str(d)]
        srcs = (cfg.get("claude_setting_sources") or "").strip()
        if cfg.get("token_lean_agent_context", True):
            # The user's personal plugins, skills, slash commands and MCP connectors (claude.ai, user settings) add
            # thousands of tokens to every API call and nothing to a coding turn; the task's own tools come from Relay.
            srcs = srcs or "project,local"
            args += ["--strict-mcp-config", "--disable-slash-commands"]
        if srcs:
            args += ["--setting-sources", srcs]
        args += list(cfg.get("claude_extra_args") or [])
        return windows_cli("claude", args), self.env(cfg), prompt, session

    def parse_line(self, line, ctx):
        obj = _try_json(line)
        if obj is None:
            return [{"kind": "log", "text": line}] if line.strip() else []
        typ = obj.get("type")
        out = []
        if typ == "system":
            if obj.get("subtype") == "init":
                ctx.session_id = obj.get("session_id") or ctx.session_id
                ctx.model = obj.get("model") or ctx.model
                out.append({"kind": "session", "session_id": ctx.session_id, "model": ctx.model,
                            "tools": obj.get("tools", [])})
            return out
        if typ == "assistant":
            msg = obj.get("message") or {}
            ctx.model = msg.get("model") or ctx.model
            for block in msg.get("content") or []:
                bt = block.get("type")
                if bt == "text" and block.get("text", "").strip():
                    ctx.texts.append(block["text"])
                    out.append({"kind": "text", "text": block["text"]})
                elif bt == "thinking" and block.get("thinking"):
                    out.append({"kind": "thinking", "text": block["thinking"]})
                elif bt == "tool_use":
                    tid = block.get("id") or f"tu_{len(ctx.tools)}"
                    ctx.tools[tid] = {"tool": block.get("name"), "at": time.time()}
                    ctx.tool_calls += 1
                    out.append({"kind": "tool_use", "id": tid, "tool": block.get("name"),
                                "category": _tool_category(block.get("name")),
                                "input": block.get("input"), "summary": _summarize_input(block.get("name"), block.get("input"))})
            u = msg.get("usage") or {}
            if u:
                # One API call can arrive as several assistant events carrying the same usage: count each message id once.
                mid = msg.get("id") or f"m{len(ctx.texts)}:{ctx.tool_calls}"
                seen = ctx.__dict__.setdefault("_usage_ids", set())
                if mid not in seen:
                    seen.add(mid)
                    ctx.usage["input"] += int(u.get("input_tokens") or 0)
                    ctx.usage["output"] += int(u.get("output_tokens") or 0)
                    ctx.usage["cached"] += int(u.get("cache_read_input_tokens") or 0)
                    ctx.usage["cache_write"] = ctx.usage.get("cache_write", 0) + int(u.get("cache_creation_input_tokens") or 0)
                # The session's context size is what the latest call sent (for compaction decisions).
                ctx.usage["context"] = (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                                        + int(u.get("cache_creation_input_tokens") or 0)) or ctx.usage.get("context", 0)
            return out
        if typ == "user":
            msg = obj.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        c = block.get("content")
                        if isinstance(c, list):
                            c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                        started = ctx.tools.get(tid, {}).get("at")
                        out.append({"kind": "tool_result", "id": tid, "ok": not block.get("is_error"),
                                    "output": truncate(str(c or ""), 12000),
                                    "duration": (time.time() - started) if started else None})
            return out
        if typ == "result":
            ctx.session_id = obj.get("session_id") or ctx.session_id
            ctx.result_ok = not obj.get("is_error")
            if obj.get("result"):
                ctx.result_text = str(obj.get("result"))
            if obj.get("total_cost_usd") is not None:
                ctx.usage["cost_usd"] = float(obj.get("total_cost_usd") or 0)
            u = obj.get("usage") or {}
            if u:
                # The result's usage covers the whole turn and is authoritative.
                ctx.usage["input"] = int(u.get("input_tokens") or 0) or ctx.usage["input"]
                ctx.usage["output"] = int(u.get("output_tokens") or 0) or ctx.usage["output"]
                ctx.usage["cached"] = int(u.get("cache_read_input_tokens") or 0) or ctx.usage["cached"]
                ctx.usage["cache_write"] = int(u.get("cache_creation_input_tokens") or 0) or ctx.usage.get("cache_write", 0)
            sub = obj.get("subtype") or ""
            if obj.get("is_error") or sub.startswith("error"):
                ctx.error = f"Claude ended with {sub or 'error'}: {truncate(str(obj.get('result') or obj.get('error') or ''), 400)}"
                if sub == "error_max_turns":
                    ctx.error = None  # partial but usable; the pipeline will nudge for an envelope
                    ctx.result_ok = True
            out.append({"kind": "usage", **ctx.usage})
            out.append({"kind": "result", "ok": ctx.result_ok, "text": ctx.result_text, "error": ctx.error,
                        "num_turns": obj.get("num_turns"), "duration_ms": obj.get("duration_ms")})
            return out
        # rate_limit_event, stream_event, hooks: ignore
        return out


# ----------------------------------------------------------------------------- Codex
class CodexAdapter(AgentAdapter):
    name = "codex"

    def install_hint(self):
        return "Install Codex CLI: npm install -g @openai/codex, then run `codex` once to sign in."

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        last_file = Path(run_dir) / f"codex_{role}_last.txt"
        try:
            last_file.unlink(missing_ok=True)
        except Exception:
            pass
        common = ["--json", "-o", str(last_file)]
        # `codex exec resume` does not accept -s/--sandbox, so use config overrides for both paths.
        if cfg.get("codex_sandbox", "workspace-write") == "danger-full-access":
            common.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            common += ["-c", "sandbox_mode=\"workspace-write\"", "-c", "approval_policy=\"never\""]
            if cfg.get("extra_dirs"):  # a multi-repository task's other worktrees are writable too
                common += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(d) for d in cfg["extra_dirs"]])]
        if model:
            common += ["-m", model]
        eff = (effort or cfg.get("codex_reasoning_effort") or "").strip()
        if eff:
            common += ["-c", f"model_reasoning_effort=\"{eff}\""]
        verbosity = (cfg.get("token_codex_verbosity") or "").strip()
        if verbosity in ("low", "medium", "high"):  # GPT-5 family text verbosity; Codex ignores it for models without it
            common += ["-c", f"model_verbosity=\"{verbosity}\""]
        # No subagent-model override for Codex. Its `agents.<role>` config table
        # defines whole custom agent roles (a description is mandatory), not a
        # model for built-in helpers, so passing a model there only produced
        # "malformed agent role" errors and was ignored.
        common += list(cfg.get("codex_extra_args") or [])
        # Screenshots for a vision turn (the design focus group): `--image=<file>` attaches each one to this prompt.
        images = [f"--image={p}" for p in cfg.get("attach_images") or [] if Path(p).is_file()]
        if session.get("id") and session.get("turns", 0) > 0:
            args = ["exec", "resume", session["id"], *images, *common, "-"]
        else:
            args = ["exec", *images, *common, "-"]
        session["_last_file"] = str(last_file)
        if cfg.get("agent_web_search", True):
            args = ["--search", *args]  # global flag: live web search for research and docs, no per-call approval
        return windows_cli("codex", args), self.env(cfg), prompt, session

    def _item_events(self, item: dict, phase: str, ctx: TurnContext) -> list[dict]:
        it = item.get("type")
        iid = item.get("id") or f"it_{len(ctx.tools)}"
        out = []
        if it == "agent_message":
            if phase == "completed" and item.get("text", "").strip():
                ctx.texts.append(item["text"])
                out.append({"kind": "text", "text": item["text"]})
        elif it == "reasoning":
            if phase == "completed" and (item.get("text") or item.get("summary")):
                out.append({"kind": "thinking", "text": item.get("text") or str(item.get("summary"))})
        elif it == "command_execution":
            cmd = pretty_command(item.get("command") or "")
            if iid not in ctx.tools:
                ctx.tools[iid] = {"tool": "command_execution", "at": time.time()}
                ctx.tool_calls += 1
                out.append({"kind": "tool_use", "id": iid, "tool": "Shell", "category": "shell",
                            "input": {"command": cmd}, "summary": truncate(cmd, 180)})
            if phase == "completed":
                rc = item.get("exit_code")
                out.append({"kind": "tool_result", "id": iid, "ok": (rc in (0, None)) and item.get("status") != "failed",
                            "output": truncate(item.get("aggregated_output") or "", 12000),
                            "duration": time.time() - ctx.tools[iid]["at"]})
        elif it == "file_change":
            changes = item.get("changes") or []
            summary = ", ".join(f"{c.get('kind','update')} {c.get('path','')}" for c in changes[:6])
            if len(changes) > 6:
                summary += f" (+{len(changes)-6} more)"
            if iid not in ctx.tools:
                ctx.tools[iid] = {"tool": "file_change", "at": time.time()}
                ctx.tool_calls += 1
                out.append({"kind": "tool_use", "id": iid, "tool": "Edit", "category": "edit",
                            "input": {"changes": changes}, "summary": truncate(summary, 180)})
            if phase == "completed":
                out.append({"kind": "tool_result", "id": iid, "ok": item.get("status") != "failed",
                            "output": "\n".join(f"{c.get('kind','update')}: {c.get('path','')}" for c in changes),
                            "duration": time.time() - ctx.tools[iid]["at"]})
        elif it == "mcp_tool_call":
            name = f"{item.get('server','mcp')}.{item.get('tool','tool')}"
            if iid not in ctx.tools:
                ctx.tools[iid] = {"tool": name, "at": time.time()}
                ctx.tool_calls += 1
                out.append({"kind": "tool_use", "id": iid, "tool": name, "category": "mcp",
                            "input": item.get("arguments"), "summary": _summarize_input(name, item.get("arguments"))})
            if phase == "completed":
                res = item.get("result") if item.get("result") is not None else item.get("error")
                out.append({"kind": "tool_result", "id": iid, "ok": item.get("status") != "failed" and not item.get("error"),
                            "output": truncate(json.dumps(res, ensure_ascii=False) if not isinstance(res, str) else res, 12000),
                            "duration": time.time() - ctx.tools[iid]["at"]})
        elif it == "web_search":
            if phase == "completed":
                out.append({"kind": "tool_use", "id": iid, "tool": "WebSearch", "category": "web",
                            "input": {"query": item.get("query")}, "summary": truncate(item.get("query") or "", 180)})
                out.append({"kind": "tool_result", "id": iid, "ok": True, "output": "", "duration": 0})
        elif it == "todo_list":
            if phase == "completed":
                items = item.get("items") or []
                txt = "\n".join(f"[{'x' if x.get('completed') else ' '}] {x.get('text','')}" for x in items)
                out.append({"kind": "tool_use", "id": iid, "tool": "Plan", "category": "plan",
                            "input": {"items": items}, "summary": f"{len(items)} step plan"})
                out.append({"kind": "tool_result", "id": iid, "ok": True, "output": txt, "duration": 0})
        elif it == "error":
            if phase != "completed" or iid in ctx.tools:
                return out
            ctx.tools[iid] = {"tool": "error", "at": time.time()}
            msg = item.get("message") or json.dumps(item)
            if msg.lower().startswith(("ignoring", "warning", "model metadata for")):
                # "Model metadata for `x` not found": a model Codex has no local table for (any OpenRouter model) runs fine.
                # Non-fatal configuration warnings: the turn carries on, so show them
                # once in the turn's notice rather than as crash-style error cards.
                out.append({"kind": "log", "text": msg})
            else:
                out.append({"kind": "error", "text": msg})
        return out

    def parse_line(self, line, ctx):
        obj = _try_json(line)
        if obj is None:
            s = line.strip()
            if not s:
                return []
            # codex logs skill-loading errors and similar noise on stderr
            return [{"kind": "log", "text": s}]
        typ = obj.get("type") or ""
        if typ == "thread.started":
            ctx.session_id = obj.get("thread_id") or ctx.session_id
            return [{"kind": "session", "session_id": ctx.session_id, "model": ctx.model}]
        if typ.startswith("item."):
            phase = typ.split(".", 1)[1]
            return self._item_events(obj.get("item") or {}, phase, ctx)
        if typ == "turn.completed":
            u = obj.get("usage") or {}
            ctx.usage["input"] += int(u.get("input_tokens") or 0)
            ctx.usage["output"] += int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
            ctx.usage["cached"] += int(u.get("cached_input_tokens") or 0)
            ctx.result_ok = True
            return [{"kind": "usage", **ctx.usage}, {"kind": "result", "ok": True, "text": None, "error": None}]
        if typ == "turn.failed":
            err = obj.get("error") or {}
            ctx.error = "Codex turn failed: " + truncate(err.get("message") if isinstance(err, dict) else str(err), 400)
            ctx.result_ok = False
            return [{"kind": "error", "text": ctx.error}, {"kind": "result", "ok": False, "text": None, "error": ctx.error}]
        if typ == "error":
            msg = obj.get("message") or json.dumps(obj)
            ctx.error = "Codex error: " + truncate(msg, 400)
            return [{"kind": "error", "text": msg}]
        return []

    def finalize(self, ctx, rc, run_dir, session):
        res = super().finalize(ctx, rc, run_dir, session)
        lf = session.get("_last_file")
        if lf and Path(lf).exists():
            try:
                last = Path(lf).read_text(encoding="utf-8", errors="replace").strip()
                if last and (not res["text"] or len(last) >= len(ctx.texts[-1]) if ctx.texts else True):
                    # prefer the authoritative last message file for envelope parsing
                    res["last_message"] = last
            except Exception:
                pass
        return res


# ----------------------------------------------------------------------------- Gemini
class GeminiAdapter(AgentAdapter):
    name = "gemini"
    supports_resume = True  # via --resume latest (per-project session list)

    def install_hint(self):
        return "Install Gemini CLI: npm install -g @google/gemini-cli, then run `gemini` once to authenticate."

    def extra_health(self, info, cfg):
        env = self.env(cfg)
        if info["ok"] and not self.signed_in(env):
            # `gemini --version` succeeds without any login, and Docker creates an empty
            # ~/.gemini for a missing mount, so neither proves the CLI can run a turn.
            info["ok"] = False
            info["signed_in"] = False
            info["error"] = "Not signed in"
            info["hint"] = ("Run `gemini` once and choose Login with Google, or set GEMINI_API_KEY "
                            "under Settings → Agents → Gemini environment.")
            return
        info["signed_in"] = True
        if info["ok"] and not (env.get("GOOGLE_CLOUD_PROJECT") or env.get("GOOGLE_CLOUD_PROJECT_ID") or env.get("GEMINI_API_KEY")):
            info["hint"] = ("If Gemini reports that your account needs GOOGLE_CLOUD_PROJECT, set it under "
                            "Settings → Agents → Gemini environment (or set GEMINI_API_KEY).")

    @staticmethod
    def signed_in(env) -> bool:
        """True when Gemini CLI has credentials it can use without asking."""
        if any(env.get(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")):
            return True
        home = Path(env.get("HOME") or Path.home()) / ".gemini"
        # oauth_creds.json holds a Google login; the CLI also loads keys from ~/.gemini/.env.
        if (home / "oauth_creds.json").is_file():
            return True
        try:
            dotenv = (home / ".env").read_text(encoding="utf-8", errors="replace")
            if re.search(r"^\s*(?:export\s+)?(?:GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*\S", dotenv, re.M):
                return True
        except OSError:
            pass
        try:
            d = json.loads((home / "settings.json").read_text(encoding="utf-8", errors="replace"))
            auth = ((d.get("security") or {}).get("auth") or {}).get("selectedType") or d.get("selectedAuthType") or ""
        except (OSError, ValueError, AttributeError):
            return False
        # Vertex AI and Cloud Shell use ambient credentials once chosen in settings.json.
        if auth in ("vertex-ai", "cloud-shell", "compute-default-credentials"):
            return True
        # Newer builds keep an API key or a Google login in their own encrypted file instead of
        # oauth_creds.json or the environment. It is keyed to the hostname, so it only counts as
        # signed in when it was written on this machine; the smoke test proves it either way.
        if auth in ("gemini-api-key", "oauth-personal") and (home / "gemini-credentials.json").is_file():
            return True
        # Builds that keep the Google token in the system keychain still record the account here.
        if auth == "oauth-personal":
            try:
                return bool(json.loads((home / "google_accounts.json").read_text(encoding="utf-8", errors="replace")).get("active"))
            except (OSError, ValueError, AttributeError):
                return False
        return False

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        args = ["-o", "stream-json"]  # Gemini CLI has no effort control; `effort` is ignored.
        if cfg.get("gemini_approval", "yolo") == "auto_edit":
            args += ["--approval-mode", "auto_edit"]
        else:
            args += ["-y"]
        if model:
            args += ["-m", model]
        if cfg.get("extra_dirs"):  # a multi-repository task's other worktrees
            args += ["--include-directories", ",".join(str(d) for d in cfg["extra_dirs"])]
        if session.get("id") and session.get("turns", 0) > 0:
            args += ["--resume", "latest"]
        args += list(cfg.get("gemini_extra_args") or [])
        return windows_cli("gemini", args), self.env(cfg), prompt, session

    def _flush_text(self, ctx, out):
        if ctx.current_text.strip():
            ctx.texts.append(ctx.current_text)
            out.append({"kind": "text", "text": ctx.current_text, "replace_delta": True})
        ctx.current_text = ""

    def parse_line(self, line, ctx):
        obj = _try_json(line)
        if obj is None:
            s = line.strip()
            return [{"kind": "log", "text": s}] if s else []
        typ = obj.get("type")
        out = []
        if typ == "init":
            ctx.session_id = obj.get("session_id") or ctx.session_id
            ctx.model = obj.get("model") or ctx.model
            out.append({"kind": "session", "session_id": ctx.session_id, "model": ctx.model})
        elif typ == "message":
            if obj.get("role") == "assistant":
                content = obj.get("content") or ""
                if obj.get("delta"):
                    ctx.current_text += content
                    out.append({"kind": "text_delta", "text": content})
                else:
                    self._flush_text(ctx, out)
                    if content.strip():
                        ctx.texts.append(content)
                        out.append({"kind": "text", "text": content})
        elif typ == "tool_use":
            self._flush_text(ctx, out)
            tid = obj.get("tool_id") or f"g_{len(ctx.tools)}"
            name = obj.get("tool_name") or "tool"
            ctx.tools[tid] = {"tool": name, "at": time.time()}
            ctx.tool_calls += 1
            out.append({"kind": "tool_use", "id": tid, "tool": name, "category": _tool_category(name),
                        "input": obj.get("parameters"), "summary": _summarize_input(name, obj.get("parameters"))})
        elif typ == "tool_result":
            tid = obj.get("tool_id")
            err = obj.get("error") or {}
            started = ctx.tools.get(tid, {}).get("at")
            out.append({"kind": "tool_result", "id": tid, "ok": obj.get("status") != "error",
                        "output": truncate(obj.get("output") or (err.get("message") if isinstance(err, dict) else "") or "", 12000),
                        "duration": (time.time() - started) if started else None})
        elif typ == "error":
            msg = obj.get("message") or json.dumps(obj)
            if (obj.get("severity") or "error") == "error":
                ctx.error = "Gemini error: " + truncate(msg, 400)
                out.append({"kind": "error", "text": msg})
            else:
                out.append({"kind": "log", "text": msg})
        elif typ == "result":
            self._flush_text(ctx, out)
            stats = obj.get("stats") or {}
            models = stats.get("models") or {}
            for _, m in (models.items() if isinstance(models, dict) else []):
                tok = (m or {}).get("tokens") or {}
                ctx.usage["input"] += int(tok.get("prompt") or tok.get("input") or 0)
                ctx.usage["output"] += int(tok.get("candidates") or tok.get("output") or 0)
                ctx.usage["cached"] += int(tok.get("cached") or 0)
            if not models:
                ctx.usage["input"] += int(stats.get("input_tokens") or 0)
                ctx.usage["output"] += int(stats.get("output_tokens") or 0)
            ctx.result_ok = obj.get("status") != "error" and not ctx.error
            out.append({"kind": "usage", **ctx.usage})
            out.append({"kind": "result", "ok": ctx.result_ok, "text": None, "error": ctx.error})
        return out


ADAPTERS: dict[str, AgentAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "gemini": GeminiAdapter(),
}


# The agent pack (orchestrator/pack/*.py): one adapter per extra CLI, registered by name.
from .pack import PACK_ADAPTERS  # noqa: E402  (pack modules import the helpers above)

ADAPTERS.update(PACK_ADAPTERS)


def adapter(name: str) -> AgentAdapter:
    a = ADAPTERS.get((name or "").lower())
    if not a:
        raise RuntimeError(f"Unknown agent '{name}'. Choose one of: {', '.join(ADAPTERS)}")
    return a


def agent_health(cfg: dict, force: bool = False) -> dict:
    """Health of all agents (cached 45 s)."""
    if not force and _health_cache["data"] and time.time() - _health_cache["at"] < 45:
        return _health_cache["data"]
    def one(item):
        name, a = item
        try:
            return name, a.health(cfg)
        except Exception as e:
            return name, {"agent": name, "label": a.label, "installed": False, "ok": False, "error": str(e)}
    # Fourteen `--version` calls one after another would hold the Agents page for many seconds.
    with ThreadPoolExecutor(max_workers=8) as pool:
        data = dict(pool.map(one, ADAPTERS.items()))
    data["git"] = {"agent": "git", "label": "Git", "installed": bool(which("git")), "ok": bool(which("git")), "path": which("git")}
    data["gh"] = {"agent": "gh", "label": "GitHub CLI", "installed": bool(which("gh")), "ok": bool(which("gh")), "path": which("gh")}
    _health_cache["data"] = data
    _health_cache["at"] = time.time()
    return data


def invalidate_health():
    _health_cache["at"] = 0
