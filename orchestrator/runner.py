"""Per-task process runner.

Runs agent turns and shell commands as subprocesses, streams their structured
output into the task conversation, and provides stop / pause / interrupt /
answer control points for the pipeline.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .agents import TurnContext, adapter
from .environment import venv_bin
from .util import IS_WINDOWS, kill_tree, new_id, now, popen_group_kwargs, truncate


def _with_venv(env: dict, cwd) -> None:
    """Python repos: the .venv Relay prepared is the python, pip and pytest everyone uses in that worktree."""
    vb = venv_bin(cwd) if cwd else ""
    if vb and env is not None:
        env["VIRTUAL_ENV"] = str(Path(vb).parent)
        env["PATH"] = vb + os.pathsep + env.get("PATH", "")

NOISE = (
    "failed to load skill", "hook: sessionstart", "hook: stop", "warning: unable to access",
    "startupprofiler", "[startup]", "loaded cached credentials", "yolo mode is enabled",
    "deprecationwarning", "rate_limit_event",
)


class Stopped(RuntimeError):
    pass


class Interrupted(RuntimeError):
    def __init__(self, note=""):
        super().__init__("Interrupted by user")
        self.note = note


class TurnTimeout(RuntimeError):
    pass


class Runner:
    def __init__(self, tid: str, manager):
        self.tid = tid
        self.m = manager
        self.proc = None
        self.stop_requested = False
        self.pause_requested = False
        self.interrupt_requested = False
        self.interrupt_note = ""
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._answer_event = threading.Event()
        self._answer = None
        self.log_file = None
        self.current = {"state": "idle"}

    # ------------------------------------------------------------------ logging
    def bind_log(self, p):
        self.log_file = Path(p)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def rawlog(self, line: str, tag: str = ""):
        if not self.log_file:
            return
        try:
            with self.log_file.open("a", encoding="utf-8", errors="replace") as f:
                f.write((f"[{tag}] " if tag else "") + str(line).rstrip("\n") + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ events
    def timeline(self, role, title, detail=""):
        self.m.timeline(self.tid, role, title, detail)

    def status(self, status, detail=""):
        self.m.set_status(self.tid, status, detail)

    def meta(self, **kw):
        self.m.set_meta(self.tid, **kw)

    def msg(self, **fields) -> dict:
        fields.setdefault("id", new_id("m"))
        return self.m.message(self.tid, fields)

    def msg_update(self, mid, **patch):
        self.m.message_update(self.tid, mid, patch)

    # ------------------------------------------------------------------ control
    def check_stop(self):
        if self.stop_requested:
            raise Stopped("Stopped by user")

    def wait_if_paused(self):
        if self._pause_event.is_set():
            return
        self.status("paused", "Paused by user · resume to continue")
        self.timeline("user", "Paused", "The next agent turn will start when you resume.")
        while not self._pause_event.wait(0.5):
            self.check_stop()
        self.check_stop()
        self.timeline("user", "Resumed", "")

    def stop(self):
        self.stop_requested = True
        self._pause_event.set()
        self._answer_event.set()
        kill_tree(self.proc)

    def pause(self):
        self.pause_requested = True
        self._pause_event.clear()

    def resume(self):
        self.pause_requested = False
        self._pause_event.set()

    def interrupt(self, note=""):
        """Kill the current agent process; the pipeline re-sends the turn with the note."""
        self.interrupt_requested = True
        self.interrupt_note = note or ""
        kill_tree(self.proc)

    def answer(self, qid, text, extra=None):
        self._answer = {"qid": qid, "text": text, "extra": extra or {}}
        self._answer_event.set()

    def wait_for_answer(self, qid, timeout=None) -> dict | None:
        """Block until the human answers; with a timeout (seconds), return None when nobody did."""
        self._answer_event.clear()
        self._answer = None
        deadline = time.time() + timeout if timeout else None
        while not self._answer_event.wait(0.5):
            self.check_stop()
            if deadline and time.time() > deadline:
                return None
        self.check_stop()
        return self._answer or {"qid": qid, "text": "", "extra": {}}

    # ------------------------------------------------------------------ processes
    def _spawn(self, args, cwd, env, stdin_text, on_line, timeout, role, agent, label):
        self.check_stop()
        cmdline = subprocess.list2cmdline([str(x) for x in args])
        self.rawlog(f"> {cmdline}", role)
        p = subprocess.Popen(
            args, cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=env, **popen_group_kwargs(),
        )
        self.proc = p
        started = time.time()
        last_out = [started]
        self.current = {"state": "running", "role": role, "agent": agent, "pid": p.pid, "started_at": started,
                        "label": label, "command": truncate(cmdline, 300)}
        self.m.process(self.tid, {**self.current, "elapsed": 0, "silent_for": 0})
        stop_hb = threading.Event()
        stall_min = float(self.m.cfg().get("stall_warning_minutes") or 0)
        warned = [False]

        def heartbeat():
            while not stop_hb.wait(1.0):
                if p.poll() is not None:
                    return
                t = time.time()
                silent = t - last_out[0]
                self.m.process(self.tid, {**self.current, "elapsed": t - started, "silent_for": silent})
                if stall_min and silent > stall_min * 60 and not warned[0]:
                    warned[0] = True
                    self.timeline(role, "Agent is quiet", f"No output for {int(silent // 60)} min · still running (PID {p.pid})")

        threading.Thread(target=heartbeat, daemon=True).start()

        def feed():
            try:
                p.stdin.write(stdin_text)
                p.stdin.close()
            except Exception:
                pass

        if stdin_text is not None:
            threading.Thread(target=feed, daemon=True).start()

        rc = None
        timed_out = False
        try:
            for line in iter(p.stdout.readline, ""):
                if line:
                    last_out[0] = time.time()
                    try:
                        on_line(line.rstrip("\r\n"))
                    except Exception as e:  # never let a parser bug kill the run
                        self.rawlog(f"parser error: {e}", "system")
                if self.stop_requested or self.interrupt_requested:
                    kill_tree(p)
                    break
                if timeout and time.time() - started > timeout:
                    timed_out = True
                    kill_tree(p)
                    break
            rc = p.wait(timeout=30) if p.poll() is None else p.poll()
        finally:
            stop_hb.set()
            self.proc = None
            self.current = {"state": "idle"}
            self.m.process(self.tid, {"state": "idle", "elapsed": time.time() - started, "last_role": role, "last_agent": agent})
        if self.stop_requested:
            raise Stopped("Stopped by user")
        if self.interrupt_requested:
            self.interrupt_requested = False
            raise Interrupted(self.interrupt_note)
        if timed_out:
            raise TurnTimeout(f"{label or role} exceeded the {int(timeout // 60)} minute turn timeout")
        return rc, time.time() - started

    # ------------------------------------------------------------------ agent turn
    def run_agent(self, role, agent_name, prompt, cwd, cfg, model, session, run_dir, label="", turn=None, effort="") -> dict:
        self.wait_if_paused()
        self.check_stop()
        ad = adapter(agent_name)
        sent_prompt = prompt
        transcript = list((session or {}).get("transcript") or [])
        if not ad.supports_resume and transcript:
            # This CLI starts every turn with an empty memory, so replay the role's earlier turns.
            sent_prompt = ("You are continuing a conversation, but this tool keeps no memory between turns. "
                           "Here is everything said earlier in this session, oldest first:\n\n"
                           + "\n\n".join(f"--- Relay said ---\n{t['prompt']}\n--- You replied ---\n{t['reply']}" for t in transcript)
                           + "\n\n--- Relay now says ---\n" + prompt)
        args, env, stdin_text, session = ad.build(sent_prompt, Path(cwd), cfg, model, session, Path(run_dir), role, effort=effort or "")
        _with_venv(env, cwd)
        ctx = TurnContext()
        tool_msgs: dict = {}
        delta_msg = {"id": None, "buf": "", "last": 0.0}
        log_lines = []
        self.rawlog(f"===== {role}/{agent_name} turn {turn or ''} {label} =====", role)
        self.rawlog(prompt, f"{role}:prompt")

        def flush_delta(final=None):
            if delta_msg["id"] is None:
                return
            content = final if final is not None else delta_msg["buf"]
            self.msg_update(delta_msg["id"], content=content, streaming=final is None)
            delta_msg["last"] = time.time()
            if final is not None:
                delta_msg["id"] = None
                delta_msg["buf"] = ""

        def on_line(line):
            self.rawlog(line, agent_name)
            for ev in ad.parse_line(line, ctx):
                k = ev["kind"]
                if k == "text":
                    if ev.get("replace_delta") and delta_msg["id"]:
                        flush_delta(final=ev["text"])
                    else:
                        flush_delta(final=delta_msg["buf"]) if delta_msg["id"] else None
                        self.msg(role=role, agent=agent_name, kind="text", content=ev["text"], turn=turn)
                elif k == "text_delta":
                    if delta_msg["id"] is None:
                        m = self.msg(role=role, agent=agent_name, kind="text", content="", streaming=True, turn=turn)
                        delta_msg["id"] = m["id"]
                        delta_msg["buf"] = ""
                    delta_msg["buf"] += ev["text"]
                    if time.time() - delta_msg["last"] > 0.25:
                        flush_delta()
                elif k == "thinking":
                    self.msg(role=role, agent=agent_name, kind="thinking", content=truncate(ev["text"], 6000), turn=turn)
                elif k == "tool_use":
                    if delta_msg["id"]:
                        flush_delta(final=delta_msg["buf"])
                    inp = ev.get("input")
                    try:
                        import json as _j
                        inp_s = inp if isinstance(inp, str) else _j.dumps(inp, ensure_ascii=False, indent=2)
                    except Exception:
                        inp_s = str(inp)
                    m = self.msg(role=role, agent=agent_name, kind="tool", tool=ev.get("tool"), category=ev.get("category"),
                                 summary=ev.get("summary"), input=truncate(inp_s, 6000), status="running", turn=turn)
                    tool_msgs[ev.get("id")] = m["id"]
                elif k == "tool_result":
                    mid = tool_msgs.get(ev.get("id"))
                    if mid:
                        cap = int(cfg.get("budget_tool_output_chars") or 12000)
                        self.msg_update(mid, status="ok" if ev.get("ok", True) else "error",
                                        output=truncate(ev.get("output") or "", cap), duration=ev.get("duration"))
                elif k == "session":
                    self.m.session_seen(self.tid, role, agent_name, ev.get("session_id"), ev.get("model"))
                elif k == "usage":
                    pass  # applied at the end from ctx.usage
                elif k == "error":
                    self.msg(role=role, agent=agent_name, kind="error", content=truncate(ev["text"], 2000), turn=turn)
                elif k == "log":
                    low = ev["text"].lower()
                    if any(n in low for n in NOISE):
                        continue
                    log_lines.append(ev["text"])
                elif k == "result":
                    pass

        timeout = float(cfg.get("agent_turn_timeout_minutes") or 0) * 60 or None
        self.m.turn_started(self.tid, role, agent_name, label, turn)
        rc, elapsed = self._spawn(args, cwd, env, stdin_text, on_line, timeout, role, agent_name, label)
        flush_delta(final=delta_msg["buf"]) if delta_msg["id"] else None
        res = ad.finalize(ctx, rc, Path(run_dir), session)
        if res.get("tools") and not tool_msgs:
            # CLIs that only report their tool calls after the turn (Crush, Continue): show them anyway.
            for t in res["tools"][:200]:
                self.msg(role=role, agent=agent_name, kind="tool", tool=t.get("tool"), category=t.get("category"),
                         summary=t.get("summary"), input="", status="error" if t.get("ok") is False else "ok", turn=turn)
        res["session"] = {**{k: v for k, v in session.items() if not k.startswith("_")},
                          "id": res.get("session_id") or session.get("id"),
                          "turns": int(session.get("turns", 0)) + 1, "agent": agent_name}
        if not ad.supports_resume:
            transcript.append({"prompt": truncate(prompt, 20000), "reply": truncate(res.get("text") or "", 12000)})
            while len(transcript) > 1 and sum(len(t["prompt"]) + len(t["reply"]) for t in transcript) > 80000:
                transcript.pop(0)  # keep the most recent turns within a sane prompt size
            res["session"]["transcript"] = transcript
        res["elapsed"] = elapsed
        if log_lines:
            interesting = list(dict.fromkeys(l for l in log_lines if any(w in l.lower() for w in ("error", "fail", "denied", "cannot", "unable", "requires", "ignoring"))))
            if interesting:
                self.msg(role=role, agent=agent_name, kind="notice", content=truncate("\n".join(interesting[-12:]), 3000), turn=turn)
        self.m.metrics_add(self.tid, agent_name, role, res.get("usage") or {}, elapsed, res.get("tool_calls", 0), turn)
        if rc not in (0, None) and not res.get("text"):
            tail = "\n".join(log_lines[-15:])
            res["ok"] = False
            res["error"] = res.get("error") or f"{ad.label} exited with code {rc}.\n{tail}".strip()
        return res

    # ------------------------------------------------------------------ shell
    def run_shell(self, cmd: str, cwd, role="verify", timeout=None, title=None) -> dict:
        self.check_stop()
        m = self.msg(role=role, agent=None, kind="command", content=cmd, title=title or cmd, status="running")
        lines = []

        def on_line(line):
            self.rawlog(line, role)
            lines.append(line)
            if len(lines) % 40 == 0:
                self.msg_update(m["id"], output=truncate("\n".join(lines), 12000, tail=True))

        args = ["cmd.exe", "/d", "/c", cmd] if IS_WINDOWS else ["bash", "-lc", cmd]
        env = os.environ.copy()
        env["CI"] = env.get("CI", "1")
        env["FORCE_COLOR"] = "0"
        env["NO_COLOR"] = "1"
        _with_venv(env, cwd)
        try:
            rc, elapsed = self._spawn(args, cwd, env, None, on_line, timeout, role, None, cmd)
        except TurnTimeout as e:
            out = "\n".join(lines)
            self.msg_update(m["id"], status="error", output=truncate(out + f"\n\n[timeout] {e}", 12000, tail=True))
            return {"ok": False, "output": out, "rc": -1, "duration": timeout, "error": str(e)}
        out = "\n".join(lines)
        self.msg_update(m["id"], status="ok" if rc == 0 else "error", output=truncate(out, 12000, tail=True), duration=elapsed, rc=rc)
        return {"ok": rc == 0, "output": out, "rc": rc, "duration": elapsed}

    def run_shell_args(self, args, cwd, role="git", title=None, quiet_output=False, timeout=900) -> str:
        self.check_stop()
        cmdline = subprocess.list2cmdline([str(x) for x in args])
        m = None if quiet_output else self.msg(role=role, agent=None, kind="command", content=cmdline, title=title or cmdline, status="running")
        lines = []

        def on_line(line):
            self.rawlog(line, role)
            lines.append(line)

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        rc, elapsed = self._spawn(args, cwd, env, None, on_line, timeout, role, None, title or cmdline)
        out = "\n".join(lines)
        if m:
            self.msg_update(m["id"], status="ok" if rc == 0 else "error", output=truncate(out, 8000, tail=True), duration=elapsed, rc=rc)
        if rc != 0:
            raise RuntimeError(f"{title or cmdline} failed ({rc})\n{truncate(out, 3000, tail=True)}")
        return out
