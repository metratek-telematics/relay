"""OpenCode and Kilo Code (an OpenCode fork): `<bin> run --format json` NDJSON adapters.

Stream (one JSON object per line, each with "type", "timestamp" and "sessionID"):
  step_start   a model step begins
  text         part.text, the whole text part (json mode has no token deltas)
  reasoning    part.text, only with --thinking
  tool_use     only once the tool has finished: part.tool, part.callID,
               part.state.{status,input,output,error,title,metadata}
  step_finish  part.tokens.{input,output,reasoning,cache.{read,write}}, part.cost (per step: summed)
  error        error.{name,data.{message,statusCode}}
There is no final result event: the turn ends when the process exits.

The prompt goes on stdin with no positional message. `run` joins positional words
and wraps any word containing spaces in literal quotes, while stdin is passed
verbatim, so stdin also keeps prompts that start with "-" and very long prompts safe.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..agents import AgentAdapter, TurnContext, _summarize_input, _tool_category, _try_json
from ..util import truncate, windows_cli

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_CATEGORY = {"list": "search", "patch": "edit", "multiedit": "edit", "codesearch": "search",
             "skill": "tool", "question": "tool"}


class _OpenCodeBase(AgentAdapter):
    name = ""
    env_prefix = "OPENCODE"
    config_dirs: tuple = ()

    def env(self, cfg):
        env = super().env(cfg)
        p = self.env_prefix
        env.setdefault(f"{p}_DISABLE_AUTOUPDATE", "1")
        env.setdefault(f"{p}_DISABLE_SHARE", "1")
        return env

    def signed_in(self, env) -> bool:
        if super().signed_in(env):
            return True
        p = self.env_prefix
        if env.get(f"{p}_AUTH_CONTENT"):
            return True
        # A BYOK provider with an inline apiKey in config also counts.
        if re.search(r'"apiKey"\s*:\s*"[^"]+', env.get(f"{p}_CONFIG_CONTENT") or ""):
            return True
        home = Path(env.get("HOME") or Path.home())
        for rel in self.config_dirs:
            try:
                txt = (home / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r'"apiKey"\s*:\s*"[^"]+', txt):
                return True
        return False

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        args = ["run", "--format", "json", "--auto", "--thinking"]
        if model:
            args += ["-m", model]
        eff = (effort or "").strip()
        if eff and eff in (self.spec.get("efforts") or []):
            args += ["--variant", eff]
        if session.get("id") and session.get("turns", 0) > 0:
            args += ["-s", session["id"]]
        else:
            # The CLI assigns the ses_ id itself; it arrives on every stream line.
            session.pop("id", None)
            args += ["--title", f"Relay {role}"]
        args += list(cfg.get(f"{self.name}_extra_args") or [])
        return windows_cli(self.binary, args), self.env(cfg), prompt, session

    # ---- stream ---------------------------------------------------------
    def parse_line(self, line, ctx):
        try:
            return self._parse(line, ctx)
        except Exception as e:  # never let one odd line break the turn
            return [{"kind": "log", "text": f"{self.label} parse error: {e}: {truncate(line, 300)}"}]

    def _parse(self, line, ctx: TurnContext):
        obj = _try_json(line)
        if not isinstance(obj, dict):
            s = _ANSI.sub("", line).strip()
            if not s:
                return []
            return [{"kind": "log", "text": s}]
        out = []
        sid = obj.get("sessionID")
        if sid and sid != ctx.session_id:
            ctx.session_id = sid
            out.append({"kind": "session", "session_id": sid, "model": ctx.model})
        typ = obj.get("type")
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if typ == "text":
            t = part.get("text") or ""
            if t.strip() and not part.get("synthetic"):
                ctx.texts.append(t)
                out.append({"kind": "text", "text": t})
        elif typ == "reasoning":
            t = part.get("text") or ""
            if t.strip():
                out.append({"kind": "thinking", "text": t})
        elif typ == "tool_use":
            out += self._tool(part, ctx)
        elif typ == "step_finish":
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            ctx.usage["input"] += int(tok.get("input") or 0)
            ctx.usage["output"] += int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
            ctx.usage["cached"] += int(cache.get("read") or 0)
            ctx.usage["cost_usd"] = round(float(ctx.usage.get("cost_usd") or 0) + float(part.get("cost") or 0), 6)
            out.append({"kind": "usage", **ctx.usage})
            if part.get("reason") not in ("tool-calls", "tool_calls"):
                if not ctx.error:
                    ctx.result_ok = True
                out.append({"kind": "result", "ok": not ctx.error, "text": None, "error": ctx.error})
        elif typ == "error":
            err = obj.get("error") or {}
            data = err.get("data") if isinstance(err, dict) else None
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("message") or "")
            if not msg:
                msg = str(err.get("message") or "") if isinstance(err, dict) else str(err)
            name = err.get("name") if isinstance(err, dict) else ""
            code = data.get("statusCode") if isinstance(data, dict) else None
            text = f"{name or 'Error'}{f' ({code})' if code else ''}: {msg or json.dumps(err)}"
            ctx.error = f"{self.label} error: " + truncate(text, 400)
            ctx.result_ok = False
            out.append({"kind": "error", "text": text})
            out.append({"kind": "result", "ok": False, "text": None, "error": ctx.error})
        return out

    def _tool(self, part, ctx):
        state = part.get("state") or {}
        status = state.get("status")
        if status not in ("completed", "error"):
            return []
        name = part.get("tool") or "tool"
        tid = part.get("callID") or part.get("id") or f"oc_{len(ctx.tools)}"
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        # OpenCode uses camelCase keys (filePath); give the shared summarizer the usual ones.
        norm = dict(inp)
        if "filePath" in norm and "file_path" not in norm:
            norm["file_path"] = norm["filePath"]
        summary = _summarize_input(name, norm) if norm else truncate(state.get("title") or "", 180)
        times = state.get("time") or {}
        dur = None
        try:
            if times.get("start") and times.get("end"):
                dur = (float(times["end"]) - float(times["start"])) / 1000.0
        except (TypeError, ValueError):
            dur = None
        first = tid not in ctx.tools
        ctx.tools[tid] = {"tool": name, "at": time.time()}
        out = []
        if first:
            ctx.tool_calls += 1
            out.append({"kind": "tool_use", "id": tid, "tool": name,
                        "category": _CATEGORY.get(name.lower()) or _tool_category(name),
                        "input": inp, "summary": summary})
        meta = state.get("metadata") or {}
        ok = status == "completed"
        exit_code = meta.get("exit") if isinstance(meta, dict) else None
        if ok and isinstance(exit_code, int) and exit_code != 0:
            ok = False
        output = state.get("output") if ok or state.get("output") else state.get("error")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False) if output is not None else ""
        out.append({"kind": "tool_result", "id": tid, "ok": ok,
                    "output": truncate(output, 12000), "duration": dur})
        return out

    def finalize(self, ctx, rc, run_dir, session):
        res = super().finalize(ctx, rc, run_dir, session)
        if not res["ok"] and not res["error"]:
            res["error"] = f"{self.label} exited with code {rc}"
        return res


class OpenCodeAdapter(_OpenCodeBase):
    name = "opencode"
    env_prefix = "OPENCODE"
    config_dirs = (".config/opencode/opencode.json", ".config/opencode/opencode.jsonc")


class KiloAdapter(_OpenCodeBase):
    name = "kilo"
    env_prefix = "KILO"
    config_dirs = (".config/kilo/kilo.json", ".config/kilo/kilo.jsonc",
                   ".config/kilo/opencode.json", ".config/kilo/opencode.jsonc")

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("KILO_TELEMETRY_LEVEL", "off")
        env.setdefault("KILO_DISABLE_SESSION_INGEST", "1")
        return env


ADAPTERS = {"opencode": OpenCodeAdapter(), "kilo": KiloAdapter()}
