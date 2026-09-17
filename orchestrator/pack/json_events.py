"""GitHub Copilot CLI, Cline CLI and Goose: JSON event-stream adapters.

Copilot  `copilot --output-format json` JSONL, envelope {"type","data",...}:
           assistant.message_delta (data.deltaContent), assistant.message (data.content, full),
           assistant.reasoning, tool.execution_start / tool.execution_complete (toolCallId),
           session.error, and a final result {sessionId, exitCode}. Token counts are only
           reliable from --usage-output-file, which finalize() reads. Prompt on stdin.
Cline    `cline --json` NDJSON: agent_event {content_start|content_end, contentType text|tool},
           usage (cumulative totals), done, then run_result {finishReason, usage, text}.
           The prompt must be an argument, and piped stdin is appended to it, so a short
           fixed argument carries the real prompt on stdin. No non-interactive resume.
Goose    `goose run --output-format stream-json` NDJSON: message {role, id, content[]} where
           assistant text arrives as fragments sharing one message id, toolRequest /
           toolResponse items pair by id, and complete {input_tokens,...} is cumulative for
           the whole session. Provider failures exit 0 as a locally made assistant text.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from ..agents import AgentAdapter, TurnContext, _summarize_input, _tool_category, _try_json
from ..util import truncate, windows_cli

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ERRLINE = re.compile(r"^(?:error|fatal)\b[:\s]|^\S*error:\s", re.I)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _text_of(content) -> str:
    """Flatten MCP-style content ([{type:text,text}], str, dict) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(c) for c in content if c is not None).strip()
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    return str(content)


class _JsonEventsBase(AgentAdapter):
    """Shared plumbing: delta flushing, stderr error capture, safe parse_line."""

    tool_categories: dict = {}

    def _category(self, tool: str) -> str:
        return self.tool_categories.get((tool or "").lower()) or _tool_category(tool)

    def _flush_text(self, ctx: TurnContext, out: list, full: str | None = None):
        text = full if full is not None else ctx.current_text
        if text and text.strip():
            ctx.texts.append(text)
            out.append({"kind": "text", "text": text, "replace_delta": True})
        ctx.current_text = ""

    def _tool_use(self, ctx, out, tid, name, inp):
        if tid in ctx.tools:
            return
        ctx.tools[tid] = {"tool": name, "at": time.time()}
        ctx.tool_calls += 1
        out.append({"kind": "tool_use", "id": tid, "tool": name, "category": self._category(name),
                    "input": inp, "summary": _summarize_input(name, inp)})

    def _tool_result(self, ctx, out, tid, ok, output):
        started = ctx.tools.get(tid, {}).get("at")
        out.append({"kind": "tool_result", "id": tid, "ok": bool(ok), "output": truncate(output or "", 12000),
                    "duration": (time.time() - started) if started else None})

    def _log(self, line, ctx):
        s = _ANSI.sub("", line).strip()
        if not s:
            return []
        if _ERRLINE.search(s):
            ctx._stderr_error = s  # only used when the process also exits non-zero
        return [{"kind": "log", "text": s}]

    def parse_line(self, line, ctx):
        try:
            obj = _try_json(line)
            if not isinstance(obj, dict):
                return self._log(line, ctx)
            return self._parse(obj, ctx)
        except Exception as e:  # never let an odd line break the turn
            return [{"kind": "log", "text": f"{self.name} parser: {e}: {truncate(line, 300)}"}]

    def _parse(self, obj: dict, ctx: TurnContext) -> list[dict]:
        raise NotImplementedError

    def finalize(self, ctx, rc, run_dir, session):
        if rc not in (0, None) and not ctx.error and getattr(ctx, "_stderr_error", None):
            ctx.error = f"{self.label}: {truncate(ctx._stderr_error, 400)}"
        return super().finalize(ctx, rc, run_dir, session)


# ----------------------------------------------------------------------------- GitHub Copilot
class CopilotAdapter(_JsonEventsBase):
    name = "copilot"
    tool_categories = {"view": "read", "str_replace_editor": "edit", "str_replace": "edit", "create": "edit",
                       "edit": "edit", "glob": "search", "grep": "search", "rg": "search",
                       "web_fetch": "web", "read_bash": "shell", "write_bash": "shell", "stop_bash": "shell",
                       "update_todo": "plan", "report_intent": "plan"}

    def signed_in(self, env) -> bool:
        # Bare existence of ~/.copilot/config.json proves nothing (it also holds settings), so the
        # catalog's auth files are not used here.
        if any(env.get(k) for k in (self.spec.get("auth") or {}).get("env") or []):
            return True
        if env.get("COPILOT_PROVIDER_BASE_URL"):  # BYOK provider, no GitHub login needed
            return True
        home = Path(env.get("HOME") or Path.home())
        cdir = Path(env["COPILOT_HOME"]) if env.get("COPILOT_HOME") else home / ".copilot"
        try:
            d = json.loads((cdir / "config.json").read_text(encoding="utf-8", errors="replace"))
            # Without a system keychain `copilot login` (after a "y" to the plain-text prompt) keeps
            # the token here; snake_case is the older spelling the CLI still reads.
            if isinstance(d, dict) and any(d.get(k) for k in ("copilotTokens", "copilot_tokens", "authTokens")):
                return True
        except (OSError, ValueError):
            pass
        # The CLI falls back to `gh auth token`, so a gh login with a stored token works too.
        try:
            hosts = (home / ".config" / "gh" / "hosts.yml").read_text(encoding="utf-8", errors="replace")
            return bool(re.search(r"^\s*oauth_token:\s*\S", hosts, re.M))
        except OSError:
            return False

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("COPILOT_AUTO_UPDATE", "false")
        return env

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        usage_file = Path(run_dir) / f"copilot_{role}_usage.json"
        try:
            usage_file.unlink(missing_ok=True)
        except Exception:
            pass
        args = ["--output-format", "json", "--allow-all-tools", "--no-auto-update",
                "-C", str(cwd), "--usage-output-file", str(usage_file)]
        if model:
            args += ["--model", model]
        eff = (effort or "").strip()
        if eff and eff in (self.spec.get("efforts") or []):
            args += ["--reasoning-effort", eff]
        if session.get("id") and session.get("turns", 0) > 0:
            args.append(f"--resume={session['id']}")  # optional-value flag: keep it attached
            session["_resumed"] = True
        else:
            session["id"] = self.new_session_id()
            session.pop("copilot_tokens", None)
            args.append(f"--session-id={session['id']}")
        args += list(cfg.get("copilot_extra_args") or [])
        session["_usage_file"] = str(usage_file)
        # No -p: the prompt goes on stdin, which is safe for long prompts and ones starting with "-".
        return windows_cli(self.binary, args), self.env(cfg), prompt, session

    def _parse(self, obj, ctx):
        typ = obj.get("type") or ""
        d = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        out: list = []
        if typ == "assistant.message_delta":
            chunk = d.get("deltaContent") or ""
            if chunk:
                ctx.current_text += chunk
                out.append({"kind": "text_delta", "text": chunk})
        elif typ == "assistant.message":
            ctx.model = d.get("model") or ctx.model
            content = d.get("content") or ""
            self._flush_text(ctx, out, content if content.strip() else None)
        elif typ == "assistant.reasoning":
            if (d.get("content") or "").strip():
                out.append({"kind": "thinking", "text": d["content"]})
        elif typ == "tool.execution_start":
            self._flush_text(ctx, out)
            tid = d.get("toolCallId") or f"cp_{len(ctx.tools)}"
            ctx.model = d.get("model") or ctx.model
            self._tool_use(ctx, out, tid, d.get("toolName") or "tool", d.get("arguments"))
        elif typ == "tool.execution_complete":
            tid = d.get("toolCallId")
            if tid not in ctx.tools:
                self._tool_use(ctx, out, tid, "tool", None)
            res = d.get("result") if isinstance(d.get("result"), dict) else {}
            err = d.get("error") if isinstance(d.get("error"), dict) else {}
            text = res.get("content") or res.get("detailedContent") or err.get("message") or ""
            self._tool_result(ctx, out, tid, d.get("success", not err), _text_of(text))
        elif typ == "assistant.usage":
            ctx.usage["input"] += _int(d.get("inputTokens"))
            ctx.usage["output"] += _int(d.get("outputTokens"))
            ctx.usage["cached"] += _int(d.get("cacheReadTokens"))
        elif typ == "session.error":
            msg = d.get("message") or json.dumps(d)
            ctx.error = "Copilot error: " + truncate(msg, 400)
            out.append({"kind": "error", "text": msg})
        elif typ == "session.warning":
            out.append({"kind": "log", "text": d.get("message") or json.dumps(d)})
        elif typ == "result":
            self._flush_text(ctx, out)
            ctx._copilot_result = True
            ctx.session_id = obj.get("sessionId") or ctx.session_id
            ctx.result_ok = _int(obj.get("exitCode")) == 0 and not ctx.error
            if not ctx.result_ok and not ctx.error:
                ctx.error = f"Copilot exited with code {obj.get('exitCode')}"
            out.append({"kind": "session", "session_id": ctx.session_id, "model": ctx.model})
            out.append({"kind": "usage", **ctx.usage})
            out.append({"kind": "result", "ok": ctx.result_ok, "text": None, "error": ctx.error})
        return out

    def finalize(self, ctx, rc, run_dir, session):
        uf = session.get("_usage_file")
        if uf:
            try:
                d = json.loads(Path(uf).read_text(encoding="utf-8", errors="replace"))
                metrics = d.get("modelMetrics") or {}
                tot = {"input": 0, "output": 0, "cached": 0}
                for mname, m in (metrics.items() if isinstance(metrics, dict) else []):
                    u = (m or {}).get("usage") or {}
                    tot["input"] += _int(u.get("inputTokens"))
                    tot["output"] += _int(u.get("outputTokens"))
                    tot["cached"] += _int(u.get("cacheReadTokens"))
                    ctx.model = ctx.model or mname
                if metrics:
                    # The file covers the whole session, so a resumed turn subtracts the last total.
                    prev = session.get("copilot_tokens") if session.get("_resumed") else None
                    ctx.usage.update({k: max(0, tot[k] - _int((prev or {}).get(k))) for k in tot})
                    if any(tot.values()):
                        session["copilot_tokens"] = tot
            except (OSError, ValueError, AttributeError, TypeError):
                pass
        if not getattr(ctx, "_copilot_result", False) and not session.get("_resumed"):
            session["id"] = None  # never started (no auth, bad flag): don't try to resume it next turn
        return super().finalize(ctx, rc, run_dir, session)


# ----------------------------------------------------------------------------- Cline
# Provider ids Cline accepts for -P (the ones in providers.json count too).
_CLINE_PROVIDERS = {
    "cline", "anthropic", "openai", "openai-native", "openai-compatible", "openai-codex", "openrouter", "gemini",
    "vertex", "bedrock", "ollama", "lmstudio", "deepseek", "xai", "groq", "mistral", "litellm", "requesty",
    "together", "fireworks", "cerebras", "sambanova", "moonshot", "qwen", "doubao", "zai", "huggingface",
    "vercel-ai-gateway", "aihubmix", "baseten", "nebius", "oca", "minimax", "hicap", "v0", "claude-code",
}


class ClineAdapter(_JsonEventsBase):
    name = "cline"
    supports_resume = False  # `--id` forces the interactive TUI
    tool_categories = {"run_commands": "shell", "read_files": "read", "editor": "edit", "list_files": "search",
                       "search_files": "search", "fetch_web": "web", "submit_and_exit": "plan"}
    # Cline appends piped stdin to the argument ("arg\n\nstdin"), so the real prompt rides on stdin:
    # no ARG_MAX limit and no chance of a leading "-" being read as an option.
    PROMPT_ARG = "Carry out the task below."

    @staticmethod
    def _providers_file(env) -> Path:
        if env.get("CLINE_PROVIDER_SETTINGS_PATH"):
            return Path(env["CLINE_PROVIDER_SETTINGS_PATH"])
        data = Path(env["CLINE_DATA_DIR"]) if env.get("CLINE_DATA_DIR") else \
            Path(env.get("HOME") or Path.home()) / ".cline" / "data"
        return data / "settings" / "providers.json"

    def _saved(self, env) -> dict:
        try:
            d = json.loads(self._providers_file(env).read_text(encoding="utf-8", errors="replace"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def signed_in(self, env) -> bool:
        if any(env.get(k) for k in ("CLINE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")):
            return True
        provs = self._saved(env).get("providers") or {}
        return any(isinstance(p, dict) and (p.get("settings") or p.get("tokenSource")) for p in provs.values())

    def resolve_model(self, model: str, env) -> tuple[str, str]:
        """Relay model string "provider:model" -> (provider, model); blanks come from `cline auth`."""
        saved = self._saved(env)
        provs = saved.get("providers") if isinstance(saved.get("providers"), dict) else {}
        last = saved.get("lastUsedProvider") or ""
        model = (model or "").strip()
        prov = ""
        if ":" in model:
            head, rest = model.split(":", 1)
            if head in provs or head.lower() in _CLINE_PROVIDERS:
                prov, model = head, rest.strip()
        if not prov:
            # A bare model id: use the provider that has it saved, else the last used one.
            prov = next((p for p, v in provs.items() if model and ((v or {}).get("settings") or {}).get("model") == model), "") \
                or last or (next(iter(provs), "") if provs else "")
        if not model and prov:
            model = (((provs.get(prov) or {}).get("settings") or {}).get("model") or "")
        return prov, model

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("CLINE_SESSION_BACKEND_MODE", "local")  # no hub daemon left running after the turn
        env.setdefault("CLINE_NO_AUTO_UPDATE", "1")
        return env

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        session.pop("id", None)  # every turn is a fresh Cline task
        env = self.env(cfg)
        prov, mdl = self.resolve_model(model, env)
        args = ["--json", "-c", str(cwd)]
        if prov:
            args += ["-P", prov]
        if mdl:
            args += ["-m", mdl]
        eff = (effort or "").strip()
        if eff and eff in (self.spec.get("efforts") or []):
            args += ["--thinking", eff]
        args += list(cfg.get("cline_extra_args") or [])
        args.append(self.PROMPT_ARG)
        return windows_cli(self.binary, args), env, prompt, session

    _NOISE = re.compile(r"^at \S+ \(|^AI SDK Warning|^Warning: AI SDK Warning")

    def _log(self, line, ctx):
        # AI SDK deprecation warnings arrive with Bun stack frames on every model call.
        if self._NOISE.search(_ANSI.sub("", line).strip()):
            return []
        return super()._log(line, ctx)

    def _tool_use(self, ctx, out, tid, name, inp):
        n = len(out)
        super()._tool_use(ctx, out, tid, name, inp)
        cmds = inp.get("commands") if isinstance(inp, dict) else None
        if len(out) > n and cmds:
            first = cmds if isinstance(cmds, str) else "; ".join(str(c) for c in cmds)
            out[-1]["summary"] = truncate(first.splitlines()[0] if first else "", 180)

    def _parse(self, obj, ctx):
        typ = obj.get("type") or ""
        out: list = []
        if typ == "agent_event":
            ev = obj.get("event") if isinstance(obj.get("event"), dict) else {}
            if obj.get("parentAgentId") or ev.get("parentAgentId"):
                return out  # sub-agent chatter; the parent reports its outcome
            et, ct = ev.get("type"), ev.get("contentType")
            if et == "content_start" and ct == "text":
                chunk = ev.get("text") or ""
                if chunk:
                    ctx.current_text += chunk
                    out.append({"kind": "text_delta", "text": chunk})
            elif et == "content_end" and ct == "text":
                full = ev.get("text") or ""
                self._flush_text(ctx, out, full if full.strip() else None)
            elif et == "content_end" and ct in ("reasoning", "thinking"):
                txt = ev.get("text") or ev.get("reasoning") or ""
                if txt.strip():
                    out.append({"kind": "thinking", "text": txt})
            elif et == "content_start" and ct == "tool":
                self._flush_text(ctx, out)
                tid = ev.get("toolCallId") or f"cl_{len(ctx.tools)}"
                self._tool_use(ctx, out, tid, ev.get("toolName") or "tool", ev.get("input"))
            elif et == "content_end" and ct == "tool":
                tid = ev.get("toolCallId") or f"cl_{max(len(ctx.tools) - 1, 0)}"
                if tid not in ctx.tools:
                    self._tool_use(ctx, out, tid, ev.get("toolName") or "tool", ev.get("input"))
                output, ok = ev.get("output"), not ev.get("error")
                if isinstance(output, list) and output and all(isinstance(x, dict) for x in output):
                    parts = []
                    for x in output:
                        ok = ok and x.get("success", True) is not False
                        body = x.get("result") if x.get("result") is not None else x.get("error")
                        parts.append((f"$ {x['query']}\n" if x.get("query") else "") + _text_of(body))
                    text = "\n".join(parts)
                else:
                    text = _text_of(output if output is not None else ev.get("error"))
                self._tool_result(ctx, out, tid, ok, text)
            elif et == "usage":
                ctx.usage["input"] = _int(ev.get("totalInputTokens")) or ctx.usage["input"]
                ctx.usage["output"] = _int(ev.get("totalOutputTokens")) or ctx.usage["output"]
                ctx.usage["cached"] = _int(ev.get("totalCacheReadTokens")) or ctx.usage["cached"]
                if ev.get("totalCost") is not None:
                    ctx.usage["cost_usd"] = float(ev.get("totalCost") or 0)
            elif et == "done":
                self._flush_text(ctx, out)
                if ev.get("reason") == "error":
                    ctx.error = "Cline error: " + truncate(ev.get("text") or "agent stopped with an error", 400)
                    out.append({"kind": "error", "text": ev.get("text") or ctx.error})
        elif typ == "run_result":
            self._flush_text(ctx, out)
            u = obj.get("usage") or obj.get("aggregateUsage") or {}
            if u:
                ctx.usage["input"] = _int(u.get("inputTokens"))
                ctx.usage["output"] = _int(u.get("outputTokens"))
                ctx.usage["cached"] = _int(u.get("cacheReadTokens"))
                ctx.usage["cost_usd"] = float(u.get("totalCost") or 0)
            m = obj.get("model")
            if isinstance(m, dict):
                ctx.model = "{}:{}".format(m.get("provider"), m.get("id")) if m.get("provider") else m.get("id")
            reason = obj.get("finishReason") or ""
            if reason == "error":
                ctx.error = ctx.error or "Cline error: " + truncate(obj.get("text") or "run failed", 400)
                ctx.result_ok = False
            else:
                ctx.result_ok = not ctx.error
                if (obj.get("text") or "").strip():
                    ctx.result_text = obj["text"]
            out.append({"kind": "session", "session_id": None, "model": ctx.model})
            out.append({"kind": "usage", **ctx.usage})
            out.append({"kind": "result", "ok": ctx.result_ok, "text": ctx.result_text, "error": ctx.error})
        elif typ == "error":
            msg = obj.get("message") or json.dumps(obj)
            ctx.error = "Cline error: " + truncate(msg, 400)
            out.append({"kind": "error", "text": msg})
        return out  # hook_event, iteration_*: nothing to show

    def finalize(self, ctx, rc, run_dir, session):
        res = super().finalize(ctx, rc, run_dir, session)
        res["session_id"] = None
        return res


# ----------------------------------------------------------------------------- Goose
_GOOSE_PROVIDERS = {
    "openai", "anthropic", "google", "gemini", "openrouter", "ollama", "databricks", "azure_openai", "groq", "xai",
    "litellm", "gcp_vertex_ai", "aws_bedrock", "bedrock", "sagemaker_tgi", "github_copilot", "claude_code", "codex",
    "gemini-cli", "cursor-agent", "chatgpt_codex", "deepseek", "mistral", "venice", "snowflake", "tetrate",
    "lmstudio", "docker_model_runner", "ramalama", "xai", "cerebras", "together", "fireworks", "custom",
}
_GOOSE_ERR = re.compile(r"^(?:Ran into this error|Network error|Server error|Authentication error|"
                        r"Request failed|Error:|Context length exceeded)|error:", re.I)


class GooseAdapter(_JsonEventsBase):
    name = "goose"
    tool_categories = {"shell": "shell", "developer__shell": "shell", "edit": "edit", "write": "edit",
                       "developer__text_editor": "edit", "tree": "search", "analyze": "search",
                       "todo__todo_write": "plan", "delegate": "agent", "read_image": "read"}

    def signed_in(self, env) -> bool:
        if super().signed_in(env) or env.get("GOOSE_PROVIDER"):
            return True
        return False

    def env(self, cfg):
        env = super().env(cfg)
        env["GOOSE_MODE"] = "auto"  # approve modes hard-fail without a terminal
        env.setdefault("GOOSE_TELEMETRY_OFF", "1")
        env.setdefault("GOOSE_CLI_SHOW_THINKING", "1")
        return env

    @staticmethod
    def split_model(model: str) -> tuple[str, str]:
        """"provider:model" -> (provider, model) when the prefix names a Goose provider."""
        model = (model or "").strip()
        if ":" in model:
            head, rest = model.split(":", 1)
            if head.lower() in _GOOSE_PROVIDERS:
                return head, rest.strip()
        return "", model

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        args = ["run", "-i", "-", "--output-format", "stream-json", "-q"]
        prov, mdl = self.split_model(model)
        if prov:
            args += ["--provider", prov]
        if mdl:
            args += ["--model", mdl]
        if session.get("id") and session.get("turns", 0) > 0:
            args += ["-r", "-n", session["id"]]
            session["_resumed"] = True
        else:
            session["id"] = f"relay-{uuid.uuid4()}"
            session.pop("goose_tokens", None)
            args += ["-n", session["id"]]
        args += list(cfg.get("goose_extra_args") or [])
        return windows_cli(self.binary, args), self.env(cfg), prompt, session

    def _parse(self, obj, ctx):
        typ = obj.get("type") or ""
        out: list = []
        if typ == "message":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            role, mid = msg.get("role"), msg.get("id") or ""
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            inf = meta.get("inference") if isinstance(meta.get("inference"), dict) else None
            if inf and (inf.get("requestedModel") or inf.get("model")):
                ctx.model = inf.get("model") or inf.get("requestedModel")
            for item in msg.get("content") or []:
                if not isinstance(item, dict):
                    continue
                it = item.get("type")
                if role == "assistant" and it == "text":
                    chunk = item.get("text") or ""
                    if mid != getattr(ctx, "_goose_mid", None):
                        self._flush_text(ctx, out)
                        ctx._goose_mid = mid
                    # Goose reports provider failures as a text it made itself (msg_ id, no inference).
                    if mid.startswith("msg_") and not inf and _GOOSE_ERR.search(chunk.strip()):
                        ctx.error = "Goose error: " + truncate(chunk.strip(), 400)
                        out.append({"kind": "error", "text": chunk.strip()})
                        continue
                    if chunk:
                        ctx.current_text += chunk
                        out.append({"kind": "text_delta", "text": chunk})
                elif it in ("thinking", "reasoning"):
                    txt = item.get("thinking") or item.get("text") or ""
                    if txt.strip():
                        out.append({"kind": "thinking", "text": txt})
                elif it == "toolRequest":
                    self._flush_text(ctx, out)
                    ctx._goose_mid = None
                    tid = item.get("id") or f"gs_{len(ctx.tools)}"
                    call = item.get("toolCall") if isinstance(item.get("toolCall"), dict) else {}
                    val = call.get("value") if isinstance(call.get("value"), dict) else {}
                    name = val.get("name") or "tool"
                    self._tool_use(ctx, out, tid, name, val.get("arguments"))
                    if call.get("status") not in (None, "success"):
                        self._tool_result(ctx, out, tid, False, _text_of(call.get("error") or call.get("value")))
                elif it == "toolResponse":
                    tid = item.get("id")
                    if tid not in ctx.tools:
                        self._tool_use(ctx, out, tid, "tool", None)
                    res = item.get("toolResult") if isinstance(item.get("toolResult"), dict) else {}
                    val = res.get("value")
                    if isinstance(val, dict):
                        ok = res.get("status") == "success" and not val.get("isError")
                        text = _text_of(val.get("content"))
                    else:
                        ok = res.get("status") == "success"
                        text = _text_of(val if val is not None else res.get("error"))
                    self._tool_result(ctx, out, tid, ok, text)
        elif typ == "complete":
            self._flush_text(ctx, out)
            ctx._goose_totals = {"input": _int(obj.get("input_tokens")), "output": _int(obj.get("output_tokens")),
                                 "cached": _int(obj.get("cache_read_input_tokens"))}
            ctx.usage.update(ctx._goose_totals)  # cumulative; finalize turns it into this turn's share
            ctx.result_ok = not ctx.error
            out.append({"kind": "session", "session_id": ctx.session_id, "model": ctx.model})
            out.append({"kind": "usage", **ctx.usage})
            out.append({"kind": "result", "ok": ctx.result_ok, "text": None, "error": ctx.error})
        elif typ == "error":
            msg = obj.get("error") or obj.get("message") or json.dumps(obj)
            ctx.error = "Goose error: " + truncate(_text_of(msg), 400)
            out.append({"kind": "error", "text": _text_of(msg)})
        return out

    def finalize(self, ctx, rc, run_dir, session):
        totals = getattr(ctx, "_goose_totals", None)
        if totals:
            prev = session.get("goose_tokens") if session.get("_resumed") else None
            if isinstance(prev, dict):
                for k in ("input", "output", "cached"):
                    ctx.usage[k] = max(0, totals[k] - _int(prev.get(k)))
            if any(totals.values()):
                session["goose_tokens"] = totals  # kept with the session for the next resumed turn
        if ctx.result_ok is None and rc == 0 and not ctx.error:
            ctx.error = "Goose ended without a result"  # stream cut short
        if totals is None and not session.get("_resumed"):
            # Goose stopped before the session got going (no provider, bad flag): the named
            # session may not exist, so the next turn must start fresh instead of resuming it.
            session["id"] = None
        ctx.session_id = session.get("id")
        return super().finalize(ctx, rc, run_dir, session)


ADAPTERS = {
    "copilot": CopilotAdapter(),
    "cline": ClineAdapter(),
    "goose": GooseAdapter(),
}
