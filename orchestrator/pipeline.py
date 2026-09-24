"""The multi-agent dialogue pipeline.

Supervisor plans and delegates work packages; the worker implements and
reports; the supervisor verifies and decides; an optional independent
reviewer gates delivery. Humans can answer questions, approve delivery and
inject guidance at any turn boundary (or interrupt immediately).

The loop is checkpointed in the task record so an interrupted run can be
resumed against the same persistent agent sessions.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import traceback
from pathlib import Path

from . import config as C
from . import commitguard, connectors, design, designcheck, environment, exploration, gitops, github, judge, lessons, multirepo, protocol, repo_env, stacks
from . import knowledge, perfcheck, solo, timing, tokens, toolbox, triage, verifyfast
from .runner import Interrupted, Stopped, TurnTimeout
from .util import APP_DIR, new_id, now, quiet, read_text, truncate, write_text

SUP_TYPES = {"plan", "instruction", "decision", "question"}
WRK_TYPES = {"report", "question"}
REV_TYPES = {"review"}


_CONFIG_ERRORS = re.compile(
    r"model[^\n]{0,80}(?:not supported|not found|does not exist|is not available|unknown|invalid)|unknown model|"
    r"not supported when using|401 unauthorized|\b401\b[^\n]{0,40}unauthori|invalid api key|api key (?:is )?(?:missing|invalid|not valid)|"
    r"not signed in|not logged in|authentication required|no authentication information|please (?:log ?in|sign in)|"
    r"openrouter[^\n]{0,40}(?:out of credits|free-model requests are used up|rejected the api key|spend(?:ing)? limit|cap reached)", re.I)


def _config_error(text: str) -> bool:
    return bool(_CONFIG_ERRORS.search(text or ""))


# A daily quota or an empty balance does not clear in minutes: retrying it six times with back-off only burns the wait.
_HARD_QUOTA = re.compile(r"free-model requests are used up|out of credits|spend(?:ing)? limit|cap reached|insufficient (?:credits|balance)", re.I)


def _hard_quota(text: str) -> bool:
    return bool(_HARD_QUOTA.search(text or ""))


def suite_not_run_reason(cmd: str, res: dict) -> str:
    """Why a failing test command never got to run the tests (collection/import errors), or "" when tests did run."""
    out = res.get("output") or ""
    tail = out[-6000:]
    if "pytest" in cmd or "py.test" in cmd:
        m = re.search(r"Interrupted: (\d+) errors? during collection", tail)
        if m or (res.get("rc") in (2, 3, 4) and re.search(r"\bERROR collecting\b|ImportError while importing", tail)):
            err = re.search(r"^E\s+((?:ImportError|ModuleNotFoundError|OSError)[^\n]*)", tail, re.M)
            n = m.group(1) if m else "some"
            return f"pytest stopped during collection ({n} error(s))" + (f": {err.group(1).strip()}" if err else "")
    err = re.search(r"cannot open shared object file[^\n]*|Error: Cannot find module '[^']+'", tail)
    if err and "passed" not in tail and "Tests:" not in tail:
        return f"the command failed before running tests: {err.group(0).strip()}"
    return ""


class TurnBudget(RuntimeError):
    pass


def _label(agent):
    return C.AGENTS.get(agent, {}).get("label", agent or "none")


# ----------------------------------------------------------------------------- attachments
def stage_refs(wt, run_dir, paths, tid):
    refs = []
    sandbox = Path(wt) / ".orchestrator_refs" / tid
    runtime = Path(run_dir) / "attachments"
    runtime.mkdir(exist_ok=True)
    if paths:
        sandbox.mkdir(parents=True, exist_ok=True)
    for f in paths or []:
        p = Path(f)
        if not p.exists():
            continue
        a = runtime / p.name
        b = sandbox / p.name
        shutil.copy2(p, a)
        shutil.copy2(p, b)
        refs.append(b)
    return refs, (sandbox if refs else None)


def cleanup_refs(wt):
    shutil.rmtree(Path(wt) / ".orchestrator_refs", ignore_errors=True)


def context_refs(refs):
    if not refs:
        return ""
    return "\n".join(["Reference files inside the worktree (do not commit .orchestrator_refs):"] + [f"- {x}" for x in refs])


# ----------------------------------------------------------------------------- pipeline
class Pipeline(solo.SoloFlow, exploration.ExplorationFlow, design.DesignFlow, multirepo.MultiRepo):
    def __init__(self, task, runner, manager):
        self.task = task
        self.r = runner
        self.m = manager
        self.tid = task["id"]
        self.cfg = manager.cfg()
        wf = task.get("workflow") or {}
        self.roles = wf.get("roles") or self.cfg["roles"]
        self.max_turns = int(wf.get("max_turns") or self.cfg.get("max_turns") or 12)
        self.max_review_rounds = int(wf.get("max_review_rounds") or self.cfg.get("max_review_rounds") or 3)
        self.verify_mode = wf.get("verify_mode") or self.cfg.get("verify_mode") or "each_report"
        self.approval = bool(wf.get("approval_before_delivery", self.cfg.get("approval_before_delivery", False)))
        self.design_gate = bool(self.cfg.get("design_gate", True))
        self.allow_questions = bool(wf.get("allow_agent_questions", self.cfg.get("allow_agent_questions", True)))
        self.run_dir = manager.store.task_dir(self.tid)
        self.sessions = dict(task.get("sessions") or {})
        self.state = dict(task.get("checkpoint") or {})
        self.wt = None
        self.branch = None
        self.base = None
        self.issue_text = ""
        self.refs_text = ""
        self.verify_cmds = []
        self.max_review_rounds = max(self.max_review_rounds, int(self.state.get("max_review_rounds") or 0))
        self._auto_choice = False
        self.lessons_text = ""
        self.playbook_text = ""
        self.knowledge_text = ""  # the task's knowledge docs (orchestrator/knowledge.py), in every kickoff prompt
        self.stack = None  # integration stack definition (orchestrator/stacks.py)
        self.related = []  # the task's other repositories (orchestrator/multirepo.py)

    # ------------------------------------------------------------ small helpers
    def save(self):
        self.m.set_meta(self.tid, checkpoint=self.state, sessions=self.sessions)

    def artifact(self, kind, filename, text):
        p = self.run_dir / filename
        write_text(p, text)
        self.m.artifact(self.tid, kind, str(p))
        return p

    def role_agent(self, role):
        r = self.roles.get(role) or {}
        agent = (r.get("agent") or "").strip()
        # The global role's model only applies when that role uses the same agent: a Codex model name
        # handed to Kilo or Claude would fail (or silently pick a paid default).
        glob = self.cfg.get("roles", {}).get(role, {}) or {}
        same = (glob.get("agent") or "").strip() == agent
        if self.role_provider(role):
            # On OpenRouter the model is an OpenRouter id; blank means the OpenRouter default (openrouter_launch.py).
            own = (r.get("model") or "").strip()
            if not own and same and (glob.get("provider") or "") == "openrouter":
                own = (glob.get("model") or "").strip()
            d = self.cfg.get("agent_defaults", {}).get(agent) or {}
            if not own and (d.get("provider") or "") == "openrouter":
                own = (d.get("model") or "").strip()
            return agent, own
        model = ((r.get("model") or "").strip()
                 or ((glob.get("model") or "").strip() if same else "")
                 or ((self.cfg.get("agent_defaults", {}).get(agent) or {}).get("model") or "").strip())
        return agent, model

    def role_provider(self, role):
        """"openrouter" when the role runs its agent on OpenRouter, else "" (the agent's own sign-in)."""
        r = self.roles.get(role) or {}
        agent = (r.get("agent") or "").strip()
        prov = (r.get("provider") or "").strip().lower()
        if not prov and not (r.get("model") or "").strip():
            # A blank role falls back to the agent's defaults, provider included.
            prov = ((self.cfg.get("agent_defaults", {}).get(agent) or {}).get("provider") or "").strip().lower()
        return "openrouter" if prov == "openrouter" else ""

    def role_effort(self, role):
        r = self.roles.get(role) or {}
        agent = (r.get("agent") or "").strip()
        glob = self.cfg.get("roles", {}).get(role, {}) or {}
        eff = ((r.get("effort") or "").strip()
               or ((glob.get("effort") or "").strip() if (glob.get("agent") or "").strip() == agent else "")
               or ((self.cfg.get("agent_defaults", {}).get(agent) or {}).get("effort") or "").strip())
        allowed = C.AGENTS.get(agent, {}).get("efforts") or []
        return eff if eff in allowed else ""

    def role_account(self, role):
        """The sign-in this role is pinned to, or "" for the automatic pick (orchestrator/accounts.py)."""
        r = self.roles.get(role) or {}
        agent = (r.get("agent") or "").strip()
        glob = self.cfg.get("roles", {}).get(role, {}) or {}
        return ((r.get("account") or "").strip()
                or ((glob.get("account") or "").strip() if (glob.get("agent") or "").strip() == agent else ""))

    def role_sources(self, role):
        """Which layer decided each value: the task itself, the role default, the agent default, or the CLI.

        The page says whose value is in effect, so the answer has to come from the one place that
        resolves it rather than being guessed again in the browser.
        """
        r = self.roles.get(role) or {}
        agent = (r.get("agent") or "").strip()
        glob = self.cfg.get("roles", {}).get(role, {}) or {}
        same = (glob.get("agent") or "").strip() == agent
        d = self.cfg.get("agent_defaults", {}).get(agent) or {}

        def layer(key, value):
            if not value:
                return "cli"
            if (r.get(key) or "").strip() == value:
                return "task"
            if same and (glob.get(key) or "").strip() == value:
                return "role"
            if (d.get(key) or "").strip() == value:
                return "agent"
            return "cli"
        return {"model": layer("model", self.role_agent(role)[1]),
                "effort": layer("effort", self.role_effort(role)),
                "account": "task" if (r.get("account") or "").strip() else ("role" if self.role_account(role) else "auto")}

    def handoff(self, frm, to, title, content, **extra):
        return self.r.msg(role=frm, agent=self.role_agent(frm)[0] if frm in self.roles else None, kind="handoff",
                          to=to, title=title, content=content or "", turn=self.state.get("turn"), **extra)

    def status_for(self, role, turn=None):
        if role == "supervisor":
            return "planning" if not turn else "reviewing"
        if role == "worker":
            return "implementing"
        return "reviewing"

    # ------------------------------------------------------------ agent turns
    def run_role(self, role, prompt, label, turn=None, expect=None, session_key=None, images=None):
        agent, model = self.role_agent(role)
        if not agent:
            raise RuntimeError(f"No agent configured for the {role} role.")
        # A separate session key gives the same agent a fresh, independent conversation (the design reviewer).
        skey = session_key or role
        provider = self.role_provider(role)
        sess = dict(self.sessions.get(skey) or {})
        if sess.get("agent") != agent or (sess.get("provider") or "") != provider:
            # Another agent, or the same CLI moved between its own sign-in and OpenRouter: a new conversation.
            sess = {"agent": agent, "turns": 0, "provider": provider}
        expect = expect or {"supervisor": SUP_TYPES, "worker": WRK_TYPES, "reviewer": REV_TYPES}[role]
        nudges = 0
        failures = 0
        timeouts = 0
        restarts = 0
        original_prompt = prompt
        max_failures = max(1, int(self.cfg.get("agent_failure_retries") or 6))

        def fresh_session(reason):
            # A crashed or wedged CLI session often keeps failing on resume (corrupt history, a stuck tool call).
            # Start the role over on a new session with the original request; the worktree keeps all the work.
            nonlocal sess, restarts
            restarts += 1
            had_turns = int(sess.get("turns", 0)) > 0
            sess_before = sess
            sess = {"agent": agent, "turns": 0, "provider": provider, **({"or_model": sess_before["or_model"]} if sess_before.get("or_model") else {})}
            self.sessions[skey] = sess
            self.save()
            self.r.timeline(role, "Starting a fresh session", f"{_label(agent)} ({role}): {truncate(reason, 160)}")
            note = (f"ORCHESTRATOR · your previous session for this role broke ({truncate(reason, 300)}), so this is a new session. "
                    "Work already done is in the working tree: inspect it (git status, git diff) before acting, do not redo "
                    "finished work, and end with the appropriate protocol envelope.")
            if had_turns and self.cfg.get("token_prompt_deltas", True) and self.wt:
                # A later message alone lacks the rules and the task the lost session had; rebuild them from Relay's records.
                return self.handoff_prompt(role, agent, note, original_prompt, int(sess_before.get("context_tokens") or 0))
            return note + "\n\n" + original_prompt

        prompt, sess = self.maybe_compact(role, agent, sess, prompt, skey)
        original_prompt = prompt
        while True:
            prompt, sess = self.tools_turn_note(role, agent, sess, prompt)
            self.r.wait_if_paused()
            try:
                acfg = {**self.agent_cfg(), "attach_images": list(images)} if images else self.agent_cfg()
                if provider:
                    acfg = {**acfg, "_provider": provider}
                pinned = self.role_account(role)
                if pinned:
                    acfg = {**acfg, "_account": pinned}
                res = self.r.run_agent(role, agent, prompt, self.wt, acfg, model, sess, self.run_dir, label, turn,
                                       effort=self.role_effort(role))
            except Interrupted as e:
                if sess.get("id"):
                    sess["turns"] = int(sess.get("turns", 0)) + 1
                self.r.timeline("user", "Turn interrupted", f"{_label(agent)} ({role}) will restart with your note")
                prompt = protocol.interrupted_note(e.note)
                continue
            except TurnTimeout as e:
                timeouts += 1
                if sess.get("id"):
                    sess["turns"] = int(sess.get("turns", 0)) + 1
                self.r.msg(role=role, agent=agent, kind="error", content=str(e), turn=turn)
                if timeouts > 3:
                    raise
                prompt = (protocol.timeout_note(int(self.cfg.get("agent_turn_timeout_minutes") or 0)) if timeouts == 1
                          else fresh_session(str(e)))
                continue
            sess = res["session"]
            self.sessions[skey] = sess
            self.guard_commits(role, agent)
            self.save()
            if not res.get("ok"):
                failures += 1
                self.r.msg(role=role, agent=agent, kind="error", content=truncate(res.get("error") or "Agent turn failed", 3000), turn=turn)
                if (failures >= 2 or _hard_quota(res.get("error") or "")) and _config_error(res.get("error") or ""):
                    # Retrying cannot fix a wrong model name or a missing sign-in; say what to change instead.
                    raise RuntimeError(f"{_label(agent)} ({role}) cannot run with this setup: {truncate(res.get('error') or '', 400)}\n"
                                       f"Fix the model or sign-in for {_label(agent)} (Agents page, or the task's team), then send a message or Resume.")
                if failures > max_failures:
                    raise RuntimeError(f"{_label(agent)} ({role}) failed {failures} times in a row, including on fresh sessions: {truncate(res.get('error') or '', 500)}")
                self.r.timeline(role, f"Agent turn failed · retrying ({failures}/{max_failures})", truncate(res.get("error") or "", 200))
                # Back off so rate limits and provider hiccups can clear: 10 s, 20 s, 40 s … capped at 3 minutes.
                wait = min(180, 10 * 2 ** (failures - 1))
                t0 = time.time()
                deadline = t0 + wait
                while time.time() < deadline:
                    self.r.check_stop()
                    time.sleep(1)
                try:
                    timing.span(self.m, self.tid, "backoff", t0, time.time())
                except Exception:
                    pass
                if failures % 2 == 0:
                    prompt = fresh_session(res.get("error") or "the turn failed")
                elif sess.get("id"):
                    prompt = ("ORCHESTRATOR · your previous turn ended with an error: "
                              + truncate(res.get("error") or "", 600)
                              + "\nContinue from the current working tree state and end with the appropriate protocol envelope.")
                continue
            env = protocol.envelope_from_result(res)
            if "review" in expect and (not env or env.get("type") != "review"):
                v = protocol.verdict_from_text(res.get("last_message") or res.get("text") or "")
                if v:
                    env = {"type": "review", "verdict": v, "summary": "(verdict parsed from text)", "findings": [],
                           "text": res.get("text")}
            if env and env.get("type") in expect:
                env["_text"] = res.get("text") or ""
                self.tool_request_from_envelope(role, agent, env)
                return res, env
            nudges += 1
            if nudges > int(self.cfg.get("envelope_retries") or 2):
                if restarts < 2:
                    nudges = 0
                    prompt = fresh_session("it did not return a valid protocol envelope")
                    continue
                raise RuntimeError(f"{_label(agent)} ({role}) did not return a valid protocol envelope after {nudges} attempts.")
            self.r.timeline(role, "Missing protocol envelope", f"Asking {_label(agent)} to restate its reply ({nudges})")
            prompt = protocol.nudge(role, expect)

    def guard_commits(self, role, agent):
        """Agents never commit (Relay does at delivery): undo any commit made during the turn, keeping its changes."""
        self.guard_related_commits(role, agent)
        expected = self.state.get("head")
        if not expected or not self.wt:
            return
        info = commitguard.rewind(self.wt, expected, self.branch)
        if not info:
            return
        if not info["rewound"]:
            self.r.timeline("git", f"{_label(agent)} ({role}) moved HEAD", f"Not undone: {info['reason']}")
            return
        if info["pushed"]:
            self.state["agent_pushed"] = info["from"]
        listed = "; ".join(info["commits"]) or f"{info['from'][:10]} (HEAD moved back)"
        self.r.timeline("git", "Agent commit undone", f"{_label(agent)} ({role}) committed: {truncate(listed, 240)} · changes kept in the working tree")
        self.r.msg(role="orchestrator", agent=None, kind="notice", turn=self.state.get("turn"),
                   content=f"{_label(agent)} ({role}) made commits during its turn ({truncate(listed, 400)}). Agents never commit, so Relay "
                           f"soft-reset them to {expected[:10]}: every change is still in the working tree and Relay commits it at delivery.")
        note = commitguard.supervisor_note(info, role)
        self.state["judge_note"] = (self.state.get("judge_note") + "\n\n" if self.state.get("judge_note") else "") + note

    # ------------------------------------------------------------ questions
    # Reasons that justify stopping to ask the owner under the "blocked" question policy.
    BLOCKING_REASONS = {"credentials", "access", "destructive", "irreversible", "contradiction", "human_only"}

    def decide_without_asking(self, asker_role, env, question, options):
        """Under the "blocked" policy, answer a non-blocking question on the owner's behalf.

        Returns the follow-up prompt, or None when the question really needs the human."""
        policy = str((self.task.get("workflow") or {}).get("question_policy") or self.cfg.get("question_policy") or "blocked").lower()
        if policy != "blocked":
            return None
        reason = str(env.get("blocking_reason") or "").strip().lower()
        if reason in self.BLOCKING_REASONS:
            return None
        labels = [(o.get("label") if isinstance(o, dict) else str(o)) for o in options]
        rec = str(env.get("recommended") or "").strip()
        pick = next((l for l in labels if rec and l and rec.lower() in l.lower()), None) \
            or next((l for l in labels if l and "recommend" in l.lower()), None) or (labels[0] if labels else "")
        agent, _ = self.role_agent(asker_role)
        decision = (f"Go with: {pick}." if pick else "Use your best judgement.")
        self.r.msg(role=asker_role, agent=agent, kind="question", content=question, options=options, answered=True,
                   answer=f"(decided by Relay) {decision} The owner is only interrupted when an agent is truly blocked; "
                          "they can redirect at any time.", turn=self.state.get("turn"))
        self.r.timeline(asker_role, "Decided without asking", truncate(f"{decision} · {question}", 200))
        return protocol.human_answer(question, (
            f"{decision} Relay's policy is to interrupt the owner only when you are truly blocked "
            "(credentials or access, a destructive or irreversible step, contradictory requirements, or a decision only a human can make). "
            "Otherwise choose the most reasonable option that fits the request and the repository, state it under an "
            "\"Assumptions\" heading in your next envelope, and continue. The owner may redirect you with guidance later."))

    def needs_design_research(self) -> bool:
        cfg = self.cfg
        if not cfg.get("design_research", True):
            return False
        tri = self.state.get("triage") or {}
        if tri:
            # Only an explicit redesign or new look: a bare "layout" or "visual" in a performance request is not design work.
            want, why = triage.research_wanted(tri)
            if (self.state.get("ceremony") or {}).get("research", {}).get("why") != why:
                self.ceremony("research", want, why)
            return want
        text = " ".join(str(self.task.get(k) or "") for k in ("name", "requirements", "template"))
        return "DESIGN_RESEARCH" in protocol.rule_names_for("supervisor", {**cfg, "lean_prompts": True}, text)

    def ask_human(self, asker_role, env):
        """Block until the human answers. Returns the follow-up prompt for the asker."""
        if env.get("add_repo"):
            return self.ask_add_repo(asker_role, env)
        question = env.get("question") or env.get("content") or "(no question text)"
        options = env.get("options") if isinstance(env.get("options"), list) else []
        decided = self.decide_without_asking(asker_role, env, question, options)
        if decided is not None:
            return decided
        agent, _ = self.role_agent(asker_role)
        if not self.allow_questions:
            self.r.msg(role=asker_role, agent=agent, kind="question", content=question, options=options, answered=True,
                       answer="(auto) Agent questions are disabled for this task; proceed with the safer option.", turn=self.state.get("turn"))
            return protocol.human_answer(question, "Questions to the human are disabled for this task. Choose the safer, reversible option and continue.")
        qid = new_id("q")
        qmsg = self.r.msg(role=asker_role, agent=agent, kind="question", qid=qid, content=question, options=options,
                          answered=False, turn=self.state.get("turn"))
        self.m.ask_user(self.tid, {"id": qid, "kind": "question", "from": asker_role, "agent": agent,
                                   "question": question, "options": options, "message_id": qmsg["id"], "time": now()})
        self.r.status("needs_input", f"{_label(agent)} ({asker_role}) has a question for you")
        self.m.notify("warning", f"{_label(agent)} needs your input", truncate(question, 140), self.tid, kind="needs_input")
        ans = self.r.wait_for_answer(qid)
        self.m.clear_pending(self.tid)
        self.r.msg_update(qmsg["id"], answered=True, answer=ans.get("text") or "")
        self.r.msg(role="user", agent=None, kind="user", content=ans.get("text") or "(no text)", to=asker_role,
                   reply_to=qid, turn=self.state.get("turn"))
        self.r.timeline("user", "Question answered", truncate(ans.get("text") or "", 200))
        self.apply_human_text(ans.get("text") or "")
        return protocol.human_answer(question, ans.get("text"))

    def supervisor_turn(self, prompt, label, turn=None):
        """Run the supervisor and resolve any questions to the human inline."""
        self.r.status(self.status_for("supervisor", turn), label)
        res, env = self.run_role("supervisor", prompt, label, turn)
        while env.get("type") == "question" and (env.get("to") or "user") == "user":
            follow = self.ask_human("supervisor", env)
            self.r.status(self.status_for("supervisor", turn), label)
            res, env = self.run_role("supervisor", follow, label, turn)
        return res, env

    def worker_turn(self, prompt, label, turn):
        self.r.status("implementing", f"{_label(self.role_agent('worker')[0])} is implementing · work package #{turn}")
        res, env = self.run_role("worker", prompt, label, turn)
        while env.get("type") == "question":
            to = env.get("to") or "supervisor"
            if to == "user":
                follow = self.ask_human("worker", env)
                self.r.status("implementing", f"Worker continuing · work package #{turn}")
                res, env = self.run_role("worker", follow, label, turn)
                continue
            # question for the supervisor
            q = env.get("question") or "(no question text)"
            self.handoff("worker", "supervisor", "Question for the supervisor", q, subtype="question")
            sup_agent, _ = self.role_agent("supervisor")
            wrk_agent, _ = self.role_agent("worker")
            self.r.status("reviewing", f"{_label(sup_agent)} is answering the worker")
            _, senv = self.supervisor_turn(protocol.supervisor_worker_question(_label(wrk_agent), q), f"Answer worker · #{turn}", turn)
            if senv.get("type") == "decision" and senv.get("decision") == "done":
                # Supervisor decided the task is complete despite the open question.
                return res, {"type": "report", "status": "complete", "summary": senv.get("summary", ""),
                             "report": "(supervisor closed the work package while answering a question)", "_text": ""}
            answer = senv.get("instruction") or senv.get("summary") or senv.get("_text") or ""
            self.handoff("supervisor", "worker", "Answer from the supervisor", answer, subtype="answer")
            self.r.status("implementing", f"Worker continuing · work package #{turn}")
            res, env = self.run_role("worker", protocol.worker_followup(_label(sup_agent), turn, answer, self.take_guidance("worker"), "answer"), label, turn)
        return res, env

    # ------------------------------------------------------------ verification
    def prepare_environment(self, t):
        """Install the repository's dependencies before any agent starts, so nobody improvises one."""
        cmd = environment.setup_command(t, self.cfg, self.wt)
        info = {"command": cmd, "ok": None, "skipped": False, "duration": 0, "screenshot": environment.screenshot_tool()}
        info["system_packages"] = self.ensure_system_packages()
        if self.wt and environment.is_python(self.wt):
            environment.exclude_python_artifacts(self.wt)  # also when a saved or custom setup replaces detection
        restored = None
        if cmd and self.cfg.get("deps_cache", True) and not (t.get("workflow") or {}).get("setup_command"):
            try:
                restored = verifyfast.deps_restore(t.get("repo") or "", self.wt)
            except Exception:
                restored = None
            if restored:
                self.r.timeline("system", "Dependencies reused", f"node_modules from an earlier task with the same lockfile · {restored['seconds']}s")
                info["deps_cache"] = restored
        if not cmd:
            info["skipped"] = True
        elif environment.already_prepared(self.wt) and not (t.get("workflow") or {}).get("setup_command"):
            info.update(skipped=True, note="dependencies reused from an earlier task (same lockfile)" if restored
                        else "dependencies already present in the worktree")
        else:
            self.r.status("preparing", f"Preparing the environment · {cmd}")
            self.r.timeline("system", "Preparing environment", cmd)
            timeout = float(self.cfg.get("env_prepare_timeout_minutes") or 20) * 60
            res = self.r.run_shell(cmd, self.wt, "setup", timeout=timeout, title=f"Prepare environment · {cmd}")
            info.update(ok=res["ok"], duration=round(res.get("duration") or 0, 1),
                        output=truncate(res.get("output") or "", 1500, tail=True) if not res["ok"] else "")
            if res["ok"]:
                self.r.timeline("system", "Environment ready", f"{cmd} · {round(res.get('duration') or 0)}s")
                if self.cfg.get("deps_cache", True):
                    try:
                        verifyfast.deps_save(t.get("repo") or "", self.wt, int(self.cfg.get("deps_cache_keep") or 3))
                    except Exception:
                        pass
            else:
                # Not fatal: the team implements without dependencies and the gap is visible to everyone.
                self.record_blocked([{"check": f"Environment setup ({cmd})", "action_required": False,
                                      "reason": truncate((res.get("output") or "").strip().splitlines()[-1] if (res.get("output") or "").strip() else f"exit {res.get('rc')}", 300),
                                      "impact": "tests, builds and previews that need dependencies cannot run"}])
                self.r.timeline("system", "Environment setup failed", cmd)
        if self.repo_env.get("services_up"):
            self.start_services()
        self.m.set_meta(self.tid, environment=info)
        return info

    def ensure_system_packages(self):
        """apt packages the repository environment lists (unixodbc for pyodbc …), installed before its own setup."""
        from . import syspkgs
        pkgs = (getattr(self, "repo_env", None) or {}).get("system_packages") or []
        if not pkgs:
            detected = syspkgs.detect(self.wt) if self.wt else []
            if detected:
                # Not installed without the operator's say-so, but visible before a test run fails on it.
                self.r.timeline("system", "System packages suggested",
                                "; ".join(f"{d['dependency']} needs {' '.join(d['packages'])}" for d in detected)
                                + " · add them under Repositories → Environment → System packages")
            return {"packages": [], "suggested": [p for d in detected for p in d["packages"]]}
        timeout = float(self.cfg.get("env_prepare_timeout_minutes") or 20) * 60
        res = syspkgs.ensure(pkgs, lambda c, to: self.r.run_shell(c, self.wt, "setup", timeout=to, title=f"Install system packages · {' '.join(pkgs)}"),
                             timeout)
        if res["ok"] and res["installed"]:
            self.r.timeline("system", "System packages installed", " ".join(res["installed"]))
        elif not res["ok"]:
            self.record_blocked([{"check": f"System packages ({' '.join(res['missing'] or pkgs)})", "action_required": True,
                                  "reason": truncate(res["reason"], 300),
                                  "impact": "code that loads these libraries (imports, native builds, tests) fails until they are installed"}])
            self.r.timeline("system", "System packages could not be installed", truncate(res["reason"], 200))
        return {k: res[k] for k in ("ok", "packages", "installed", "missing", "skipped", "reason")}

    def start_services(self):
        cmd = self.repo_env["services_up"]
        self.r.timeline("system", "Starting services", cmd)
        res = self.r.run_shell(cmd, self.wt, "setup", timeout=float(self.cfg.get("env_prepare_timeout_minutes") or 20) * 60,
                               title=f"Start services · {cmd}")
        if not res["ok"]:
            self.record_blocked([{"check": f"Services ({cmd})", "action_required": True,
                                  "reason": truncate((res.get("output") or "").strip().splitlines()[-1] if (res.get("output") or "").strip() else f"exit {res.get('rc')}", 300),
                                  "impact": "checks that need these services cannot pass"}])
        self.state["services_started"] = res["ok"]
        return res

    def stop_services(self):
        self.stop_related_services()
        cmd = (getattr(self, "repo_env", None) or {}).get("services_down")
        if not cmd or not self.wt or not self.state.get("services_started"):
            return
        try:
            self.r.run_shell(cmd, self.wt, "setup", timeout=300, title=f"Stop services · {cmd}")
        except Exception:
            pass  # stopping services must never mask the task's own outcome
        self.state["services_started"] = False

    def env_text(self):
        info = self.state.get("environment") or (self.task_meta().get("environment") or {})
        lines = []
        if info.get("command"):
            state = "installed" if info.get("ok") else ("already present" if info.get("skipped") else "FAILED, see blocked checks")
            lines.append(f"- Dependencies: Relay ran `{info['command']}` before you started ({state}). Do not install dependencies another way.")
        described = repo_env.describe(getattr(self, "repo_env", None) or repo_env.empty())
        if described:
            lines.append(described)
        described = connectors.describe(getattr(self, "connector_names", None) or [], self.run_dir / "screenshots")
        if described:
            lines.append(described)
        if self.stack:
            lines.append(stacks.describe({**self.task, "id": self.tid}, self.stack))
        if self.wt and environment.venv_bin(self.wt):
            lines.append("- Python: Relay created `.venv` in the worktree with the requirements and pytest, and it is first on PATH, "
                         "so `python`, `pip` and `python -m pytest` already use it. Do not create another environment.")
        dev = environment.dev_command(self.wt) if self.wt else ""
        if dev:
            lines.append(f"- Run the app with `{dev}` from the worktree (pick a free port; stop it before you report).")
        if info.get("screenshot"):
            lines.append("- A headless browser is available to look at UI you build: "
                         "`relay-screenshot <url> <out.png> --width 1440 --height 900 --theme light|dark [--full]` "
                         "(also prints console errors and failed requests). Save screenshots under the run folder, not the repository: "
                         f"{self.run_dir / 'screenshots'}")
            lines.append("- To prove an interaction works, drive the running app like a user: `relay-browse <url> steps.json --out "
                         f"{self.run_dir / 'browse'}` with steps such as "
                         '[{"click":"#pick"},{"click":{"x":640,"y":360}},{"frame":"iframe#map"},{"click":".vessel"},{"frame":null},'
                         '{"expect":{"js":"…state changed…"}},{"screenshot":"after"}]. '
                         "The report lists each step, recorded values, the real postMessage payloads between frames/components, "
                         "and console errors. Paste the relevant part as evidence.")
        if perfcheck.applies(self):
            lines.append(perfcheck.env_line(self.run_dir))
        return "\n".join(lines)

    def forbidden_terms_file(self):
        """Terms live in Relay's settings and its run folder, never in the repository the agents work on."""
        terms = [t.strip() for t in self.cfg.get("design_forbidden_terms") or [] if str(t).strip()]
        path = self.run_dir / "forbidden-terms.txt"
        write_text(path, "\n".join(terms) + ("\n" if terms else ""))
        return path, terms

    def gate_command(self):
        if not self.design_gate or not self.base:
            return ""
        path, _ = self.forbidden_terms_file()
        return f"python3 {APP_DIR / 'orchestrator' / 'designcheck.py'} --base {self.base} --forbid-file {path}"

    def run_design_gate(self):
        _, terms = self.forbidden_terms_file()
        started = time.time()
        result = designcheck.check(self.wt, self.base, terms)
        text = designcheck.report_text(result)
        self.m.set_meta(self.tid, design_gate={"ok": result["ok"], "errors": len(result["errors"]), "warnings": len(result["warnings"]),
                                               "findings": (result["errors"] + result["warnings"])[:80], "time": now()})
        return {"command": "Design gate", "ok": result["ok"], "rc": 0 if result["ok"] else 1, "skipped": False,
                "duration": round(time.time() - started, 1)}, text

    def baseline_result(self, cmd, timeout, repo=None, base=None, wt=None, key=None):
        """Run a failing command once on the commit the task started from.

        A check that already failed before the team touched anything (a flaky suite, a host-specific test)
        must not block delivery or send the supervisor round in circles. Results are cached per command.
        """
        cache = self.state.setdefault("baseline", {})
        key = key or cmd
        if key in cache:
            return cache[key]
        base, wt = base or self.base, wt or self.wt
        if not base or not wt:
            return None
        repo = repo or self.task.get("repo")
        known = verifyfast.baseline_get(str(repo), base, cmd)
        if known:
            # Another task already ran this command on this exact commit.
            cache[key] = {"ok": known.get("ok"), "rc": known.get("rc"), "cached": True}
            self.save()
            return cache[key]
        path = Path(str(wt) + "-baseline")
        gitops.remove_worktree(repo, path)
        add = quiet(["git", "worktree", "add", "--detach", str(path), base], cwd=repo, timeout=300)
        if add.returncode != 0:
            return None
        try:
            # Share the prepared dependencies; the baseline only needs to run the same check.
            if (Path(wt) / "node_modules").is_dir() and not (path / "node_modules").exists():
                (path / "node_modules").symlink_to(Path(wt) / "node_modules", target_is_directory=True)
            gitops.copy_ignored_root_files(repo, path)
            self.r.timeline("verify", "Checking the failure on the starting commit", cmd)
            res = self.r.run_shell(cmd, path, "verify", timeout=timeout, title=f"Baseline · {cmd}")
            cache[key] = {"ok": res["ok"], "rc": res.get("rc")}
            if res.get("rc") not in (-1, 126, 127):
                verifyfast.baseline_put(str(repo), base, cmd, cache[key])
        finally:
            gitops.remove_worktree(repo, path)
        self.save()
        return cache[key]

    def run_verification(self, quick=False):
        """Relay's own checks. `quick` (after each work package) defers builds to the final run."""
        n_stack = len((self.stack or {}).get("checks") or [])
        perf = perfcheck.applies(self)
        if not self.verify_cmds and not self.design_gate and not n_stack and not perf and not any(r.get("verify_commands") for r in self.related):
            return ""
        prev = getattr(self, "_phase", None)
        self.phase_mark("verify")
        try:
            return self._run_verification(quick)
        finally:
            if prev and prev != "verify":
                self.phase_mark(prev)

    def _verify_command(self, c, res, timeout, quiet_pass, fail_chars):
        """(item, text) for one finished command: baseline check, blocked records and the verdict line."""
        # pytest exit 5 means no tests were collected: nothing failed, so do not send the team chasing it.
        skipped = res.get("rc") == 5 and "pytest" in c
        if skipped:
            res["ok"] = True
        pre_existing = False
        # 126/127: the command itself could not run (not installed, not executable). That proves nothing
        # either way, so it is never excused as a pre-existing failure and is recorded for a human to fix.
        cannot_run = res.get("rc") in (126, 127)
        if cannot_run:
            self.record_blocked([{"check": c, "action_required": True,
                                  "reason": truncate((res.get("output") or "").strip().splitlines()[-1] if (res.get("output") or "").strip() else f"exit {res.get('rc')}", 300),
                                  "impact": "this check could not run, so it proves nothing; fix the environment (Repositories → Environment)"}])
        elif not res["ok"]:
            base = self.baseline_result(c, timeout)
            pre_existing = bool(base and not base["ok"])
        not_run = "" if res["ok"] or cannot_run else suite_not_run_reason(c, res)
        if not_run:
            # Excused or not, a suite that stopped while collecting proved nothing: say so where people look.
            from . import syspkgs
            hint = syspkgs.missing_library_hint(res.get("output") or "")
            self.record_blocked([{"check": c, "action_required": bool(hint),
                                  "reason": truncate(not_run + (f"; {hint}" if hint else ""), 400),
                                  "impact": "the test suite did not run, so this check proves nothing about the change"}])
        optional = c in getattr(self, "optional_checks", set())
        item = {"command": c, "ok": res["ok"] or pre_existing or optional, "rc": res.get("rc"), "skipped": skipped,
                "pre_existing": pre_existing, "optional": optional, "passed": res["ok"],
                "duration": round(res.get("duration") or 0, 1)}
        verdict = ("SKIPPED (no tests collected)" if skipped else "PASS" if res["ok"]
                   else "PRE-EXISTING FAILURE, THE SUITE DID NOT RUN (also fails on the starting commit; proves nothing)" if pre_existing and not_run
                   else "PRE-EXISTING FAILURE (also fails on the starting commit; does not block)" if pre_existing
                   else "FAIL (optional check; reported, does not block)" if optional else "FAIL")
        head = f"$ {c}\n{verdict} (exit {res.get('rc')}, {round(res.get('duration') or 0)}s)"
        # A passing command only needs its verdict; a failure needs output the agents can act on.
        body = "" if (res["ok"] and quiet_pass) else "\n" + (
            tokens.failure_excerpt(res["output"], fail_chars) if self.cfg.get("token_failure_excerpts", True)
            else truncate(res["output"], fail_chars, tail=True))
        return item, head + body

    def _run_verification(self, quick=False):
        n_stack = len((self.stack or {}).get("checks") or [])
        perf = perfcheck.applies(self)
        cmds = list(self.verify_cmds)
        # The same tree and the same checks were already verified: reuse the results instead of paying for them again.
        fp = ""
        if self.cfg.get("verify_reuse_results", True):
            fp = tokens.digest(self.fingerprint() + "|" + "\n".join(cmds) + f"|{bool(self.design_gate)}|{n_stack}")
            cached = self.state.get("verify_cache") or {}
            if cached.get("fp") == fp and cached.get("vt") and (quick or not cached.get("quick")):
                self.r.timeline("verify", "Verification reused", "nothing changed since these checks last ran")
                self.m.set_meta(self.tid, verification={**(cached.get("meta") or {}), "reused": True, "time": now()})
                return cached["vt"]
        deferred, unaffected = [], []
        builds = [c for c in cmds if verifyfast.is_build(c)]
        if builds and quick:
            deferred = builds
        elif builds and self.cfg.get("verify_skip_unaffected_build", True):
            need, _ = verifyfast.build_needed([c["path"] for c in gitops.changed_files(self.wt, self.base)])
            if not need:
                unaffected = builds
        run_cmds = [c for c in cmds if c not in deferred and c not in unaffected]
        self.r.status("verifying", f"Running {len(run_cmds) + (1 if self.design_gate else 0) + n_stack} verification check(s)")
        # Checks such as a production build may rewrite tracked files (version stamps, generated maps).
        # Remember what was already changed so anything the checks alone touched is put back afterwards.
        before = {line[3:] for line in quiet(["git", "status", "--porcelain"], cwd=self.wt).stdout.splitlines() if line.strip()}
        self.r.timeline("verify", "Verification", ", ".join(run_cmds + (["design gate"] if self.design_gate else []))
                        + (f" · build skipped: {', '.join(unaffected)} (only tests, docs or specs changed)" if unaffected else "")
                        + (f" · deferred to the final check: {', '.join(deferred)}" if deferred else ""))
        items = []
        parts = []
        timeout = float(self.cfg.get("verification_timeout_minutes") or 20) * 60
        quiet_pass = bool(self.cfg.get("budget_verify_pass_quiet", True))
        fail_chars = int(self.cfg.get("budget_verify_chars") or 3500)
        results = {}
        for group in verifyfast.plan_groups(run_cmds, bool(self.cfg.get("verify_parallel", True))):
            results.update(verifyfast.run_group(group, lambda c: self.r.run_shell(c, self.wt, "verify", timeout=timeout)))
        for c in cmds:
            if c in deferred or c in unaffected:
                why = ("DEFERRED (the build runs once in the final verification)" if c in deferred
                       else "SKIPPED (only tests, docs or specs changed; nothing the build reads)")
                items.append({"command": c, "ok": True, "rc": 0, "skipped": True, "duration": 0.0,
                              "note": "deferred" if c in deferred else "no build input changed"})
                parts.append(f"$ {c}\n{why}")
                continue
            item, text = self._verify_command(c, results[c], timeout, quiet_pass, fail_chars)
            items.append(item)
            parts.append(text)
        if self.stack:
            # End-to-end checks against the task's integration stack; no baseline: the stack is built from this branch.
            stack_items, stack_parts = stacks.pipeline_verify(self)
            items += stack_items
            parts += stack_parts
        if perf:
            # Performance tasks: re-measure the worktree against the baseline (orchestrator/perfcheck.py).
            perf_items, perf_parts = perfcheck.pipeline_verify(self)
            items += perf_items
            parts += perf_parts
        if self.design_gate and self.base:
            item, text = self.run_design_gate()
            items.append(item)
            # Every violation is listed: the gate is only useful if the team can fix each line it names.
            parts.append(text)
        touched = [line[3:].strip('"') for line in quiet(["git", "status", "--porcelain"], cwd=self.wt).stdout.splitlines()
                   if line[:2].strip() in ("M", "D") and line[3:] not in before]
        if touched:
            quiet(["git", "checkout", "--", *touched], cwd=self.wt)
            self.r.timeline("verify", "Restored files the checks rewrote", ", ".join(touched[:10]))
            verifyfast.generated_add(self.task.get("repo") or "", touched)
        if self.related:
            # Each other repository of a multi-repository task runs its own checks in its own worktree.
            more_items, more_parts = self.verify_related(timeout, quiet_pass, fail_chars)
            items += more_items
            parts += more_parts
        vt = "\n\n".join(parts)
        all_ok = all(i["ok"] for i in items)
        self.artifact("verification", "VERIFICATION.md", vt)
        meta = {"ok": all_ok, "items": items, "time": now(), **({"quick": True} if quick else {})}
        self.m.set_meta(self.tid, verification=meta)
        self.r.msg(role="verify", agent=None, kind="verification", ok=all_ok, items=items, turn=self.state.get("turn"))
        self.r.timeline("verify", "Verification passed" if all_ok else "Verification failed",
                        f"{sum(1 for i in items if i['ok'])}/{len(items)} commands passed")
        if fp:
            self.state["verify_cache"] = {"fp": fp, "vt": vt, "meta": meta, "quick": bool(quick)}
            self.save()
        return vt

    def restore_generated(self):
        """Files Relay's checks are known to rewrite (build stamps): an agent's own build leaves them changed; put them back."""
        known = verifyfast.generated_get(self.task.get("repo") or "")
        if not known or not self.wt:
            return
        status = {line[3:].strip('"'): line[:2] for line in quiet(["git", "status", "--porcelain"], cwd=self.wt).stdout.splitlines() if line.strip()}
        dirty = [p for p in known if status.get(p, "").strip() in ("M",)]
        if dirty:
            quiet(["git", "checkout", "--", *dirty], cwd=self.wt)
            self.r.timeline("git", "Restored generated files", ", ".join(dirty[:6]) + " (build output an agent's own build rewrote)")

    # ------------------------------------------------------------ phases
    def prepare(self):
        t = self.task
        self.r.bind_log(self.run_dir / "raw.log")
        # An existing worktree plus a checkpoint is enough to continue. Agent sessions are a
        # bonus: if they were lost, the roles simply start a fresh session in the same worktree.
        resumable = bool(self.state) and t.get("worktree") and Path(t["worktree"]).exists()
        if resumable:
            self.wt = Path(t["worktree"])
            self.branch = t.get("branch")
            self.r.status("running", "Resuming from checkpoint")
            self.r.timeline("system", "Resuming from checkpoint",
                            f"phase {self.state.get('phase')} · work package {self.state.get('turn')} · awaiting {self.state.get('awaiting')}")
            self.state["resumed"] = True
            if self.state.pop("budget_exhausted", False):
                # Resuming a task that stopped at its work-package budget means "keep going": grant the extension.
                ext = max(1, int(self.cfg.get("judge_budget_extension") or 3))
                need = int(self.state.get("turn") or 1) + ext - 1 - self.max_turns
                self.state["extra_turns"] = max(int(self.state.get("extra_turns") or 0), need)
                self.r.timeline("judge", "Work-package budget extended on resume", f"{ext} more work packages")
        else:
            self.state = {"phase": "kickoff", "turn": 0, "review_round": 0, "awaiting": "supervisor"}
            self.sessions = {}
            self.r.status("running", "Preparing isolated worktree")
            if int(t.get("runs") or 1) > 1:
                self.r.msg(role="orchestrator", agent=None, kind="notice", turn=0,
                           content=f"Run {t['runs']} · retried from scratch. Everything above belongs to earlier runs.")
            self.r.timeline("system", "Task started", t["name"])
            self.phase_mark("setup")
            self.wt, self.branch = gitops.create_worktree(self.r, t, self.cfg, self.run_dir)
            self.r.timeline("git", "Worktree ready", f"{self.branch} → {self.wt}")
            # Recorded before any agent works, so changes still count once Relay commits them.
            t["base_commit"] = quiet(["git", "rev-parse", "HEAD"], cwd=self.wt).stdout.strip()
            self.repo_env = repo_env.load(t["repo"]) if t.get("repo") else repo_env.empty()
            self.r.set_masker(repo_env.masker(self.repo_env))
            self.state["environment"] = self.prepare_environment(t)
        self.repo_env = repo_env.load(t["repo"]) if t.get("repo") else repo_env.empty()
        self.r.set_masker(repo_env.masker(self.repo_env))
        if resumable and self.repo_env.get("system_packages") and self.state.get("phase") not in ("done", "delivered"):
            self.ensure_system_packages()  # a recreated container loses what apt installed; dpkg makes this a no-op otherwise
        if resumable and self.repo_env.get("services_up") and self.state.get("phase") != "delivered":
            self.start_services()
        stacks.pipeline_prepare(self)
        self.base = t.get("base_commit") or gitops.base_commit(self.wt, t.get("repo"))
        repo_full = t.get("github_repo") or github.remote_repo_name(t.get("repo"))
        self.m.set_meta(self.tid, worktree=str(self.wt), branch=self.branch, run_dir=str(self.run_dir), github_repo=repo_full, base_commit=self.base)
        if not self.state.get("head"):
            # The commit agents work on top of; anything they commit after it is undone (guard_commits).
            self.state["head"] = commitguard.head(self.wt)
        self.repo_full = repo_full
        self.attach_related(fresh=not resumable)

        if t.get("issue"):
            try:
                data = github.issue_view(t["issue"], self.wt)
                self.issue_text = f"#{data.get('number')} {data.get('title')}\n{data.get('url','')}\n\n{data.get('body','')}"
                write_text(self.run_dir / "ISSUE.json", json.dumps(data, indent=2))
                self.m.set_meta(self.tid, github_issue_number=data.get("number"), github_issue_title=data.get("title"), github_issue_url=data.get("url"))
            except Exception as e:
                self.r.timeline("github", "Could not load issue", str(e)[:200])
                self.issue_text = f"Issue #{t['issue']} (details unavailable: {e})"

        refs, _ = stage_refs(self.wt, self.run_dir, t.get("attachments") or [], self.tid)
        self.refs_text = context_refs(refs)

        wf = t.get("workflow") or {}
        cmds = list(wf.get("verification_commands") or self.cfg.get("verification_commands") or [])
        saved_checks = self.repo_env.get("checks") or []
        self.optional_checks = {c["command"] for c in saved_checks if not c.get("required", True)}
        if saved_checks and not wf.get("verification_commands"):
            # The repository's own test profile replaces guessing.
            cmds = [c["command"] for c in saved_checks]
        elif wf.get("auto_detect_verification", self.cfg.get("auto_detect_verification", True)):
            for c in gitops.detect_verify(self.wt):
                if c not in cmds:
                    cmds.append(c)
        self.verify_cmds = [] if self.verify_mode == "off" else cmds
        self.m.set_meta(self.tid, verify_commands=self.verify_cmds)
        # Approved lessons from earlier tasks on this repository, added to the kickoff prompts.
        self.lessons_text = lessons.kickoff_block(self.m, {**t, "github_repo": repo_full}, self.cfg)
        self.learning_start({**t, "github_repo": repo_full})
        self.knowledge_start({**t, "github_repo": repo_full})

    def knowledge_start(self, t):
        """The Knowledge section (paths and one line per doc) every kickoff prompt of this task carries (knowledge.py)."""
        try:
            self.knowledge_text = knowledge.task_block(t, self.cfg)
        except Exception:  # knowledge must never stop a task
            self.knowledge_text = ""
        if self.knowledge_text:
            self.m.set_meta(self.tid, knowledge_docs=[line[2:].split(":", 1)[0] for line in self.knowledge_text.splitlines()[1:] if line.startswith("- /")])

    def learning_start(self, t):
        """Prompt/rule versions for the outcome dataset, and the repository's playbook for planning (learning_engine.py)."""
        engine = getattr(getattr(self.m, "learning", None), "engine", None)
        if not engine:
            return
        try:
            names = sorted({n for r in ("supervisor", "worker", "reviewer") for n in protocol.rule_names_for(r, self.cfg, t.get("requirements") or "")})
            engine.task_started(self.tid, names)
            self.playbook_text = engine.playbook_for_task(t)
        except Exception:  # learning must never stop a task
            self.playbook_text = ""

    def kickoff(self):
        self.phase_mark("plan")
        sup_agent, _ = self.role_agent("supervisor")
        self.handoff("orchestrator", "supervisor", "Task briefing", self.task.get("requirements", ""),
                     subtype="briefing", issue=self.issue_text[:400] if self.issue_text else "")
        guidance = self.take_guidance("supervisor")
        prompt = protocol.supervisor_kickoff(self.task, self.wt, self.branch, self.issue_text, self.refs_text, guidance, self.verify_cmds, self.cfg,
                                             self.env_text(), self.tools_text("supervisor"))
        prompt = protocol.with_lessons(prompt, self.lessons_text)
        prompt = protocol.with_block(prompt, self.knowledge_text, "knowledge")
        prompt = protocol.with_block(prompt, self.playbook_text)
        prompt = protocol.with_block(prompt, self.context_text("supervisor"))
        prompt = protocol.with_block(prompt, self.kickoff_design_note())
        prompt = protocol.with_block(prompt, perfcheck.kickoff_note(self))
        prompt = protocol.with_block(prompt, self.solo_handoff_note())
        res, env = self.supervisor_turn(prompt, f"{_label(sup_agent)} is inspecting the repository and planning", turn=0)
        if env.get("type") != "plan":
            if env.get("type") in ("instruction", "decision") and env.get("instruction"):
                env = {"type": "plan", "summary": env.get("summary", ""), "plan": env.get("summary", "") or "(plan given inline)",
                       "acceptance": [], "instruction": env["instruction"]}
            else:
                raise RuntimeError("The supervisor did not produce a plan envelope.")
        criteria = judge.normalize_acceptance(env.get("acceptance"))
        if not criteria:
            # One chance to state the contract; without it the task runs in legacy mode (no evidence gate).
            self.r.timeline("supervisor", "Plan has no acceptance contract", "Asking the supervisor to add one")
            _, env2 = self.supervisor_turn(
                "ORCHESTRATOR · your plan has no `acceptance` list. Delivery is gated on it. Reply with the same plan envelope including "
                '"acceptance":[{"id":"A1","criterion":"…","how_to_verify":"test: …","required":true}] (2 to 6 criteria from the requirements).',
                f"{_label(sup_agent)} is adding acceptance criteria", turn=0)
            if env2.get("type") == "plan":
                env = {**env, **{k: v for k, v in env2.items() if v}}
                criteria = judge.normalize_acceptance(env.get("acceptance"))
        if self.needs_design_research() and len(env.get("design_research") or []) < 3:
            # Design work starts from researched principles, not the first idea (rules/DESIGN_RESEARCH.md).
            self.r.timeline("supervisor", "Design research missing", "Asking the supervisor to research before designing")
            _, env3 = self.supervisor_turn(
                "ORCHESTRATOR · this is design work, and your plan has no design research. Before any design: search the web "
                "(you have web search) for current UI/UX practice relevant to this screen (usability heuristics, Laws of UX, WCAG 2.2 AA, "
                "the fitting platform guidelines, and how established products solve the same thing). Then reply with the same plan "
                'envelope plus "design_research":[{"principle":"…","applies_how":"…","source":"url or name"}] (5 to 10 items) and '
                '"assumptions":["…"]. The repository\'s own design rules and tokens take precedence over anything you find.',
                f"{_label(sup_agent)} is researching design principles", turn=0)
            if env3.get("type") == "plan":
                env = {**env, **{k: v for k, v in env3.items() if v}}
        research = [r for r in (env.get("design_research") or []) if isinstance(r, (dict, str))]
        if research:
            lines = [f"- **{r.get('principle', '')}**: {r.get('applies_how', '')}" + (f" ({r.get('source')})" if r.get("source") else "")
                     if isinstance(r, dict) else f"- {r}" for r in research]
            self.artifact("design_research", "DESIGN_RESEARCH.md", "# Design research\n\n" + "\n".join(lines) + "\n")
            self.r.timeline("supervisor", "Design research", f"{len(research)} principles")
        assumptions = [str(a) for a in (env.get("assumptions") or []) if str(a).strip()]
        if assumptions:
            self.r.msg(role="supervisor", agent=sup_agent, kind="notice", turn=0,
                       content="**Assumptions** (decided without asking you; redirect any time with a message)\n" + "\n".join(f"- {a}" for a in assumptions))
        plan = {"summary": env.get("summary", ""), "plan": env.get("plan", ""), **protocol.normalize_packet(env)}
        if research:
            plan["design_research"] = research[:12]
        if assumptions:
            plan["assumptions"] = assumptions[:12]
        plan["acceptance"] = judge.criterion_texts(criteria) or plan["acceptance"]
        plan["system_design"] = multirepo.normalize_design(env.get("system_design"))
        plan["work_packages"] = multirepo.normalize_packages(env.get("work_packages"))
        self.state["plan"] = plan
        if plan["system_design"]:
            self.m.set_meta(self.tid, system_design=plan["system_design"])
            self.artifact("design", "SYSTEM_DESIGN.md", multirepo.design_md(plan["system_design"]))
        self.artifact("plan", "PLAN.md", plan["plan"] + "\n\n## Context packet\n\n```\n" + protocol.packet_block({**plan, "criteria": criteria}) + "\n```\n"
                      + ("\n" + multirepo.design_block(plan["system_design"], plan["work_packages"]) + "\n" if plan["work_packages"] or plan["system_design"] else ""))
        self.m.set_meta(self.tid, plan=plan)
        # Criteria the human added before the run started are kept next to the supervisor's.
        user_rows = [c for c in self.criteria() if c.get("source") == "user"]
        if user_rows:
            criteria = judge.edit_acceptance(criteria, {"add": user_rows})
        if criteria:
            self.set_criteria(criteria)
        else:
            self.artifact("acceptance", "ACCEPTANCE.md", "\n".join(f"- [ ] {a}" for a in plan["acceptance"]))
        self.r.msg(role="supervisor", agent=sup_agent, kind="plan", summary=plan["summary"], content=plan["plan"],
                   acceptance=plan["acceptance"], requirements=plan["requirements"], optional=plan["optional"],
                   known_files=plan["known_files"], findings=plan["findings"], constraints=plan["constraints"],
                   unknowns=plan["unknowns"], turn=0)
        self.r.timeline("supervisor", "Plan agreed", plan["summary"] or f"{len(plan['acceptance'])} acceptance criteria")
        self.state.update({"phase": "dialogue", "turn": 1, "awaiting": "worker",
                           "instruction": self.package_prefix(env) + (env.get("instruction") or "Implement the plan."), "instruction_kind": "instruction",
                           "instruction_summary": env.get("summary", "")})
        self.save()
        perfcheck.after_plan(self, env)  # performance tasks: baseline measurement of the starting commit
        self.maybe_enter_design(env)   # complex or multi-repository tasks design first (orchestrator/design.py)

    def dialogue(self):
        sup_agent, _ = self.role_agent("supervisor")
        wrk_agent, _ = self.role_agent("worker")
        while self.state.get("phase") == "dialogue":
            self.r.check_stop()
            turn = int(self.state.get("turn") or 1)
            if self.state.get("awaiting") == "worker" and self.state.get("deliver_now"):
                # The owner said to deliver: no more packages; Relay's checks still run before the pull request.
                self.r.timeline("judge", "Delivering now", "the owner asked for the pull request; remaining work becomes follow-ups")
                self.add_followups([{"severity": "should_fix", "problem": f"Not done when the owner asked to deliver: {self.state.get('instruction_summary') or truncate(self.state.get('instruction') or '', 200)}"}],
                                   "delivered on request")
                self.state["last_verification"] = self.run_verification()
                self.state["phase"] = "deliver"
                self.save()
                return
            if self.state.get("awaiting") == "worker":
                self.phase_mark("build")
                if turn > self.turn_limit():
                    if not self.budget_escalation(turn):
                        continue
                kind = self.state.get("instruction_kind") or "instruction"
                title = {"revise": f"Revision request · work package #{turn}", "question": f"Question for the worker · #{turn}"}.get(kind, f"Work package #{turn}")
                self.handoff("supervisor", "worker", title, self.state.get("instruction", ""), subtype=kind,
                             summary=self.state.get("instruction_summary", ""))
                guidance = self.take_guidance("worker")
                sess = self.sessions.get("worker") or {}
                if int(sess.get("turns", 0)) == 0 or sess.get("agent") != wrk_agent:
                    prompt = protocol.worker_kickoff(self.task, self.wt, self.branch, self.issue_text, self.refs_text,
                                                     {**(self.state.get("plan") or {}), "criteria": self.criteria()},
                                                     self.state.get("instruction", ""), guidance, self.cfg,
                                                     self.gate_command(), self.env_text(), self.tools_text("worker"))
                    prompt = protocol.with_lessons(prompt, self.lessons_text)
                    prompt = protocol.with_block(prompt, self.knowledge_text, "knowledge")
                    prompt = protocol.with_block(prompt, self.context_text("worker"))
                else:
                    prompt = protocol.worker_followup(_label(sup_agent), turn, self.state.get("instruction", ""), guidance, kind)
                if self.state.get("resumed"):
                    prompt = tokens.prepend(prompt, protocol.resume_note({k: v for k, v in self.state.items() if k in ("phase", "turn", "awaiting", "instruction_summary")}))
                    self.state["resumed"] = False
                res, env = self.worker_turn(prompt, f"Work package #{turn}", turn)
                self.restore_generated()
                self.note_package_report(env)
                self.note_amendments(env, "worker")
                report = {"status": env.get("status", "complete"), "summary": env.get("summary", ""),
                          "report": env.get("report") or env.get("_text", ""), "files": env.get("files") or [],
                          "blocked_checks": protocol.blocked_checks(env), "blockers": protocol.blockers(env)}
                self.record_blocked(report["blocked_checks"])
                # Only blockers the user alone can clear interrupt them; everything else is recorded and work goes on.
                for b in [b for b in report["blockers"] + report["blocked_checks"] if b.get("action_required")]:
                    what = b.get("check") or "Work"
                    follow = self.ask_human("worker", {"question": f"{what} is blocked: {b['reason']}" + (f"\nImpact: {b['impact']}" if b.get("impact") else "")
                                                        + "\nHow should the team proceed?",
                                                        "options": ["Continue without it", "I have fixed it, retry", "Stop the task"]})
                    report["report"] += "\n\n" + follow
                self.state["report"] = report
                self.artifact("implementation", "IMPLEMENTATION.md", f"# Work package #{turn} · {report['status']}\n\n{report['report']}")
                self.handoff("worker", "supervisor", f"Report · work package #{turn} · {report['status']}", report["report"],
                             subtype="report", status=report["status"], summary=report["summary"], files=report["files"][:60],
                             blocked_checks=report["blocked_checks"] + [{"check": "Implementation", **b} for b in report["blockers"]])
                self.r.timeline("worker", f"Work package #{turn} reported {report['status']}", report["summary"])
                self.state["last_verification"] = self.run_verification(quick=True) if self.verify_mode == "each_report" else ""
                self.state["awaiting"] = "supervisor"
                self.save()
                if kind == "revise" and self.state.get("fp_before"):
                    self.check_revision_changed()
                continue

            # supervisor evaluates the report
            changed = self.all_changed_files()
            ds = self.all_diffstat()
            self.m.set_meta(self.tid, diffstat=ds, changed_count=len(changed))
            guidance = self.take_guidance("supervisor")
            remaining = max(0, self.turn_limit() - turn)
            note = self.state.pop("judge_note", "")
            if note:
                guidance = (guidance + "\n\n" if guidance else "") + note
            vt, judge_text, brief = self.after_report_deltas()
            prompt = protocol.supervisor_after_report(_label(wrk_agent), turn, self.state.get("report"), (self.state.get("report") or {}).get("report", ""),
                                                      vt, changed, ds, guidance, remaining, self.cfg, judge_text, brief=brief)
            if self.state.get("resumed"):
                prompt = tokens.prepend(prompt, protocol.resume_note({k: v for k, v in self.state.items() if k in ("phase", "turn", "awaiting")}))
                self.state["resumed"] = False
            res, env = self.supervisor_turn(prompt, f"{_label(sup_agent)} is evaluating work package #{turn}", turn)
            self.apply_supervisor_decision(env, turn)

    def apply_supervisor_decision(self, env, turn):
        sup_agent, _ = self.role_agent("supervisor")
        typ = env.get("type")
        self.note_amendments(env, "supervisor")
        perfcheck.update_spec(self, env)
        if typ == "decision" and env.get("decision") == "done":
            self.add_followups(env.get("follow_ups") or env.get("followups"), "supervisor")
            gated = self.done_gate(env, turn)
            if gated is not None:
                return self.apply_supervisor_decision(gated, turn)
            self.state["pr_summary"] = env.get("pr_summary") or env.get("summary") or ""
            self.state["summary"] = env.get("summary") or ""
            self.r.msg(role="supervisor", agent=sup_agent, kind="decision", decision="done", summary=env.get("summary", ""),
                       content=self.state["pr_summary"], criteria=self.criteria(), turn=turn)
            self.r.timeline("supervisor", "Supervisor declared the task complete", env.get("summary", ""))
            # The design gate is enforced at done even when command verification is off.
            if ((self.verify_mode == "before_review" and (self.verify_cmds or perfcheck.applies(self))) or (self.design_gate and self.verify_mode != "each_report")
                    or (self.verify_mode == "each_report" and (self.task_meta().get("verification") or {}).get("quick"))):
                vt = self.run_verification()
                self.state["last_verification"] = vt
                if not (self.task_meta().get("verification") or {}).get("ok", True):
                    # One triage round only: a supervisor that declares done again over the same failure
                    # would otherwise loop forever, paying for a turn and a full build each time.
                    self.state["verify_triage"] = int(self.state.get("verify_triage") or 0) + 1
                    if self.state["verify_triage"] > 1:
                        self.save()
                        env2 = self.verify_escalation(vt, turn)
                        if env2 is not None:
                            return self.apply_supervisor_decision(env2, turn)
                        return self.finish_done()
                    fake = {"type": "review", "verdict": "FAIL", "summary": "Verification commands failed after the done decision.",
                            "findings": [{"severity": "blocking", "file": "(verification)", "problem": "One or more verification commands failed.", "fix": "Make the checks pass."}]}
                    self.r.timeline("verify", "Verification failed after done decision", "Sending results back to the supervisor")
                    ledger = judge.Ledger(self.state.get("ledger"))
                    fake["findings"] = ledger.observe(judge.normalize_findings(fake["findings"]), "verification")
                    self.state["ledger"] = ledger.dump()
                    self.state["pending_findings"] = [f["id"] for f in fake["findings"]]
                    _, env2 = self.supervisor_turn(protocol.supervisor_after_review("Orchestrator", fake, vt, 0, 0), "Supervisor triaging failed verification", turn)
                    return self.apply_supervisor_decision(env2, turn)
            return self.finish_done()
        if typ in ("instruction", "question") or (typ == "decision" and env.get("decision") == "revise"):
            kind = "revise" if typ == "decision" else ("question" if typ == "question" else "instruction")
            text = env.get("instruction") or env.get("question") or env.get("summary") or env.get("_text", "")
            text = self.package_prefix(env) + text
            if kind == "revise":
                judged = self.judge_revise(env, turn)
                if not judged.get("_judged"):
                    # A new envelope from the supervisor (after a nudge or a human decision): judge it from the top.
                    return self.apply_supervisor_decision(judged, turn)
                env, text = judged, judged.get("instruction") or text
            if kind == "revise":
                self.r.msg(role="supervisor", agent=sup_agent, kind="decision", decision="revise", summary=env.get("summary", ""),
                           content=text, addresses=env.get("addresses") or [], findings=env.get("_findings") or [], turn=turn)
                self.r.timeline("supervisor", "Revision requested", env.get("summary", ""))
            else:
                self.r.timeline("supervisor", "Next work package", env.get("summary", ""))
            self.state["verify_triage"] = 0
            self.state["gate_nudges"] = 0
            self.state["revise_nudges"] = 0
            self.state["fp_before"] = self.fingerprint() if kind == "revise" else ""
            self.state.update({"turn": turn + 1, "awaiting": "worker", "instruction": text, "instruction_kind": kind,
                               "instruction_summary": env.get("summary", ""), "phase": "dialogue"})
            self.save()
            return
        raise RuntimeError(f"Unexpected supervisor envelope: {typ}")

    def record_blocked(self, checks):
        """Keep one entry per check across work packages, so the reviewer and the task page see them all."""
        if not checks:
            return
        merged = {(c.get("check") or c.get("reason")): c for c in self.state.get("blocked_checks") or []}
        for c in checks:
            merged[c.get("check") or c.get("reason")] = c
        self.state["blocked_checks"] = list(merged.values())
        self.m.set_meta(self.tid, blocked_checks=self.state["blocked_checks"])
        self.r.timeline("worker", "Checks could not run", ", ".join(c.get("check") or c.get("reason") for c in checks))

    def task_meta(self):
        return self.m.store.get(self.tid) or {}

    # ------------------------------------------------------------ judge
    def turn_limit(self):
        return self.max_turns + int(self.state.get("extra_turns") or 0)

    def criteria(self):
        """The acceptance contract. Task meta is the source of truth, so a human edit is seen at the next decision."""
        return list(self.task_meta().get("acceptance") or [])

    def set_criteria(self, criteria):
        self.m.set_meta(self.tid, acceptance=criteria)
        self.artifact("acceptance", "ACCEPTANCE.md", judge.acceptance_md(criteria))

    def add_followups(self, items, source):
        if not items:
            return
        merged = judge.add_followups(self.task_meta().get("follow_ups") or [], items, source)
        self.state["follow_ups"] = merged
        self.m.set_meta(self.tid, follow_ups=merged)

    def open_findings_text(self):
        rows = [r for r in judge.Ledger(self.state.get("ledger")).dump() if r.get("status") == "open"]
        return judge.findings_block(rows) if rows else ""

    def judge_text(self):
        criteria = self.criteria()
        if not criteria and not self.state.get("ledger"):
            return ""
        lines = []
        if criteria:
            lines += ["ACCEPTANCE CONTRACT (current status; done needs every required id met with concrete evidence)", judge.acceptance_block(criteria)]
        open_rows = self.open_findings_text()
        if open_rows:
            lines += ["", "OPEN BLOCKING FINDINGS (reference their ids in a revise)", open_rows]
        n = len(self.task_meta().get("follow_ups") or [])
        if n:
            lines += ["", f"{n} follow-up(s) recorded for the pull request (should_fix / nit); they do not need another round."]
        return "\n".join(lines)

    # How many times Relay keeps the team working on its own before accepting open problems, per escalation.
    KEEP_WORKING = {"Review rounds used up": 2, "Verification still failing": 2, "Acceptance criteria not proven": 1,
                    "A blocking finding keeps coming back": 1, "Design review still blocking": 1, "Revision produced no change": 1}

    def escalate(self, title, question, choices, auto, work=None):
        """Ask the human to decide a judge escalation. Returns (choice key, extra text). Unattended runs take `auto`.

        `work` is (choice key, instruction) for the keep-working option. When Relay decides on the owner's behalf,
        a concrete problem the team can still fix (a blocking bug, a failing check) is sent back to be fixed a few
        times before anything is delivered with open problems: delivering known bugs is not "not bothering" the owner."""
        self._auto_choice = False
        self.r.timeline("judge", title, truncate(question, 200))
        turn = self.state.get("turn")
        unattended = (not self.allow_questions) or str((self.task.get("workflow") or {}).get("question_policy")
                                                         or self.cfg.get("question_policy") or "blocked").lower() == "blocked"
        if unattended and work and work[0] in choices:
            used = self.state.setdefault("keep_working", {})
            limit = int((self.cfg.get("autonomous_fix_attempts") or {}).get(title, self.KEEP_WORKING.get(title, 1))
                        if isinstance(self.cfg.get("autonomous_fix_attempts"), dict) else self.KEEP_WORKING.get(title, 1))
            if int(used.get(title) or 0) < limit:
                used[title] = int(used.get(title) or 0) + 1
                self.save()
                self._auto_choice = True
                self.r.msg(role="orchestrator", agent=None, kind="notice", turn=turn,
                           content=f"{title}. Relay keeps the team working instead of delivering known problems "
                                   f"(attempt {used[title]} of {limit}): {choices[work[0]]}. You can redirect with guidance.")
                self.r.timeline("judge", "Keep working", f"{title} · attempt {used[title]}/{limit}")
                return work[0], work[1]
        if not self.allow_questions:
            self._auto_choice = True
            self.r.msg(role="orchestrator", agent=None, kind="notice", turn=turn,
                       content=f"{title}. Agent questions are disabled for this task, so Relay chose: {choices[auto]}.")
            return auto, ""
        if str((self.task.get("workflow") or {}).get("question_policy") or self.cfg.get("question_policy") or "blocked").lower() == "blocked":
            self._auto_choice = True
            self.r.msg(role="orchestrator", agent=None, kind="notice", turn=self.state.get("turn"),
                       content=f"{title}. Relay only interrupts you when a task is truly blocked, so it chose: {choices[auto]}. "
                               "You can redirect with guidance.")
            return auto, ""
        ap = getattr(self.m, "autopilot", None)
        if ap and ap.quiet():
            # Quiet hours (Autopilot settings): nobody is there to answer, so take the safe choice now.
            self._auto_choice = True
            self.r.msg(role="orchestrator", agent=None, kind="notice", turn=turn,
                       content=f"{title}. Quiet hours are on, so Relay chose: {choices[auto]}.")
            self.r.timeline("judge", "Quiet hours · automatic choice", choices[auto])
            return auto, ""
        qid = new_id("q")
        content = f"**{title}**\n\n{question}"
        qmsg = self.r.msg(role="orchestrator", agent="orchestrator", kind="question", qid=qid, content=content,
                          options=list(choices.values()), answered=False, turn=turn)
        self.m.ask_user(self.tid, {"id": qid, "kind": "question", "from": "orchestrator", "agent": "orchestrator", "question": content,
                                   "options": list(choices.values()), "message_id": qmsg["id"], "time": now(),
                                   "auto": choices[auto]})
        self.r.status("needs_input", title)
        self.m.notify("warning", title, truncate(question, 140), self.tid, kind="needs_input")
        minutes = float(self.cfg.get("judge_escalation_timeout_minutes") or 0)
        ans = self.r.wait_for_answer(qid, timeout=minutes * 60 if minutes > 0 else None)
        self.m.clear_pending(self.tid)
        if ans is None:
            self._auto_choice = True
            self.r.msg_update(qmsg["id"], answered=True, answer=f"(no answer within {int(minutes)} minutes) {choices[auto]}")
            self.r.timeline("judge", "No answer · automatic choice", choices[auto])
            return auto, ""
        text = (ans.get("text") or "").strip()
        self.r.msg_update(qmsg["id"], answered=True, answer=text)
        self.r.msg(role="user", agent=None, kind="user", content=text or "(no text)", to="orchestrator", reply_to=qid, turn=turn)
        self.apply_human_text(text)
        key, rest = judge.classify_choice(text, choices)
        self.r.timeline("user", f"Decided: {choices.get(key, 'guidance')}", truncate(rest, 200))
        if key == "stop":
            self.save()
            raise Stopped(f"Stopped by the human at: {title}")
        return key, rest

    def done_gate(self, env, turn):
        """Refuse `done` while a required criterion lacks proof. Returns the supervisor's next envelope, or None to proceed."""
        criteria = self.criteria()
        if not criteria:
            return None   # legacy task without a contract
        criteria = judge.apply_results(criteria, judge.normalize_results(env.get("criteria")))
        gate = judge.done_gate(criteria, self.task_meta().get("verification"), check_verification=self.verify_mode == "each_report")
        self.set_criteria(gate["criteria"])
        if gate["ok"]:
            self.state["gate_nudges"] = 0
            return None
        if self.state.get("deliver_now"):
            self.add_followups([{"severity": "should_fix", "problem": f"{m['id']} not proven at delivery: {m['criterion']} ({m['reason']})"}
                                for m in gate["missing"]], "delivered on request")
            self.state["gate_nudges"] = 0
            self.save()
            return None
        sup_agent, _ = self.role_agent("supervisor")
        n = int(self.state.get("gate_nudges") or 0) + 1
        self.state["gate_nudges"] = n
        self.save()
        limit = max(0, int(self.cfg.get("judge_gate_nudges", 2)))
        ids = ", ".join(m["id"] for m in gate["missing"])
        self.r.msg(role="orchestrator", agent=None, kind="gate", ok=False, missing=gate["missing"], turn=turn,
                   content=f"Done refused: {ids} not proven")
        self.r.timeline("judge", "Done refused · criteria not proven", ids)
        if n <= limit:
            _, env2 = self.supervisor_turn(protocol.done_gate_nudge(gate["missing"], judge.acceptance_block(gate["criteria"]), n, limit),
                                           f"{_label(sup_agent)} is proving the acceptance criteria", turn)
            return env2
        rows = "\n".join(f"- **{m['id']}** {m['criterion']}: {m['reason']}" for m in gate["missing"])
        question = (f"The supervisor declared the task done {n} times without proving these required criteria:\n\n{rows}\n\n"
                    "Deliver anyway (they become follow-ups on the pull request), type guidance for the supervisor, or stop.")
        key, text = self.escalate("Acceptance criteria not proven", question,
                                  {"accept": "Deliver anyway, list the unproven criteria as follow-ups", "guidance": "Give guidance", "stop": "Stop the task"},
                                  auto="accept",
                                  work=("guidance", "For each unproven criterion: if it is not implemented, have the worker implement it; if it is, "
                                                    "produce concrete evidence (a test, command output, file:line or a screenshot) and cite it."))
        self.state["gate_nudges"] = 0
        if key == "accept":
            self.add_followups([{"severity": "should_fix", "problem": f"{m['id']} not proven at delivery: {m['criterion']} ({m['reason']})"}
                                for m in gate["missing"]], "acceptance gate")
            self.save()
            return None
        self.save()
        _, env2 = self.supervisor_turn(protocol.human_answer(question, text), f"{_label(sup_agent)} is acting on your guidance", turn)
        return env2

    def judge_revise(self, env, turn):
        """Severity discipline and loop guards for a revise. Returns the (possibly amended) envelope to act on."""
        sup_agent, _ = self.role_agent("supervisor")
        criteria = self.criteria()
        ledger = judge.Ledger(self.state.get("ledger"))
        env = dict(env)
        implicit = [i for i in self.state.get("pending_findings") or [] if ledger.get(i)]
        if not judge.revise_refs(env) and implicit:
            env["addresses"] = implicit
        chk = judge.check_revise(env, criteria, ledger)
        self.add_followups(chk["followups"], f"supervisor · work package {turn}")
        if not chk["ok"] and (criteria or chk["followups"]):
            n = int(self.state.get("revise_nudges") or 0) + 1
            if n <= max(0, int(self.cfg.get("judge_revise_nudges", 1))):
                self.state["revise_nudges"] = n
                self.save()
                self.r.msg(role="orchestrator", agent=None, kind="gate", ok=False, turn=turn, content=f"Revision refused: {chk['reason']}")
                self.r.timeline("judge", "Revision refused", chk["reason"])
                _, env2 = self.supervisor_turn(protocol.revise_nudge(chk["reason"], judge.acceptance_block(criteria), self.open_findings_text() or "(none)"),
                                               f"{_label(sup_agent)} is restating the revision", turn)
                return env2
            self.r.timeline("judge", "Revision dispatched without references", chk["reason"])
        observed = ledger.observe(chk["blocking"], f"work package {turn}")
        refs = list(dict.fromkeys(chk["refs"] + [f["id"] for f in observed]))
        finding_ids = [r for r in refs if ledger.get(r)]
        recurring = [ledger.get(i) for i in finding_ids
                     if ledger.get(i).get("status") == "open" and int(ledger.get(i).get("attempts") or 0) >= judge.MAX_FIX_ATTEMPTS]
        self.state["ledger"] = ledger.dump()
        note = ""
        if recurring:
            outcome, note = self.recurring_escalation(recurring, ledger)
            finding_ids = [i for i in finding_ids if ledger.get(i).get("status") != "accepted"]
            refs = [r for r in refs if not ledger.get(r) or r in finding_ids]
            if outcome == "accept" and not refs:
                ids = ", ".join(f["id"] for f in recurring)
                _, env2 = self.supervisor_turn(protocol.human_decision_note(f"{ids} accepted as follow-ups; do not revise for them again."),
                                               f"{_label(sup_agent)} is deciding after your decision", turn)
                return env2
        ledger.attempt(finding_ids)
        self.state["ledger"] = ledger.dump()
        self.state["pending_findings"] = []
        self.state["revise_refs"] = refs
        env["addresses"] = refs
        env["_findings"] = observed
        env["_judged"] = True
        text = env.get("instruction") or env.get("summary") or env.get("_text", "")
        if note:
            text += "\n\nHUMAN GUIDANCE\n" + note
        if finding_ids:
            text += "\n\nThis revision addresses: " + ", ".join(refs) + ". Report the evidence for each."
        env["instruction"] = text
        self.save()
        return env

    def recurring_escalation(self, recurring, ledger):
        rows = judge.findings_block(recurring)
        question = (f"This blocking finding came back after the worker was asked to fix it {judge.MAX_FIX_ATTEMPTS} times:\n\n{rows}\n\n"
                    "Accept it as a follow-up on the pull request, type guidance for the team, or stop.")
        key, text = self.escalate("A blocking finding keeps coming back", question,
                                  {"accept": "Accept as follow-up", "guidance": "Give guidance", "stop": "Stop the task"}, auto="accept",
                                  work=("guidance", "The same fix was tried twice and the finding is still there. Step back: reproduce it with a failing "
                                                    "test first, find the root cause, and take a different approach than before."))
        for f in recurring:
            if key == "accept":
                ledger.set_status(f["id"], "accepted")
            else:
                ledger.set_status(f["id"], "open")   # guidance buys a fresh pair of attempts
        if key == "accept":
            self.add_followups([{**f, "severity": "should_fix"} for f in recurring], "accepted after recurring")
        self.state["ledger"] = ledger.dump()
        self.save()
        return key, ("" if key == "accept" else (text or "The human asked the team to try once more; take a different approach."))

    def check_revision_changed(self):
        """A revision after which the worktree is byte-for-byte unchanged goes to the human instead of another round."""
        before = self.state.pop("fp_before", "")
        if not before or self.fingerprint() != before:
            self.save()
            return
        turn = self.state.get("turn")
        refs = self.state.get("revise_refs") or []
        report = self.state.get("report") or {}
        question = (f"The revision in work package #{turn} changed nothing in the worktree"
                    + (f" (it addressed {', '.join(refs)})" if refs else "") + f".\n\nWorker's summary: {report.get('summary') or '(none)'}\n\n"
                    "Accept the revised items as follow-ups, type guidance for the supervisor, or stop.")
        key, text = self.escalate("Revision produced no change", question,
                                  {"accept": "Accept the revised items as follow-ups", "guidance": "Give guidance", "stop": "Stop the task"},
                                  auto="accept",
                                  work=("guidance", "The last revision changed nothing in the working tree. Make the requested changes in the files "
                                                    "themselves, then report the diff."))
        ledger = judge.Ledger(self.state.get("ledger"))
        if key == "accept":
            rows = [ledger.get(r) for r in refs if ledger.get(r)]
            for r in rows:
                ledger.set_status(r["id"], "accepted")
            self.add_followups([{**r, "severity": "should_fix"} for r in rows]
                               + ([{"severity": "should_fix", "problem": f"Revision not applied: {self.state.get('instruction_summary')}"}]
                                  if not rows and self.state.get("instruction_summary") else []), "revision with no change")
            self.state["judge_note"] = ("ORCHESTRATOR · the last revision changed nothing in the worktree and the human accepted "
                                        + (", ".join(refs) or "the revised items") + " as follow-ups. Do not revise for them again; "
                                        "decide done (with criteria evidence) or the next planned package.")
        else:
            for r in refs:
                ledger.set_status(r, "open")
            self.state["judge_note"] = f"ORCHESTRATOR · the last revision changed nothing in the worktree. Human guidance: {text or '(none)'}"
        self.state["ledger"] = ledger.dump()
        self.save()

    def budget_escalation(self, turn):
        """The work-package budget ran out. Returns True to continue with more packages, False when the phase changed."""
        ext = max(1, int(self.cfg.get("judge_budget_extension") or 3))
        limit = self.turn_limit()
        kind = self.state.get("instruction_kind") or "instruction"
        pending = self.state.get("instruction_summary") or truncate(self.state.get("instruction") or "", 300)
        unmet = [c for c in self.criteria() if c.get("required") and c.get("status") != "met" and not (c.get("status") == "waived" and c.get("set_by") == "user")]
        question = (f"The work-package budget ({limit}) is used up. The supervisor's next step is a {kind}: {pending or '(no summary)'}"
                    + (f"\n\nRequired criteria not yet proven: {', '.join(c['id'] for c in unmet)}" if unmet else "")
                    + f"\n\nAllow {ext} more work packages (any reply such as \"go ahead\" does this), deliver what exists now, or stop.")
        choices = {"more": f"Allow {ext} more work packages", "deliver": "Deliver as it is, list remaining items as follow-ups", "stop": "Stop"}
        self.state["budget_exhausted"] = True
        self.save()
        key, text = self.escalate("Work-package budget reached", question, choices,
                                  auto="deliver" if self.state.get("auto_extended") else "more")
        self.state["budget_exhausted"] = False
        if key == "deliver":
            self.add_followups([{"severity": "should_fix", "problem": f"Not done when the work-package budget ran out ({kind}): {pending}"}]
                               + [{"severity": "should_fix", "problem": f"{c['id']} not proven: {c['criterion']}"} for c in unmet], "work-package budget")
            self.state["pr_summary"] = self.state.get("pr_summary") or self.state.get("summary") or (self.state.get("plan") or {}).get("summary", "")
            self.record_blocked([{"check": "Work-package budget", "action_required": True,
                                  "reason": f"delivered after {limit} work packages before the supervisor declared done",
                                  "impact": "remaining items are listed as follow-ups; review before merging"}])
            self.state["phase"] = "deliver"
            self.save()
            return False
        granted = ext
        if key == "guidance":
            m = re.search(r"\b(\d{1,2})\b", text)
            if m and int(m.group(1)) > 0:
                granted = min(int(m.group(1)), 20)
            if text and not re.match(r"^\s*(go ahead|go on|yes|y|ok|okay|continue|proceed|sure|allow|more|\d+)\b", text.lower()):
                self.state["instruction"] = (self.state.get("instruction") or "") + "\n\nUSER GUIDANCE\n" + text
        if self._auto_choice:
            self.state["auto_extended"] = True
        self.state["extra_turns"] = int(self.state.get("extra_turns") or 0) + granted
        self.r.timeline("judge", f"Work-package budget extended by {granted}", f"now {self.turn_limit()}")
        self.save()
        return True

    def review_cap_escalation(self, rnd, observed, ledger):
        """Review rounds are used up. Returns (continue, note): continue False means the task goes to delivery."""
        question = (f"The reviewer still blocks delivery after {rnd} review rounds:\n\n{judge.findings_block(observed)}\n\n"
                    "Deliver with these findings listed as follow-ups, allow one more review round (type guidance if you like), or stop.")
        key, text = self.escalate("Review rounds used up", question,
                                  {"deliver": "Deliver, list the open findings as follow-ups", "more": "Allow one more review round", "stop": "Stop the task"},
                                  auto="deliver",
                                  work=("more", "Fix every blocking finding above exactly as the reviewer describes, with a test for each, "
                                                "then send it back for review. Do not deliver known blocking bugs."))
        if key == "deliver":
            for f in observed:
                ledger.set_status(f["id"], "accepted")
            self.state["ledger"] = ledger.dump()
            self.add_followups([{**f, "severity": "should_fix"} for f in observed], f"open after {rnd} review rounds")
            self.record_blocked([{"check": "Independent review", "action_required": True,
                                  "reason": f"delivered with {len(observed)} blocking finding(s) open after {rnd} review rounds",
                                  "impact": "the open findings are listed as follow-ups; check them before merging"}])
            self.state["phase"] = "deliver"
            self.save()
            return False, ""
        self.max_review_rounds = rnd + 1
        self.state["max_review_rounds"] = self.max_review_rounds
        self.save()
        return True, text

    def verify_escalation(self, vt, turn):
        """Verification still fails after triage. Returns the supervisor's next envelope, or None to deliver with it recorded."""
        failures = judge.failed_checks(self.task_meta().get("verification"))
        names = ", ".join(f"`{f.get('command')}`" for f in failures) or "the verification"
        question = (f"Verification still fails after the supervisor triaged it: {names}. The failure does not also happen on the starting commit.\n\n"
                    "Deliver anyway with the failing checks recorded, type guidance for the supervisor, or stop.")
        key, text = self.escalate("Verification still failing", question,
                                  {"accept": "Deliver anyway, record the failing checks", "guidance": "Give guidance", "stop": "Stop the task"},
                                  auto="accept",
                                  work=("guidance", "These checks fail only with your change. Read the failure output, find the root cause and fix it; "
                                                    "if a check is genuinely wrong, fix the check and explain why."))
        self.state["verify_triage"] = 0
        if key == "accept":
            self.record_blocked([{"check": f.get("command") or "Verification", "action_required": True,
                                  "reason": "failed in Relay's verification at delivery and not on the starting commit",
                                  "impact": "delivered with this check failing; fix before merging"} for f in failures])
            self.add_followups([{"severity": "should_fix", "problem": f"Verification failing at delivery: {f.get('command')} (exit {f.get('rc')})"}
                                for f in failures], "verification")
            self.save()
            return None
        self.save()
        sup_agent, _ = self.role_agent("supervisor")
        _, env2 = self.supervisor_turn(protocol.human_answer(question, text) + "\n\n" + protocol.supervisor_after_review("Orchestrator", {"verdict": "FAIL", "findings": []}, vt, 0, 0),
                                       f"{_label(sup_agent)} is acting on your guidance", turn)
        return env2

    def finish_done(self):
        rev_agent, _ = self.role_agent("reviewer")
        if rev_agent and self.state.get("deliver_now"):
            self.ceremony("review", False, "the owner asked to deliver now")
            rev_agent = ""
        elif rev_agent and self.triage_info() and not self.state.get("review_round") and self.auto_ceremony():
            want, why = triage.review_wanted(self.triage_info(), self.all_diffstat(), self.all_changed_files())
            self.ceremony("review", want, why)
            if not want:
                self.r.timeline("judge", "Independent review skipped", why)
                rev_agent = ""
        if rev_agent:
            self.state["phase"] = "review"
            self.state["review_round"] = int(self.state.get("review_round") or 0) + 1
        else:
            self.state["phase"] = "deliver"
        self.save()


    def review(self):
        sup_agent, _ = self.role_agent("supervisor")
        rev_agent, _ = self.role_agent("reviewer")
        while self.state.get("phase") == "review":
            self.r.check_stop()
            rnd = int(self.state.get("review_round") or 1)
            self.phase_mark("review")
            if self.state.get("deliver_now"):
                self.state["phase"] = "deliver"
                self.save()
                break
            self.r.status("reviewing", f"{_label(rev_agent)} is reviewing independently · round {rnd}/{self.max_review_rounds}")
            vt = self.state.get("last_verification") or ""
            if (((self.verify_cmds or perfcheck.applies(self)) and self.verify_mode != "off") or self.design_gate) and not vt:
                vt = self.run_verification()
                self.state["last_verification"] = vt
            sess = self.sessions.get("reviewer") or {}
            diff = self.review_diff(sess, rev_agent)
            if int(sess.get("turns", 0)) == 0 or sess.get("agent") != rev_agent:
                prompt = protocol.reviewer_kickoff(self.task, self.wt, self.branch, {**(self.state.get("plan") or {}), "criteria": self.criteria()},
                                                   self.state.get("pr_summary", ""), vt, diff, rnd, self.cfg,
                                                   self.state.get("blocked_checks") or [], judge.acceptance_block(self.criteria()) if self.criteria() else "",
                                                   self.tools_text("reviewer"))
                prompt = protocol.with_block(prompt, self.knowledge_text, "knowledge")
                prompt = protocol.with_block(prompt, self.context_text("reviewer"))
            else:
                prompt = protocol.reviewer_followup(rnd, vt, diff, self.state.get("pr_summary", ""),
                                                    judge.acceptance_block(self.criteria()) if self.criteria() else "",
                                                    self.open_findings_text())
            self.handoff("orchestrator", "reviewer", f"Independent review requested · round {rnd}", self.state.get("pr_summary", ""), subtype="review_request")
            try:
                res, env = self.run_role("reviewer", prompt, f"Review round {rnd}")
            except (Stopped, Interrupted):
                raise
            except Exception as e:
                # The reviewer already retried with fresh sessions. A broken review tool must not throw away the
                # team's finished work: deliver on the supervisor's approval and say plainly that review did not run.
                self.r.msg(role="reviewer", agent=rev_agent, kind="error", content=f"Independent review could not run: {truncate(str(e), 1500)}", turn=self.state.get("turn"))
                self.record_blocked([{"check": f"Independent review ({_label(rev_agent)})", "action_required": True,
                                      "reason": truncate(str(e), 300), "impact": "the change was not independently reviewed; review it yourself before merging"}])
                self.r.timeline("reviewer", "Review skipped after repeated failures", truncate(str(e), 200))
                self.state["phase"] = "deliver"
                self.save()
                break
            verdict = str(env.get("verdict", "FAIL")).upper()
            text = env.get("_text") or env.get("text") or ""
            findings = judge.normalize_findings(env.get("findings") if isinstance(env.get("findings"), list) else [])
            ledger = judge.Ledger(self.state.get("ledger"))
            for f in findings:
                if f["severity"] == "blocking" and ledger.accepted(f):
                    f["severity"] = "should_fix"   # the human already accepted it as a follow-up
            blocking, minor = judge.split_findings(findings)
            self.add_followups(minor, f"review round {rnd}")
            claimed = verdict
            # Severity decides, not the headline: FAIL needs a blocking finding, and a blocking finding is a FAIL.
            # A FAIL with no structured findings at all (a verdict parsed from text) keeps the legacy behaviour.
            verdict = "FAIL" if blocking else ("PASS" if findings else verdict)
            observed = ledger.observe(blocking, f"review round {rnd}")
            self.state["ledger"] = ledger.dump()
            findings = observed + minor
            review_md = f"# Review round {rnd} · {verdict}\n\n{env.get('summary','')}\n\n" + "\n".join(
                f"- [{f.get('severity','blocking')}] {f.get('file','')}: {f.get('problem','')} → {f.get('fix','')}" for f in findings) + f"\n\n---\n{text}"
            self.artifact("review", "REVIEW.md", review_md)
            self.r.msg(role="reviewer", agent=rev_agent, kind="review", verdict=verdict, summary=env.get("summary", ""),
                       findings=findings, content=text if not findings else "", turn=self.state.get("turn"))
            self.m.set_meta(self.tid, review={"verdict": verdict, "claimed_verdict": claimed, "round": rnd, "summary": env.get("summary", ""), "findings": findings})
            self.r.timeline("reviewer", f"Review round {rnd}: {verdict}" + (f" (reviewer said {claimed})" if claimed != verdict else ""), env.get("summary", ""))
            human_note = ""
            recurring = ledger.recurring(observed)
            if verdict == "FAIL" and recurring:
                outcome, human_note = self.recurring_escalation(recurring, ledger)
                observed = [f for f in observed if not ledger.accepted(f)]
                if not observed:
                    verdict = "PASS"
            if verdict == "PASS":
                self.state["phase"] = "deliver"
                self.save()
                return
            if rnd >= self.max_review_rounds:
                if human_note:
                    self.max_review_rounds = rnd + 1   # the human just gave guidance on these findings: that buys the round
                    self.state["max_review_rounds"] = self.max_review_rounds
                else:
                    go_on, human_note = self.review_cap_escalation(rnd, observed, ledger)
                    if not go_on:
                        return
            self.state["pending_findings"] = [f["id"] for f in observed]
            self.save()
            self.handoff("reviewer", "supervisor", f"Review findings · round {rnd}", env.get("summary", ""), subtype="review", findings=findings[:30])
            _, senv = self.supervisor_turn(protocol.supervisor_after_review(_label(rev_agent), {**env, "findings": observed}, text, rnd,
                                                                            self.max_review_rounds, minor, human_note),
                                           f"{_label(sup_agent)} is triaging review findings", self.state.get("turn"))
            if senv.get("type") == "decision" and senv.get("decision") == "done":
                # Supervisor insists it is done; give the reviewer one more look with the supervisor's rebuttal.
                self.state["pr_summary"] = senv.get("pr_summary") or self.state.get("pr_summary", "")
                self.add_followups(senv.get("follow_ups") or senv.get("followups"), "supervisor")
                if self.criteria() and senv.get("criteria"):
                    self.set_criteria(judge.apply_results(self.criteria(), judge.normalize_results(senv.get("criteria"))))
                self.state["review_round"] = rnd + 1
                self.save()
                continue
            self.apply_supervisor_decision(senv, int(self.state.get("turn") or 1))
            if self.state.get("phase") == "dialogue":
                self.dialogue()
                # dialogue ends with phase review (round already incremented) or deliver

    def deliver(self):
        self.phase_mark("deliver")
        t = self.task_meta()
        ds = gitops.diff_stat(self.wt, self.base)
        changed = gitops.changed_files(self.wt, self.base)
        if self.approval and not self.state.get("approved"):
            qid = new_id("a")
            summary = self.state.get("pr_summary") or self.state.get("summary") or ""
            qmsg = self.r.msg(role="orchestrator", agent=None, kind="approval", qid=qid, content=summary, diffstat=ds,
                              files=[c["path"] for c in changed[:60]], answered=False, turn=self.state.get("turn"))
            self.m.ask_user(self.tid, {"id": qid, "kind": "approval", "from": "orchestrator", "question": "Approve delivery?",
                                       "summary": summary, "diffstat": ds, "message_id": qmsg["id"], "time": now()})
            self.r.status("needs_input", "Waiting for your approval to commit, push and open the pull request")
            self.m.notify("warning", "Approval needed", f"{t.get('name')} is ready for delivery", self.tid, kind="approval")
            ans = self.r.wait_for_answer(qid)
            self.m.clear_pending(self.tid)
            approved = bool((ans.get("extra") or {}).get("approved", True))
            self.r.msg_update(qmsg["id"], answered=True, approved=approved, answer=ans.get("text") or "")
            if not approved:
                note = ans.get("text") or "Changes requested by the human reviewer."
                self.r.msg(role="user", agent=None, kind="user", content=note, to="supervisor", turn=self.state.get("turn"))
                self.r.timeline("user", "Delivery rejected", truncate(note, 200))
                sup_agent, _ = self.role_agent("supervisor")
                _, senv = self.supervisor_turn(
                    f"HUMAN REVIEW · delivery was NOT approved. Requested changes:\n{note}\n\n"
                    "Reply with a {\"type\":\"decision\",\"decision\":\"revise\",...} envelope containing the exact work package for the worker.",
                    f"{_label(sup_agent)} is handling the human's change request", self.state.get("turn"))
                self.apply_supervisor_decision(senv, int(self.state.get("turn") or 1))
                if self.state.get("phase") == "dialogue":
                    self.dialogue()
                    if self.state.get("phase") == "review":
                        self.review()
                    return self.deliver()
            self.state["approved"] = True
            self.save()

        self.r.status("delivering", "Committing the task branch")
        # Someone can switch the worktree to another branch (VS Code's branch picker, a stray checkout).
        # Committing there would put the team's work on the wrong branch and push an empty task branch.
        current = quiet(["git", "branch", "--show-current"], cwd=self.wt).stdout.strip()
        if current != self.branch:
            raise RuntimeError(f"The worktree is on '{current or 'a detached HEAD'}', not the task branch '{self.branch}', so Relay did not "
                               f"commit. Switch it back (git switch {self.branch}, keeping the changes) and resume the task.")
        self.guard_related_branches()
        cleanup_refs(self.wt)
        shutil.rmtree(Path(self.wt) / exploration.MOCKUP_DIR, ignore_errors=True)  # mockups stay in the run folder
        cfg = self.cfg
        prefix = (cfg.get("commit_message_prefix") or "agent:").strip()
        title = t.get("github_issue_title") or t["name"]
        self.write_design_doc()   # docs/designs/<date>-<slug>.md in the primary repository, when the task has an approved design
        committed = gitops.commit_all(self.r, self.wt, f"{prefix} {title}".strip())
        self.r.timeline("git", "Committed" if committed else "Nothing new to commit", self.branch)
        self.state["head"] = commitguard.head(self.wt)
        self.save()

        pr = {"url": t.get("pr_url"), "number": t.get("pr_number")}
        if self.repo_full and cfg.get("github_auto_push_on_pass", True):
            self.r.status("delivering", "Pushing the task branch to GitHub")
            gitops.push_branch(self.r, self.wt, self.branch, lease=self.state.get("agent_pushed") or "")
            self.r.timeline("github", "Branch pushed", self.branch)
            self.design_doc_url()
            if cfg.get("github_auto_create_pr", True):
                existing = github.pr_for_branch(self.repo_full, self.branch)
                if existing:
                    pr = {"url": existing.get("url"), "number": existing.get("number")}
                    self.r.timeline("github", "Existing pull request found", pr["url"])
                else:
                    body = protocol.pr_body(cfg, t, self.state.get("pr_summary") or self.state.get("summary"), self.details_md(),
                                            t.get("github_issue_number"))
                    body_file = self.run_dir / "PR_BODY.md"
                    write_text(body_file, body)
                    self.r.status("delivering", "Opening draft pull request")
                    base = (t.get("github_base") or cfg.get("github_pr_base") or "").strip()
                    pr = github.create_pr(self.r, self.wt, self.repo_full, self.branch, title, body_file, base, cfg.get("github_pr_draft", True))
                    self.r.timeline("github", "Draft pull request opened", pr.get("url") or "")
                    self.m.notify("success", "Pull request opened", pr.get("url") or title, self.tid, kind="pr_opened")
                self.m.set_meta(self.tid, pr_url=pr.get("url"), pr_number=pr.get("number"))
                self.comment_design(pr)

        self.deliver_related(pr, title, prefix)
        self.note_knowledge(changed, pr)
        self.write_pr_previews()
        report = self.final_report_md(pr, committed)
        self.artifact("report", "REPORT.md", report)
        self.state["phase"] = "done"
        self.save()
        self.m.complete(self.tid, {"branch": self.branch, "worktree": str(self.wt), "pr_url": pr.get("url"), "pr_number": pr.get("number"),
                                   "summary": self.state.get("summary") or self.state.get("pr_summary") or "", "diffstat": gitops.diff_stat(self.wt, self.base), "changed_count": len(gitops.changed_files(self.wt, self.base))})
        self.r.msg(role="orchestrator", agent=None, kind="complete", content=self.state.get("summary") or "", pr_url=pr.get("url"),
                   branch=self.branch, turn=self.state.get("turn"))

    def note_knowledge(self, changed, pr):
        """A delivery that touched key files (manifests, Dockerfiles, compose, CI, migrations, config) marks the repository's
        knowledge doc as possibly stale until the next refresh (knowledge.py)."""
        try:
            rows = [(self.repo_full, [c["path"] for c in changed], pr.get("url") or "")]
            for r in getattr(self, "related", None) or []:
                rows.append((r.get("github_repo"), [c["path"] for c in gitops.changed_files(r["worktree"], r.get("base_commit"))], r.get("pr_url") or ""))
            for repo, files, url in rows:
                mark = knowledge.mark_delivery(repo, files, self.tid, url)
                if mark:
                    self.r.timeline("system", "Knowledge doc may be stale", f"{repo}: " + ", ".join(mark["key_files"][:5]))
        except Exception:  # knowledge bookkeeping must never fail a delivery
            traceback.print_exc()

    def write_pr_previews(self):
        """The pull request bodies as Relay sends them, per repository, written also when nothing is pushed."""
        t = self.task_meta()
        body = protocol.pr_body(self.cfg, t, self.state.get("pr_summary") or self.state.get("summary"), self.details_md(), t.get("github_issue_number"))
        write_text(self.run_dir / "PR_BODY.md", body)
        for r in self.related:
            write_text(Path(self.run_dir) / "repos" / r["name"] / "PR_BODY.md", self._related_pr_body(r, t))
        self.write_changeset_preview()

    def details_md(self):
        t = self.task_meta()
        roles = self.roles
        lines = [f"**Workflow:** {_label(roles.get('supervisor',{}).get('agent'))} supervised {_label(roles.get('worker',{}).get('agent'))}"
                 + (f", reviewed independently by {_label(roles.get('reviewer',{}).get('agent'))}" if roles.get('reviewer', {}).get('agent') else ""),
                 f"**Work packages:** {self.state.get('turn', 0)}",
                 f"**Branch:** `{self.branch}`"]
        if self.related:
            lines.append("**Repositories:** " + ", ".join([f"{Path(self.task.get('repo') or '').name} (primary)"] + [r["name"] for r in self.related]))
        v = t.get("verification") or {}
        if v.get("items"):
            lines.append("**Verification:** " + ", ".join(f"`{i['command']}` {'✓' if i['ok'] else '✗'}" for i in v["items"]))
        criteria = t.get("acceptance") or []
        acc = (self.state.get("plan") or {}).get("acceptance") or []
        if criteria:
            mark = {"met": "x", "waived": "~"}
            lines.append("\n**Acceptance criteria**\n" + "\n".join(
                f"- [{mark.get(c.get('status'), ' ')}] {c['id']} {c['criterion']}" + (f" · {c['evidence']}" if c.get("evidence") else "") for c in criteria))
        elif acc:
            lines.append("\n**Acceptance criteria**\n" + "\n".join(f"- [x] {a}" for a in acc))
        follow = judge.followups_md(t.get("follow_ups") or [])
        if follow:
            lines.append("\n**Follow-ups** (non-blocking, not done in this change)\n" + follow)
        changes = self.changeset_text()
        if changes:
            lines.append("\n" + changes)
        lines.append("\n_Generated by Relay._")
        return "\n".join(lines)

    def final_report_md(self, pr, committed):
        t = self.task_meta()
        m = t.get("metrics") or {}
        rows = []
        for agent, d in (m.get("agents") or {}).items():
            rows.append(f"| {_label(agent)} | {d.get('turns',0)} | {d.get('input',0):,} | {d.get('output',0):,} | ${d.get('cost_usd',0):.2f} | {int(d.get('seconds',0))}s |")
        return f"""# {t.get('name')}

**Status:** delivered · **Branch:** `{self.branch}` · **PR:** {pr.get('url') or 'not created'} · **Committed:** {committed}

## Summary
{self.state.get('pr_summary') or self.state.get('summary') or ''}

## Plan
{(self.state.get('plan') or {}).get('plan','')}

## Acceptance criteria
{judge.acceptance_md(t.get('acceptance')).split(chr(10), 2)[-1] if t.get('acceptance') else chr(10).join('- ' + a for a in (self.state.get('plan') or {}).get('acceptance', []))}
## Follow-ups
{judge.followups_md(t.get('follow_ups') or []) or '(none)'}

## Verification
{self.state.get('last_verification') or '(none)'}

## Agent usage
| Agent | Turns | Input tokens | Output tokens | Cost | Time |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

{self.related_report_md()}
{self.details_md()}
"""

    # ------------------------------------------------------------ entry
    def validate_team(self):
        """Fail fast, before any paid agent turn, if a required role has no agent."""
        missing = [r for r in ("supervisor", "worker") if not self.role_agent(r)[0]]
        if missing:
            raise RuntimeError("No agent is assigned to the " + " and ".join(missing)
                               + " role. Open the task menu, choose Edit task settings, pick an agent, then Retry.")
        for r in ("supervisor", "worker", "reviewer"):
            a = self.role_agent(r)[0]
            if a and a not in C.AGENTS:
                raise RuntimeError(f"Unknown agent '{a}' for the {r} role.")
        self.publish_role_plan()

    def publish_role_plan(self):
        """Record what each role will actually run on, resolved here rather than guessed by the browser.

        The model and effort a role ends up with come from the task's team, the global role settings and the
        agent defaults, in that order, and an effort the CLI does not offer is dropped. The task page shows this,
        so it has to be the same answer the runner uses — including which of those layers decided it (`sources`)
        and the sign-in the role is pinned to, so the page can name the source instead of inferring it.
        """
        plan = {}
        for r in ("supervisor", "worker", "reviewer"):
            agent, model = self.role_agent(r)
            if not agent:
                continue
            plan[r] = {"agent": agent, "model": model, "effort": self.role_effort(r),
                       "provider": self.role_provider(r),
                       "supports_effort": bool(C.AGENTS.get(agent, {}).get("efforts")),
                       "account": self.role_account(r),
                       "sources": self.role_sources(r)}
        self.m.set_meta(self.tid, role_plan=plan)
        return plan

    # ------------------------------------------------------------ tools and token efficiency (orchestrator/toolbox.py, tokens.py)
    def tools_text(self, role):
        agent, _ = self.role_agent(role)
        tools = toolbox.tools_for_task({**self.task_meta(), "id": self.tid})
        served = [t["name"] for t in tools if t.get("kind") == "mcp" and toolbox.installed(t)] if agent in toolbox.MCP_SUPPORT else []
        return toolbox.describe(tools, agent, {"servers": served}, self.cfg.get("tools_request_policy") or "ask")

    def tools_turn_note(self, role, agent, sess, prompt):
        """Tools approved or added since this session last heard about them are announced once, on its next turn."""
        names = toolbox.names_for_task({**self.task_meta(), "id": self.tid})
        seen = sess.get("tools_seen")
        notes = self.state.get("tool_notes", {}).pop(role, "") if self.state.get("tool_notes") else ""
        sess["tools_seen"] = names
        if int(sess.get("turns", 0)) == 0 or seen is None:
            return (tokens.prepend(prompt, notes, "tools") if notes else prompt), sess
        added = [n for n in names if n not in seen]
        if added:
            tools = [t for t in toolbox.tools_for_task({**self.task_meta(), "id": self.tid}) if t["name"] in added]
            served = [t["name"] for t in tools if t.get("kind") == "mcp" and toolbox.installed(t)] if agent in toolbox.MCP_SUPPORT else []
            text = toolbox.describe(tools, agent, {"servers": served}, "off")
            notes = (notes + "\n\n" if notes else "") + "ORCHESTRATOR · tools added to this task since your last turn\n" + text
        return (tokens.prepend(prompt, notes, "tools") if notes else prompt), sess

    def tool_request_from_envelope(self, role, agent, env):
        """An envelope's `tool_request` (one or a list) goes through the same policy as `relay-tools request`."""
        reqs = env.get("tool_request") or env.get("tool_requests")
        if not reqs:
            return
        for spec in (reqs if isinstance(reqs, list) else [reqs])[:3]:
            try:
                row = toolbox.request({**self.task_meta(), "id": self.tid}, role, agent, spec, self.cfg.get("tools_request_policy") or "ask")
            except Exception as e:
                row = {"status": "refused", "error": str(e)}
            self.tool_request_recorded(role, agent, row)

    def tool_request_recorded(self, role, agent, row):
        status, name = row.get("status"), row.get("name") or row.get("tool") or "?"
        self.r.msg(role=role, agent=agent, kind="notice", turn=self.state.get("turn"),
                   content=f"Tool request `{name}`: {status}" + (f" · {row.get('why')}" if row.get("why") else "")
                   + (f" · {row.get('error')}" if row.get("error") else ""))
        self.r.timeline(role, f"Tool requested · {name}", status or "")
        if status == "pending":
            self.m.notify("warning", f"{_label(agent)} asks for a tool", f"{name}: {truncate(row.get('why') or '', 120)}", self.tid, kind="needs_input")
        elif status in ("approved", "refused"):
            note = (f"ORCHESTRATOR · your tool request `{name}` was {status}"
                    + (" and is available from this turn" if status == "approved" else f": {row.get('error') or row.get('note') or ''}"))
            self.state.setdefault("tool_notes", {})[role] = note
            self.save()

    def _delta(self, role, key, text):
        """(text to send, unchanged): content this role's live session already received is not sent again."""
        if not text or not self.cfg.get("token_prompt_deltas", True):
            return text, False
        sess = self.sessions.get(role) or {}
        marker = f"{sess.get('agent')}:{sess.get('id')}"
        sent = self.state.setdefault("sent", {}).setdefault(role, {})
        if sent.get("_session") != marker:
            sent.clear()
            sent["_session"] = marker
        h = tokens.digest(re.sub(r"\d+(?:\.\d+)?s\)", "s)", text))
        if sent.get(key) == h:
            return text, True
        sent[key] = h
        return text, False

    def after_report_deltas(self):
        vt = self.state.get("last_verification", "")
        _, same = self._delta("supervisor", "verification", vt)
        if same:
            heads = [line for line in vt.splitlines() if line.startswith("$ ") or re.match(r"^(PASS|FAIL|PRE-EXISTING|SKIPPED)", line)]
            vt = "Unchanged since the previous report (same results, output not repeated):\n" + "\n".join(heads[:40])
        jt = "\n\n".join(x for x in (self.judge_text(), self.design_judge_text()) if x)
        _, same = self._delta("supervisor", "judge", jt)
        if same:
            jt = "ACCEPTANCE CONTRACT: unchanged since your last message (same criteria, statuses and open findings)."
        _, brief = self._delta("supervisor", "reply_help", "after_report")
        return vt, jt, brief

    def review_diff(self, sess, rev_agent):
        budget = int(self.cfg.get("budget_diff_chars") or 50000)
        if (self.cfg.get("token_diff_mode") or "targeted") != "targeted":
            return self.all_diff(budget)
        full = self.all_diff(max(budget * 20, 400000))
        resumed = int(sess.get("turns", 0)) > 0 and sess.get("agent") == rev_agent
        text, hashes = tokens.targeted_diff(full, budget, self.base or "", self.state.get("review_diff") if resumed else None)
        self.state["review_diff"] = hashes
        return text

    def handoff_prompt(self, role, agent, note, message, context_tokens=0):
        changed = self.all_changed_files() if self.wt else []
        handoff = tokens.compact_handoff(role, self.state, judge.acceptance_block(self.criteria()) if self.criteria() else "",
                                         changed, self.open_findings_text(), int(context_tokens or 0))
        prompt = protocol.resume_kickoff(role, self.task, self.wt, self.branch, self.cfg, note + "\n\n" + handoff, str(message),
                                         self.env_text(), self.tools_text(role), self.gate_command() if role == "worker" else "")
        return protocol.with_block(prompt, getattr(self, "knowledge_text", ""), "knowledge")

    def maybe_compact(self, role, agent, sess, prompt, skey=None):
        """A session whose context outgrew the threshold is replaced by a fresh one that starts from a Relay handoff."""
        limit = int(self.cfg.get("token_compact_threshold") or 0)
        size = int(sess.get("context_tokens") or 0)
        if not limit or int(sess.get("turns", 0)) == 0 or size <= limit or not self.wt:
            return prompt, sess
        self.r.timeline(role, "Compacting the session", f"{_label(agent)} ({role}) context ≈ {size:,} tokens > {limit:,}: fresh session with a handoff")
        new_prompt = self.handoff_prompt(role, agent, "", prompt, size)
        self.m.set_meta(self.tid, compactions=int(self.task_meta().get("compactions") or 0) + 1)
        sess = {"agent": agent, "turns": 0, "compacted_from": size}
        self.sessions[skey or role] = sess
        self.save()
        return new_prompt, sess

    # ------------------------------------------------------------ connectors
    def connect_start(self):
        """Scope the task's connectors and hand agents a token that only works while this run lasts."""
        names = connectors.names_for_task(self.task)
        self.connector_names = names
        # The token also lets agents use relay-tools (list and request tools), so it is issued without connectors too.
        token = connectors.issue_token(self.tid, names)
        self.r.set_agent_env({"RELAY_CONNECT_URL": connectors.relay_url(), "RELAY_CONNECT_TOKEN": token,
                              "RELAY_CONNECT_BIN": str(connectors.BIN_DIR)})
        repo_mask = self.repo_masker()  # every repository of the task (orchestrator/multirepo.py)
        conn_mask = connectors.masker([c for c in connectors.load_all() if c["name"] in names], [token])
        self._conn_mask = conn_mask
        self.r.set_masker(lambda s: conn_mask(repo_mask(s)))
        self.m.set_meta(self.tid, connectors_active=names)
        if names:
            self.r.timeline("system", "Connectors available", ", ".join(names))
        tools = toolbox.tools_for_task({**self.task_meta(), "id": self.tid})
        if tools:
            self.r.timeline("system", "Tools available", ", ".join(t["name"] for t in tools))

    def connect_stop(self):
        connectors.revoke_task(self.tid)
        self.r.set_agent_env({})

    def run(self):
        self.validate_team()
        self.start_triage()   # a borderline request is rated while the environment installs
        self.prepare()
        try:
            self.connect_start()
            tri = self.finish_triage()
            if self.state.get("phase") == "kickoff" and tri.get("mode") == "solo" and not self.state.get("solo_handoff"):
                self.state["phase"] = "solo"
                self.save()
            if self.state.get("phase") == "solo":
                self.solo_phase()
            if self.state.get("phase") == "kickoff":
                self.kickoff()
            if self.state.get("phase") == "design":
                self.design_phase()
            if self.exploration_due():
                # UI and design work: explore directions with a focus group before building (orchestrator/exploration.py).
                self.state.update({"phase": "explore", "explore_stage": "mockups"})
                self.save()
            if self.state.get("phase") == "explore":
                self.exploration_phase()
            if self.state.get("phase") == "dialogue":
                self.dialogue()
            if self.state.get("phase") == "review":
                self.review()
            if self.state.get("phase") == "deliver":
                self.deliver()
        finally:
            self.connect_stop()
            self.stop_services()
            stacks.pipeline_teardown(self)


def orchestrate(task, runner, manager):
    Pipeline(task, runner, manager).run()
