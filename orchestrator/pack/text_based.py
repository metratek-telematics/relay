"""Agent pack: CLIs without a usable JSON event stream (Aider, Crush, Continue).

Their plain output is streamed live as text / log events. The structured result
(the agent's actual reply, tool calls, usage) is rebuilt in finalize() from what
each CLI leaves behind:
  * Aider    - its chat-history markdown file (kept in the task's run dir),
  * Crush    - `crush session show <id> --json` against a data dir in the run dir,
  * Continue - the session JSON under a per-session CONTINUE_GLOBAL_DIR in the run dir.
All three keep their session state outside the worktree so nothing ends up committed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ..agents import AgentAdapter, _summarize_input, _tool_category, _try_json
from ..util import truncate, windows_cli


def _state_dir(run_dir: Path) -> Path:
    d = Path(run_dir) / "agent-state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _efforts(adapter: AgentAdapter, effort: str) -> str:
    effort = (effort or "").strip()
    return effort if effort and effort in (adapter.spec.get("efforts") or []) else ""


def _resuming(session: dict) -> bool:
    return bool(session.get("id")) and int(session.get("turns", 0) or 0) > 0


def _num(tok: str) -> int:
    """Aider rounds token counts: '775', '2.5k', '1.2M'."""
    s = (tok or "").strip().lower().replace(",", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1000000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _tool_summary(tool: str, inp) -> dict:
    return {"tool": tool, "category": _tool_category(tool), "summary": _summarize_input(tool, inp)}


# ----------------------------------------------------------------------------- Aider
_AIDER_TOKENS = re.compile(
    r"^Tokens: ([\d.,]+[kKmM]?) sent(?:, ([\d.,]+[kKmM]?) cache write)?(?:, ([\d.,]+[kKmM]?) cache hit)?, "
    r"([\d.,]+[kKmM]?) received\.(?: Cost: \$([\d.]+) message, \$([\d.]+) session\.)?")
_AIDER_APPLIED = re.compile(r"^Applied edit to (.+?)\s*$")
_AIDER_ERROR = re.compile(r"^(litellm\.\w+(?:Error|Exception)\b.*|.*\bAPIConnectionError\b.*)$")
_AIDER_NOISE = re.compile(
    r"^(Aider v\d|Model: |Main model: |Weak model: |Editor model: |Git repo: |Repo-map: |Analytics have been|"
    r"Update git (name|email) with|Restored previous conversation history|Added .+ to the chat|Warning: |"
    r"Can't initialize prompt toolkit|Newer aider version|Cur working dir|Git working dir|Note: |Retrying in |"
    r"Did not apply edit|The LLM did not conform|Only \d+ reflections allowed|Add file to the chat\?|"
    r"Create new file\?|Allow edits to file|Committing |Commit [0-9a-f]{7}|Scraping |https://aider\.chat/|"
    r"The API provider|Open URL for more info|Unable to add |Skipping |Dropping |Initial repo scan|"
    r"Scanning repo:|Updating repo map|Has it been deleted)")


class AiderAdapter(AgentAdapter):
    """Aider edits files but has no tool loop: it never runs shell commands on its own."""
    name = "aider"

    # Keys and endpoints a repo `.env` could silently override (aider loads it with override=True).
    _PROTECT = re.compile(r"^[A-Z0-9_]+_(API_KEY|API_BASE|BASE_URL|API_VERSION)$|^VERTEXAI_(PROJECT|LOCATION)$")

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("COLUMNS", "4000")  # keep error lines unwrapped
        protect = [f"{k}={v}" for k, v in sorted(env.items())
                   if self._PROTECT.match(k) and v and not re.search(r"[,\[\]\n]", v)]
        if protect:
            # --set-env is applied after aider loads .env files, so Relay's credentials win.
            env["AIDER_SET_ENV"] = "[" + ", ".join(protect) + "]"
        return env

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        env = self.env(cfg)
        if not model and not any(env.get(k) for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
                                                        "OPENAI_API_KEY", "GEMINI_API_KEY", "VERTEXAI_PROJECT")):
            home = Path(env.get("HOME") or Path.home())
            if not ((home / ".aider.conf.yml").is_file() or (Path(cwd) / ".aider.conf.yml").is_file()):
                # Without a model or a key aider offers an OpenRouter browser login and waits 5 minutes.
                raise RuntimeError("Aider needs a model or a provider API key: set one under Settings -> Agents -> Aider.")
        resume = _resuming(session)
        session["id"] = session.get("id") or self.new_session_id()
        state = _state_dir(run_dir)
        hist = Path(session.get("history_file") or state / f"aider-{session['id']}.chat.md")
        if not resume and hist.exists():
            hist.unlink()
        session["history_file"] = str(hist)
        prompt_file = state / f"aider-{session['id']}.prompt.md"
        prompt_file.write_text(prompt or "", encoding="utf-8")
        session["_hist_offset"] = hist.stat().st_size if hist.exists() else 0
        session["_cwd"] = str(cwd)
        session["_tags_preexisting"] = [p.name for p in Path(cwd).glob(".aider.tags.cache.v*")]
        args = ["--no-pretty", "--yes-always", "--no-auto-commits", "--no-dirty-commits", "--no-gitignore",
                "--analytics-disable", "--no-check-update", "--no-show-release-notes", "--no-show-model-warnings",
                "--disable-playwright", "--no-detect-urls", "--no-fancy-input", "--no-suggest-shell-commands",
                "--encoding", "utf-8",
                "--chat-history-file", str(hist),
                "--input-history-file", str(state / f"aider-{session['id']}.input")]
        if model:
            args += ["--model", model]
        eff = _efforts(self, effort)
        if eff:
            args += ["--reasoning-effort", eff]
        if resume and hist.exists():
            args.append("--restore-chat-history")
        args += [str(a) for a in (cfg.get("aider_extra_args") or [])]
        args += ["--message-file", str(prompt_file)]
        return windows_cli(self.binary, args), env, None, session

    def parse_line(self, line, ctx):
        try:
            s = line.rstrip()
            if not s.strip():
                return [{"kind": "text_delta", "text": "\n"}] if ctx.current_text else []
            m = _AIDER_TOKENS.match(s.strip())
            if m:
                ctx.usage["input"] += _num(m.group(1)) + _num(m.group(2) or "") + _num(m.group(3) or "")
                ctx.usage["cached"] += _num(m.group(3) or "")
                ctx.usage["output"] += _num(m.group(4))
                if m.group(5):
                    ctx.usage["cost_usd"] = round(ctx.usage["cost_usd"] + float(m.group(5)), 6)
                ctx.result_ok = True
                ctx.error = None  # a retry succeeded
                return [{"kind": "usage", **ctx.usage}]
            m = _AIDER_APPLIED.match(s.strip())
            if m:
                path = m.group(1)
                tid = f"aider_edit_{len(ctx.tools)}"
                ctx.tools[tid] = {"tool": "Edit", "at": time.time()}
                ctx.tool_calls += 1
                inp = {"file_path": path}
                return [{"kind": "tool_use", "id": tid, "tool": "Edit", "category": "edit", "input": inp,
                         "summary": truncate(path, 180)},
                        {"kind": "tool_result", "id": tid, "ok": True, "output": s.strip(), "duration": 0}]
            if _AIDER_ERROR.match(s.strip()):
                ctx.error = "Aider: " + truncate(s.strip(), 400)
                return [{"kind": "error", "text": s.strip()}]
            if _AIDER_NOISE.match(s.strip()):
                return [{"kind": "log", "text": s.strip()}]
            ctx.current_text += s + "\n"
            return [{"kind": "text_delta", "text": s + "\n"}]
        except Exception as e:  # never raise from a parser
            return [{"kind": "log", "text": f"aider parse error: {e}"}]

    @staticmethod
    def _replies(chunk: str) -> list[str]:
        """Assistant replies from aider's chat-history markdown (user lines are '#### ', tool output '> ')."""
        replies, cur = [], []
        for raw in chunk.splitlines():
            if raw.startswith(("#### ", "> ", "# aider chat started at")) or raw in ("####", ">"):
                if "".join(cur).strip():
                    replies.append("\n".join(cur).strip())
                cur = []
                continue
            cur.append(raw.rstrip())
        if "".join(cur).strip():
            replies.append("\n".join(cur).strip())
        return replies

    def finalize(self, ctx, rc, run_dir, session):
        res = super().finalize(ctx, rc, run_dir, session)
        replies = []
        try:
            hist = Path(session.get("history_file") or "")
            if hist.is_file():
                with open(hist, "rb") as f:
                    f.seek(int(session.get("_hist_offset") or 0))
                    replies = self._replies(f.read().decode("utf-8", errors="replace"))
        except Exception:
            replies = []
        if replies:
            res["text"] = "\n\n".join(replies)
            res["last_message"] = replies[-1]
        else:
            res["text"] = (ctx.current_text or "").strip()
        res["session_id"] = session.get("id")
        res["model"] = ctx.model
        if res["ok"] and not res["text"] and not ctx.result_ok:
            res["ok"] = False
            res["error"] = "Aider finished without a reply (check the model name and API key)."
        if not res["ok"] and not res.get("error"):
            res["error"] = ctx.error or f"Aider exited with code {rc}."
        # Aider's repo-map cache lands in the worktree; drop it unless the repo already had one.
        try:
            cwd = Path(session.get("_cwd") or "")
            keep = set(session.get("_tags_preexisting") or [])
            for p in cwd.glob(".aider.tags.cache.v*"):
                if p.name not in keep and p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        return res


# ----------------------------------------------------------------------------- Crush
_CRUSH_LOG = re.compile(r"^(DEBUG|INFO|WARN|ERROR)\s+\S")
_CRUSH_SESSION = re.compile(r"session_id=([0-9a-fA-F-]{16,36})")
_CRUSH_STREAM_ERR = re.compile(r'Agent stream returned error error="((?:[^"\\]|\\.)*)"')


class CrushAdapter(AgentAdapter):
    """`crush run` auto-approves every tool. Only the final reply reaches stdout; tools and usage
    come from the session database afterwards."""
    name = "crush"

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("CRUSH_DISABLE_METRICS", "1")
        env.setdefault("DO_NOT_TRACK", "1")
        return env

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        resume = _resuming(session) and session.get("data_dir")
        data_dir = Path(session.get("data_dir") or _state_dir(run_dir) / f"crush-{role}-{uuid.uuid4().hex[:8]}")
        data_dir.mkdir(parents=True, exist_ok=True)
        session["data_dir"] = str(data_dir)  # -D keeps crush.db (and .crush/) out of the worktree
        env = self.env(cfg)
        session["_cwd"], session["_env"] = str(cwd), env
        args = ["run", "-v", "-D", str(data_dir), "-c", str(cwd)]
        if model:
            args += ["-m", model]
        eff = _efforts(self, effort)
        if eff:
            args += ["--reasoning-effort", eff]
        if resume:
            args += ["-s", session["id"]]
        args += [str(a) for a in (cfg.get("crush_extra_args") or [])]
        # The prompt goes on stdin: it may be huge or start with "-".
        return windows_cli(self.binary, args), env, prompt or "", session

    def parse_line(self, line, ctx):
        try:
            s = line.rstrip()
            st = s.strip()
            if getattr(ctx, "_crush_errbox", False):
                if st:
                    ctx.error = ctx.error or ("Crush: " + truncate(st, 400))
                    return [{"kind": "log", "text": st}]
                return []
            if st == "ERROR" and not _CRUSH_LOG.match(s):
                ctx._crush_errbox = True
                return []
            if _CRUSH_LOG.match(st):
                out = []
                m = _CRUSH_SESSION.search(st)
                if m and ("session for non-interactive run" in st) and m.group(1) != ctx.session_id:
                    ctx.session_id = m.group(1)
                    out.append({"kind": "session", "session_id": ctx.session_id, "model": ctx.model})
                m = _CRUSH_STREAM_ERR.search(st)
                if m:
                    ctx.error = "Crush: " + truncate(m.group(1).replace('\\"', '"'), 400)
                    out.append({"kind": "error", "text": ctx.error})
                else:
                    out.append({"kind": "log", "text": st})
                return out
            if not st:
                return [{"kind": "text_delta", "text": "\n"}] if ctx.current_text else []
            ctx.current_text += s + "\n"
            return [{"kind": "text_delta", "text": s + "\n"}]
        except Exception as e:
            return [{"kind": "log", "text": f"crush parse error: {e}"}]

    def _show(self, session: dict, sid: str):
        p = subprocess.run(windows_cli(self.binary, ["session", "show", sid, "--json", "-D", session["data_dir"]]),
                           cwd=session.get("_cwd") or None, env=session.get("_env"), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)

    def finalize(self, ctx, rc, run_dir, session):
        res = super().finalize(ctx, rc, run_dir, session)
        sid = ctx.session_id or session.get("id")
        res["text"] = (ctx.current_text or "").strip()
        res["session_id"] = sid
        data = None
        if sid and session.get("data_dir"):
            try:
                data = self._show(session, sid)
            except Exception:
                data = None
        if isinstance(data, dict):
            msgs = data.get("messages") or []
            start = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1) + 1
            texts, tools, calls = [], [], {}
            for m in msgs[start:]:
                for part in m.get("parts") or []:
                    typ = part.get("type")
                    if m.get("role") == "assistant" and typ == "text" and (part.get("text") or "").strip():
                        texts.append(part["text"].strip())
                    elif typ == "tool_call":
                        try:
                            inp = json.loads(part.get("input") or "{}")
                        except Exception:
                            inp = part.get("input")
                        item = {"id": part.get("tool_call_id"), **_tool_summary(part.get("name") or "tool", inp), "ok": None}
                        calls[part.get("tool_call_id")] = item
                        tools.append(item)
                    elif typ == "tool_result" and part.get("tool_call_id") in calls:
                        calls[part["tool_call_id"]]["ok"] = not part.get("is_error")
                if m.get("role") == "assistant":
                    res["model"] = m.get("model") or res.get("model")
            if texts:
                res["text"] = "\n\n".join(texts)
                res["last_message"] = texts[-1]
            res["tool_calls"] = len(tools)
            res["tools"] = tools
            meta = data.get("meta") or {}
            usage = dict(ctx.usage)
            usage["input"] = int(meta.get("prompt_tokens") or 0)
            usage["output"] = int(meta.get("completion_tokens") or 0)
            total_cost = float(meta.get("cost") or 0)
            usage["cost_usd"] = round(max(0.0, total_cost - float(session.get("cost_seen") or 0)), 6)
            session["cost_seen"] = total_cost
            res["usage"] = usage
            if meta.get("uuid"):
                res["session_id"] = meta["uuid"]
        if not res["ok"] and not res.get("error"):
            res["error"] = ctx.error or f"Crush exited with code {rc}."
        return res


# ----------------------------------------------------------------------------- Continue
class ContinueAdapter(AgentAdapter):
    """`cn -p --auto`: prints only the final answer; tools and usage live in the session file.
    Each Relay session gets its own CONTINUE_GLOBAL_DIR so `--resume` (latest session) is exact."""
    name = "continue"
    _PRIVATE = {"sessions", "logs", "index", "permissions.yaml"}

    def env(self, cfg):
        env = super().env(cfg)
        env.setdefault("CONTINUE_CLI_DISABLE_COMMIT_SIGNATURE", "1")
        env.setdefault("CONTINUE_CLI_ENABLE_TELEMETRY", "0")
        env.setdefault("CONTINUE_METRICS_ENABLED", "0")
        return env

    def signed_in(self, env) -> bool:
        base = env.get("CONTINUE_GLOBAL_DIR")
        if base and any((Path(base) / f).is_file() for f in ("config.yaml", "auth.json")):
            return True
        return super().signed_in(env)

    def _link_global(self, real: Path, mine: Path):
        """Share the user's config/login with the per-session dir, keep sessions private."""
        if not real.is_dir() or real.resolve() == mine.resolve():
            return
        for src in real.iterdir():
            if src.name in self._PRIVATE:
                continue
            dst = mine / src.name
            if dst.exists() or dst.is_symlink():
                continue
            try:
                dst.symlink_to(src, target_is_directory=src.is_dir())
            except OSError:
                if src.is_file():
                    shutil.copy2(src, dst)

    @staticmethod
    def _session_file(gdir: Path):
        files = [p for p in (gdir / "sessions").glob("*.json") if p.name != "sessions.json"]
        return max(files, key=lambda p: p.stat().st_mtime) if files else None

    def build(self, prompt, cwd, cfg, model, session, run_dir, role, effort=""):
        session = dict(session or {})
        resume = _resuming(session) and session.get("global_dir")
        session["id"] = session.get("id") or self.new_session_id()
        env = self.env(cfg)
        real = Path(env.get("CONTINUE_GLOBAL_DIR") or Path(env.get("HOME") or Path.home()) / ".continue")
        gdir = Path(session.get("global_dir") or _state_dir(run_dir) / f"continue-{session['id']}")
        gdir.mkdir(parents=True, exist_ok=True)
        self._link_global(real, gdir)
        session["global_dir"] = str(gdir)
        env["CONTINUE_GLOBAL_DIR"] = str(gdir)
        prev = self._session_file(gdir) if resume else None
        session["_prev_len"], session["_prev_cost"] = 0, 0.0
        if prev:
            try:
                d = json.loads(prev.read_text(encoding="utf-8"))
                session["_prev_len"] = len(d.get("history") or [])
                session["_prev_cost"] = float((d.get("usage") or {}).get("totalCost") or 0)
            except Exception:
                pass
        args = ["--auto", "--format", "json"]
        if model:
            args += ["--model", model]
        args += [str(a) for a in (cfg.get("continue_extra_args") or [])]
        if resume:
            args.append("--resume")
        # -p must be last: cn only reads the prompt from stdin when nothing follows -p.
        args.append("-p")
        return windows_cli(self.binary, args), env, prompt or "", session

    def parse_line(self, line, ctx):
        try:
            st = line.strip()
            if not st:
                return []
            obj = _try_json(st)
            if isinstance(obj, dict) and ("status" in obj and ("response" in obj or "message" in obj)):
                status = obj.get("status")
                if "response" in obj:
                    txt = obj.get("response")
                    txt = txt if isinstance(txt, str) else json.dumps(txt, ensure_ascii=False)
                    ctx.result_text = txt
                    ctx.result_ok = status != "error"
                    ctx.texts.append(txt)
                    return [{"kind": "text", "text": txt}]
                if status == "error":
                    ctx.error = "Continue: " + truncate(str(obj.get("message")), 400)
                    ctx.result_ok = False
                    return [{"kind": "error", "text": str(obj.get("message"))}]
                return [{"kind": "log", "text": str(obj.get("message"))}]
            if obj is not None:
                # The model's reply was itself valid JSON, so cn printed it unwrapped.
                ctx.result_text = st
                ctx.texts.append(st)
                return [{"kind": "text", "text": st}]
            ctx.current_text += line.rstrip() + "\n"
            return [{"kind": "log", "text": st}]
        except Exception as e:
            return [{"kind": "log", "text": f"continue parse error: {e}"}]

    def finalize(self, ctx, rc, run_dir, session):
        res = super().finalize(ctx, rc, run_dir, session)
        res["session_id"] = session.get("id")
        res["text"] = ctx.result_text or ""
        try:
            f = self._session_file(Path(session.get("global_dir") or ""))
            d = json.loads(f.read_text(encoding="utf-8")) if f else None
        except Exception:
            d = None
        if isinstance(d, dict):
            hist = d.get("history") or []
            turn = hist[int(session.get("_prev_len") or 0):]
            users = [i for i, h in enumerate(turn) if ((h or {}).get("message") or {}).get("role") == "user"]
            if users:
                turn = turn[users[-1] + 1:]
            texts, tools = [], []
            usage = {"input": 0, "output": 0, "cached": 0, "cost_usd": 0.0}
            for h in turn:
                msg = (h or {}).get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                c = msg.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                if isinstance(c, str) and c.strip():
                    texts.append(c.strip())
                u = msg.get("usage") or {}
                usage["input"] += int(u.get("prompt_tokens") or 0)
                usage["output"] += int(u.get("completion_tokens") or 0)
                usage["cached"] += int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
                res["model"] = u.get("model") or res.get("model")
                states = {s.get("toolCallId"): s for s in (h.get("toolCallStates") or []) if isinstance(s, dict)}
                for tc in msg.get("toolCalls") or []:
                    fn = tc.get("function") or {}
                    st = states.get(tc.get("id")) or {}
                    inp = st.get("parsedArgs")
                    if inp is None:
                        try:
                            inp = json.loads(fn.get("arguments") or "{}")
                        except Exception:
                            inp = fn.get("arguments")
                    tools.append({"id": tc.get("id"), **_tool_summary(fn.get("name") or "tool", inp),
                                  "ok": (st.get("status") in ("done", None)) if st else None})
            total = float((d.get("usage") or {}).get("totalCost") or 0)
            usage["cost_usd"] = round(max(0.0, total - float(session.get("_prev_cost") or 0)), 6)
            res["usage"] = usage
            res["tool_calls"] = len(tools)
            res["tools"] = tools
            if texts:
                res["text"] = "\n\n".join(texts)
                res["last_message"] = texts[-1]
            session["cn_session_id"] = d.get("sessionId")
        if not res["ok"] and not res.get("error"):
            res["error"] = ctx.error or f"Continue exited with code {rc}."
        return res


ADAPTERS = {
    "aider": AiderAdapter(),
    "crush": CrushAdapter(),
    "continue": ContinueAdapter(),
}
