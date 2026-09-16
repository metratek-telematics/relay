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
import shutil
import time
import traceback
from pathlib import Path

from . import config as C
from . import designcheck, gitops, github, protocol
from .runner import Interrupted, Stopped, TurnTimeout
from .util import APP_DIR, new_id, now, quiet, read_text, truncate, write_text

SUP_TYPES = {"plan", "instruction", "decision", "question"}
WRK_TYPES = {"report", "question"}
REV_TYPES = {"review"}


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
class Pipeline:
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
        model = ((r.get("model") or "").strip()
                 or (self.cfg.get("roles", {}).get(role, {}).get("model") or "").strip()
                 or ((self.cfg.get("agent_defaults", {}).get(agent) or {}).get("model") or "").strip())
        return agent, model

    def role_effort(self, role):
        r = self.roles.get(role) or {}
        agent = (r.get("agent") or "").strip()
        eff = ((r.get("effort") or "").strip()
               or (self.cfg.get("roles", {}).get(role, {}).get("effort") or "").strip()
               or ((self.cfg.get("agent_defaults", {}).get(agent) or {}).get("effort") or "").strip())
        allowed = C.AGENTS.get(agent, {}).get("efforts") or []
        return eff if eff in allowed else ""

    def handoff(self, frm, to, title, content, **extra):
        return self.r.msg(role=frm, agent=self.role_agent(frm)[0] if frm in self.roles else None, kind="handoff",
                          to=to, title=title, content=content or "", turn=self.state.get("turn"), **extra)

    def take_guidance(self, role):
        return self.m.take_guidance(self.tid, role)

    def status_for(self, role, turn=None):
        if role == "supervisor":
            return "planning" if not turn else "reviewing"
        if role == "worker":
            return "implementing"
        return "reviewing"

    # ------------------------------------------------------------ agent turns
    def run_role(self, role, prompt, label, turn=None, expect=None):
        agent, model = self.role_agent(role)
        if not agent:
            raise RuntimeError(f"No agent configured for the {role} role.")
        sess = dict(self.sessions.get(role) or {})
        if sess.get("agent") != agent:
            sess = {"agent": agent, "turns": 0}
        expect = expect or {"supervisor": SUP_TYPES, "worker": WRK_TYPES, "reviewer": REV_TYPES}[role]
        nudges = 0
        failures = 0
        timeouts = 0
        while True:
            self.r.wait_if_paused()
            try:
                res = self.r.run_agent(role, agent, prompt, self.wt, self.cfg, model, sess, self.run_dir, label, turn,
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
                if timeouts > 1:
                    raise
                prompt = protocol.timeout_note(int(self.cfg.get("agent_turn_timeout_minutes") or 0))
                continue
            sess = res["session"]
            self.sessions[role] = sess
            self.save()
            if not res.get("ok"):
                failures += 1
                self.r.msg(role=role, agent=agent, kind="error", content=truncate(res.get("error") or "Agent turn failed", 3000), turn=turn)
                if failures > 2:
                    raise RuntimeError(f"{_label(agent)} ({role}) failed repeatedly: {truncate(res.get('error') or '', 500)}")
                self.r.timeline(role, "Agent turn failed · retrying", truncate(res.get("error") or "", 200))
                time.sleep(8 * failures)
                if sess.get("id"):
                    prompt = ("ORCHESTRATOR · your previous turn ended with an error: "
                              + truncate(res.get("error") or "", 600)
                              + "\nContinue from the current working tree state and end with the appropriate protocol envelope.")
                continue
            env = protocol.envelope_from_result(res)
            if role == "reviewer" and (not env or env.get("type") != "review"):
                v = protocol.verdict_from_text(res.get("last_message") or res.get("text") or "")
                if v:
                    env = {"type": "review", "verdict": v, "summary": "(verdict parsed from text)", "findings": [],
                           "text": res.get("text")}
            if env and env.get("type") in expect:
                env["_text"] = res.get("text") or ""
                return res, env
            nudges += 1
            if nudges > int(self.cfg.get("envelope_retries") or 2):
                raise RuntimeError(f"{_label(agent)} ({role}) did not return a valid protocol envelope after {nudges} attempts.")
            self.r.timeline(role, "Missing protocol envelope", f"Asking {_label(agent)} to restate its reply ({nudges})")
            prompt = protocol.nudge(role)

    # ------------------------------------------------------------ questions
    def ask_human(self, asker_role, env):
        """Block until the human answers. Returns the follow-up prompt for the asker."""
        question = env.get("question") or env.get("content") or "(no question text)"
        options = env.get("options") if isinstance(env.get("options"), list) else []
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

    def run_verification(self):
        if not self.verify_cmds and not self.design_gate:
            return ""
        self.r.status("verifying", f"Running {len(self.verify_cmds) + (1 if self.design_gate else 0)} verification check(s)")
        self.r.timeline("verify", "Verification", ", ".join(self.verify_cmds + (["design gate"] if self.design_gate else [])))
        items = []
        parts = []
        timeout = float(self.cfg.get("verification_timeout_minutes") or 20) * 60
        quiet_pass = bool(self.cfg.get("budget_verify_pass_quiet", True))
        fail_chars = int(self.cfg.get("budget_verify_chars") or 3500)
        for c in self.verify_cmds:
            res = self.r.run_shell(c, self.wt, "verify", timeout=timeout)
            # pytest exit 5 means no tests were collected: nothing failed, so do not send the team chasing it.
            skipped = res.get("rc") == 5 and "pytest" in c
            if skipped:
                res["ok"] = True
            items.append({"command": c, "ok": res["ok"], "rc": res.get("rc"), "skipped": skipped, "duration": round(res.get("duration") or 0, 1)})
            verdict = "SKIPPED (no tests collected)" if skipped else ("PASS" if res["ok"] else "FAIL")
            head = f"$ {c}\n{verdict} (exit {res.get('rc')}, {round(res.get('duration') or 0)}s)"
            # A passing command only needs its verdict; a failure needs output the agents can act on.
            body = "" if (res["ok"] and quiet_pass) else "\n" + truncate(res["output"], fail_chars, tail=True)
            parts.append(head + body)
        if self.design_gate and self.base:
            item, text = self.run_design_gate()
            items.append(item)
            # Every violation is listed: the gate is only useful if the team can fix each line it names.
            parts.append(text)
        vt = "\n\n".join(parts)
        all_ok = all(i["ok"] for i in items)
        self.artifact("verification", "VERIFICATION.md", vt)
        self.m.set_meta(self.tid, verification={"ok": all_ok, "items": items, "time": now()})
        self.r.msg(role="verify", agent=None, kind="verification", ok=all_ok, items=items, turn=self.state.get("turn"))
        self.r.timeline("verify", "Verification passed" if all_ok else "Verification failed",
                        f"{sum(1 for i in items if i['ok'])}/{len(items)} commands passed")
        return vt

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
        else:
            self.state = {"phase": "kickoff", "turn": 0, "review_round": 0, "awaiting": "supervisor"}
            self.sessions = {}
            self.r.status("running", "Preparing isolated worktree")
            self.r.timeline("system", "Task started", t["name"])
            self.wt, self.branch = gitops.create_worktree(self.r, t, self.cfg, self.run_dir)
            self.r.timeline("git", "Worktree ready", f"{self.branch} → {self.wt}")
            # Recorded before any agent works, so changes still count once Relay commits them.
            t["base_commit"] = quiet(["git", "rev-parse", "HEAD"], cwd=self.wt).stdout.strip()
        self.base = t.get("base_commit") or gitops.base_commit(self.wt, t.get("repo"))
        repo_full = t.get("github_repo") or github.remote_repo_name(t.get("repo"))
        self.m.set_meta(self.tid, worktree=str(self.wt), branch=self.branch, run_dir=str(self.run_dir), github_repo=repo_full, base_commit=self.base)
        self.repo_full = repo_full

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
        if wf.get("auto_detect_verification", self.cfg.get("auto_detect_verification", True)):
            for c in gitops.detect_verify(self.wt):
                if c not in cmds:
                    cmds.append(c)
        self.verify_cmds = [] if self.verify_mode == "off" else cmds
        self.m.set_meta(self.tid, verify_commands=self.verify_cmds)

    def kickoff(self):
        sup_agent, _ = self.role_agent("supervisor")
        self.handoff("orchestrator", "supervisor", "Task briefing", self.task.get("requirements", ""),
                     subtype="briefing", issue=self.issue_text[:400] if self.issue_text else "")
        guidance = self.take_guidance("supervisor")
        prompt = protocol.supervisor_kickoff(self.task, self.wt, self.branch, self.issue_text, self.refs_text, guidance, self.verify_cmds, self.cfg)
        res, env = self.supervisor_turn(prompt, f"{_label(sup_agent)} is inspecting the repository and planning", turn=0)
        if env.get("type") != "plan":
            if env.get("type") in ("instruction", "decision") and env.get("instruction"):
                env = {"type": "plan", "summary": env.get("summary", ""), "plan": env.get("summary", "") or "(plan given inline)",
                       "acceptance": [], "instruction": env["instruction"]}
            else:
                raise RuntimeError("The supervisor did not produce a plan envelope.")
        plan = {"summary": env.get("summary", ""), "plan": env.get("plan", ""), **protocol.normalize_packet(env)}
        self.state["plan"] = plan
        self.artifact("plan", "PLAN.md", plan["plan"] + "\n\n## Context packet\n\n```\n" + protocol.packet_block(plan) + "\n```\n")
        self.artifact("acceptance", "ACCEPTANCE.md", "\n".join(f"- [ ] {a}" for a in plan["acceptance"]))
        self.m.set_meta(self.tid, plan=plan)
        self.r.msg(role="supervisor", agent=sup_agent, kind="plan", summary=plan["summary"], content=plan["plan"],
                   acceptance=plan["acceptance"], requirements=plan["requirements"], optional=plan["optional"],
                   known_files=plan["known_files"], findings=plan["findings"], constraints=plan["constraints"],
                   unknowns=plan["unknowns"], turn=0)
        self.r.timeline("supervisor", "Plan agreed", plan["summary"] or f"{len(plan['acceptance'])} acceptance criteria")
        self.state.update({"phase": "dialogue", "turn": 1, "awaiting": "worker",
                           "instruction": env.get("instruction") or "Implement the plan.", "instruction_kind": "instruction",
                           "instruction_summary": env.get("summary", "")})
        self.save()

    def dialogue(self):
        sup_agent, _ = self.role_agent("supervisor")
        wrk_agent, _ = self.role_agent("worker")
        while self.state.get("phase") == "dialogue":
            self.r.check_stop()
            turn = int(self.state.get("turn") or 1)
            if self.state.get("awaiting") == "worker":
                if turn > self.max_turns:
                    raise TurnBudget(f"Turn budget exhausted after {self.max_turns} work packages. Raise the limit in the task workflow and resume.")
                kind = self.state.get("instruction_kind") or "instruction"
                title = {"revise": f"Revision request · work package #{turn}", "question": f"Question for the worker · #{turn}"}.get(kind, f"Work package #{turn}")
                self.handoff("supervisor", "worker", title, self.state.get("instruction", ""), subtype=kind,
                             summary=self.state.get("instruction_summary", ""))
                guidance = self.take_guidance("worker")
                sess = self.sessions.get("worker") or {}
                if int(sess.get("turns", 0)) == 0 or sess.get("agent") != wrk_agent:
                    prompt = protocol.worker_kickoff(self.task, self.wt, self.branch, self.issue_text, self.refs_text,
                                                     self.state.get("plan") or {}, self.state.get("instruction", ""), guidance, self.cfg,
                                                     self.gate_command())
                else:
                    prompt = protocol.worker_followup(_label(sup_agent), turn, self.state.get("instruction", ""), guidance, kind)
                if self.state.get("resumed"):
                    prompt = protocol.resume_note({k: v for k, v in self.state.items() if k in ("phase", "turn", "awaiting", "instruction_summary")}) + "\n\n" + prompt
                    self.state["resumed"] = False
                res, env = self.worker_turn(prompt, f"Work package #{turn}", turn)
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
                self.state["last_verification"] = self.run_verification() if self.verify_mode == "each_report" else ""
                self.state["awaiting"] = "supervisor"
                self.save()
                continue

            # supervisor evaluates the report
            changed = gitops.changed_files(self.wt, self.base)
            ds = gitops.diff_stat(self.wt, self.base)
            self.m.set_meta(self.tid, diffstat=ds, changed_count=len(changed))
            guidance = self.take_guidance("supervisor")
            remaining = max(0, self.max_turns - turn)
            prompt = protocol.supervisor_after_report(_label(wrk_agent), turn, self.state.get("report"), (self.state.get("report") or {}).get("report", ""),
                                                      self.state.get("last_verification", ""), changed, ds, guidance, remaining, self.cfg)
            if self.state.get("resumed"):
                prompt = protocol.resume_note({k: v for k, v in self.state.items() if k in ("phase", "turn", "awaiting")}) + "\n\n" + prompt
                self.state["resumed"] = False
            res, env = self.supervisor_turn(prompt, f"{_label(sup_agent)} is evaluating work package #{turn}", turn)
            self.apply_supervisor_decision(env, turn)

    def apply_supervisor_decision(self, env, turn):
        sup_agent, _ = self.role_agent("supervisor")
        typ = env.get("type")
        if typ == "decision" and env.get("decision") == "done":
            self.state["pr_summary"] = env.get("pr_summary") or env.get("summary") or ""
            self.state["summary"] = env.get("summary") or ""
            self.r.msg(role="supervisor", agent=sup_agent, kind="decision", decision="done", summary=env.get("summary", ""),
                       content=self.state["pr_summary"], turn=turn)
            self.r.timeline("supervisor", "Supervisor declared the task complete", env.get("summary", ""))
            # The design gate is enforced at done even when command verification is off.
            if (self.verify_mode == "before_review" and self.verify_cmds) or (self.design_gate and self.verify_mode != "each_report"):
                vt = self.run_verification()
                self.state["last_verification"] = vt
                if not (self.task_meta().get("verification") or {}).get("ok", True):
                    fake = {"type": "review", "verdict": "FAIL", "summary": "Verification commands failed after the done decision.",
                            "findings": [{"severity": "blocking", "file": "(verification)", "problem": "One or more verification commands failed.", "fix": "Make the checks pass."}]}
                    self.r.timeline("verify", "Verification failed after done decision", "Sending results back to the supervisor")
                    _, env2 = self.supervisor_turn(protocol.supervisor_after_review("Orchestrator", fake, vt, 0, 0), "Supervisor triaging failed verification", turn)
                    return self.apply_supervisor_decision(env2, turn)
            rev_agent, _ = self.role_agent("reviewer")
            if rev_agent:
                self.state["phase"] = "review"
                self.state["review_round"] = int(self.state.get("review_round") or 0) + 1
            else:
                self.state["phase"] = "deliver"
            self.save()
            return
        if typ in ("instruction", "question") or (typ == "decision" and env.get("decision") == "revise"):
            kind = "revise" if typ == "decision" else ("question" if typ == "question" else "instruction")
            text = env.get("instruction") or env.get("question") or env.get("summary") or env.get("_text", "")
            if kind == "revise":
                self.r.msg(role="supervisor", agent=sup_agent, kind="decision", decision="revise", summary=env.get("summary", ""),
                           content=text, turn=turn)
                self.r.timeline("supervisor", "Revision requested", env.get("summary", ""))
            else:
                self.r.timeline("supervisor", "Next work package", env.get("summary", ""))
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

    def review(self):
        sup_agent, _ = self.role_agent("supervisor")
        rev_agent, _ = self.role_agent("reviewer")
        while self.state.get("phase") == "review":
            self.r.check_stop()
            rnd = int(self.state.get("review_round") or 1)
            self.r.status("reviewing", f"{_label(rev_agent)} is reviewing independently · round {rnd}/{self.max_review_rounds}")
            vt = self.state.get("last_verification") or ""
            if ((self.verify_cmds and self.verify_mode != "off") or self.design_gate) and not vt:
                vt = self.run_verification()
                self.state["last_verification"] = vt
            diff = gitops.full_diff(self.wt, int(self.cfg.get("budget_diff_chars") or 50000), self.base)
            sess = self.sessions.get("reviewer") or {}
            if int(sess.get("turns", 0)) == 0 or sess.get("agent") != rev_agent:
                prompt = protocol.reviewer_kickoff(self.task, self.wt, self.branch, self.state.get("plan") or {},
                                                   self.state.get("pr_summary", ""), vt, diff, rnd, self.cfg,
                                                   self.state.get("blocked_checks") or [])
            else:
                prompt = protocol.reviewer_followup(rnd, vt, diff, self.state.get("pr_summary", ""))
            self.handoff("orchestrator", "reviewer", f"Independent review requested · round {rnd}", self.state.get("pr_summary", ""), subtype="review_request")
            res, env = self.run_role("reviewer", prompt, f"Review round {rnd}")
            verdict = str(env.get("verdict", "FAIL")).upper()
            findings = env.get("findings") if isinstance(env.get("findings"), list) else []
            text = env.get("_text") or env.get("text") or ""
            review_md = f"# Review round {rnd} · {verdict}\n\n{env.get('summary','')}\n\n" + "\n".join(
                f"- [{f.get('severity','blocking')}] {f.get('file','')}: {f.get('problem','')} → {f.get('fix','')}" for f in findings) + f"\n\n---\n{text}"
            self.artifact("review", "REVIEW.md", review_md)
            self.r.msg(role="reviewer", agent=rev_agent, kind="review", verdict=verdict, summary=env.get("summary", ""),
                       findings=findings, content=text if not findings else "", turn=self.state.get("turn"))
            self.m.set_meta(self.tid, review={"verdict": verdict, "round": rnd, "summary": env.get("summary", ""), "findings": findings})
            self.r.timeline("reviewer", f"Review round {rnd}: {verdict}", env.get("summary", ""))
            if verdict == "PASS":
                self.state["phase"] = "deliver"
                self.save()
                return
            if rnd >= self.max_review_rounds:
                raise RuntimeError(f"The reviewer still blocks delivery after {rnd} review rounds. Inspect the findings, then resume with guidance.")
            self.handoff("reviewer", "supervisor", f"Review findings · round {rnd}", env.get("summary", ""), subtype="review", findings=findings[:30])
            _, senv = self.supervisor_turn(protocol.supervisor_after_review(_label(rev_agent), env, text, rnd, self.max_review_rounds),
                                           f"{_label(sup_agent)} is triaging review findings", self.state.get("turn"))
            if senv.get("type") == "decision" and senv.get("decision") == "done":
                # Supervisor insists it is done; give the reviewer one more look with the supervisor's rebuttal.
                self.state["pr_summary"] = senv.get("pr_summary") or self.state.get("pr_summary", "")
                self.state["review_round"] = rnd + 1
                self.save()
                continue
            self.apply_supervisor_decision(senv, int(self.state.get("turn") or 1))
            if self.state.get("phase") == "dialogue":
                self.dialogue()
                # dialogue ends with phase review (round already incremented) or deliver

    def deliver(self):
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
        cleanup_refs(self.wt)
        cfg = self.cfg
        prefix = (cfg.get("commit_message_prefix") or "agent:").strip()
        title = t.get("github_issue_title") or t["name"]
        committed = gitops.commit_all(self.r, self.wt, f"{prefix} {title}".strip())
        self.r.timeline("git", "Committed" if committed else "Nothing new to commit", self.branch)

        pr = {"url": t.get("pr_url"), "number": t.get("pr_number")}
        if self.repo_full and cfg.get("github_auto_push_on_pass", True):
            self.r.status("delivering", "Pushing the task branch to GitHub")
            gitops.push_branch(self.r, self.wt, self.branch)
            self.r.timeline("github", "Branch pushed", self.branch)
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

        report = self.final_report_md(pr, committed)
        self.artifact("report", "REPORT.md", report)
        self.state["phase"] = "done"
        self.save()
        self.m.complete(self.tid, {"branch": self.branch, "worktree": str(self.wt), "pr_url": pr.get("url"), "pr_number": pr.get("number"),
                                   "summary": self.state.get("summary") or self.state.get("pr_summary") or "", "diffstat": gitops.diff_stat(self.wt, self.base), "changed_count": len(gitops.changed_files(self.wt, self.base))})
        self.r.msg(role="orchestrator", agent=None, kind="complete", content=self.state.get("summary") or "", pr_url=pr.get("url"),
                   branch=self.branch, turn=self.state.get("turn"))

    def details_md(self):
        t = self.task_meta()
        roles = self.roles
        lines = [f"**Workflow:** {_label(roles.get('supervisor',{}).get('agent'))} supervised {_label(roles.get('worker',{}).get('agent'))}"
                 + (f", reviewed independently by {_label(roles.get('reviewer',{}).get('agent'))}" if roles.get('reviewer', {}).get('agent') else ""),
                 f"**Work packages:** {self.state.get('turn', 0)}",
                 f"**Branch:** `{self.branch}`"]
        v = t.get("verification") or {}
        if v.get("items"):
            lines.append("**Verification:** " + ", ".join(f"`{i['command']}` {'✓' if i['ok'] else '✗'}" for i in v["items"]))
        acc = (self.state.get("plan") or {}).get("acceptance") or []
        if acc:
            lines.append("\n**Acceptance criteria**\n" + "\n".join(f"- [x] {a}" for a in acc))
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
{chr(10).join('- ' + a for a in (self.state.get('plan') or {}).get('acceptance', []))}

## Verification
{self.state.get('last_verification') or '(none)'}

## Agent usage
| Agent | Turns | Input tokens | Output tokens | Cost | Time |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

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

    def run(self):
        self.validate_team()
        self.prepare()
        if self.state.get("phase") == "kickoff":
            self.kickoff()
        if self.state.get("phase") == "dialogue":
            self.dialogue()
        if self.state.get("phase") == "review":
            self.review()
        if self.state.get("phase") == "deliver":
            self.deliver()


def orchestrate(task, runner, manager):
    Pipeline(task, runner, manager).run()
