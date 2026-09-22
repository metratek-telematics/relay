"""Triage at run start and the solo fast path.

Solo mode runs ONE agent (the worker role's agent) for the whole request: it writes a short plan and acceptance
criteria first (a file in the run folder), implements, runs the checks closest to its change and reports once.
Relay keeps what makes the result trustworthy without a second agent in the loop:

    verification      the repository's full checks, once, after the report (a failure goes back to the same session)
    acceptance gate   every required criterion needs concrete evidence (one nudge, then follow-ups)
    independent check a single review by another agent, only when the triage or the result says it is risky
    escalation        a worker that finds the task needs a team hands over to a supervisor, keeping its session and tree

Triage runs its cheap rating (when the heuristic is unsure) in parallel with the environment setup, so it costs
no wall time on a repository that installs dependencies.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from . import gitops, judge, protocol, timing, triage
from .util import now, read_text, truncate


_WAIVE = re.compile(r"\b(waive[sd]?|waiving|drop(?:ped)?|remove[sd]?|skip(?:ped)?|not required|no longer required|ignore)\b", re.I)
_DELIVER = re.compile(r"\b(?:make|open|create|raise|deliver|ship|submit)\b[^.\n]{0,40}?\b(?:pr|pull request)\b|"
                      r"\bdeliver (?:it|now|as is|what you have)\b|\bship it\b", re.I)
_NOT_YET = re.compile(r"\b(?:don'?t|do not|not yet|never|before you|until)\b[^.\n]{0,30}\b(?:pr|pull request|deliver|ship)", re.I)
_CRIT_ID = re.compile(r"\b([A-Z]{1,2}\d{1,2})\b")


def waiver_ids(text: str, known: list[str]) -> list[str]:
    """Criterion ids a person's message waives ("waive D1", "D1 is not required"), limited to ids that exist."""
    if not text or not _WAIVE.search(text):
        return []
    return [i for i in dict.fromkeys(_CRIT_ID.findall(text)) if i in known]


def wants_delivery(text: str) -> bool:
    return bool(text and _DELIVER.search(text) and not _NOT_YET.search(text))


class SoloFlow:
    """Pipeline mixin. Relies on run_role, run_verification, record_blocked, criteria, set_criteria, ask_human, r, m, state."""

    # ------------------------------------------------------------ bookkeeping
    def phase_mark(self, phase: str):
        try:
            timing.mark(self.m, self.tid, phase)
        except Exception:
            pass
        self._phase = phase

    def ceremony(self, key: str, run: bool, why: str):
        """Record whether a heavy phase runs and why; shown on the task page."""
        cer = dict(self.state.get("ceremony") or {})
        cer[key] = {"run": bool(run), "why": why}
        self.state["ceremony"] = cer
        self.m.set_meta(self.tid, ceremony=cer)

    def triage_info(self) -> dict:
        return self.state.get("triage") or {}

    def auto_ceremony(self) -> bool:
        """Proportional phases apply unless the task asked for the full team explicitly."""
        return self.triage_info().get("mode_setting", "auto") != "team"

    # ------------------------------------------------------------ what a person's message decides
    def apply_human_text(self, text: str):
        """A person's answer or guidance is a decision, not just context: waivers and "deliver now" take effect in Relay."""
        if not text:
            return
        crit = self.criteria()
        ids = waiver_ids(text, [c["id"] for c in crit])
        if ids:
            out = []
            for c in crit:
                if c["id"] in ids and c.get("status") != "met":
                    c = {**c, "status": "waived", "set_by": "user", "evidence": "waived by the owner: " + truncate(text.strip(), 160)}
                out.append(c)
            self.set_criteria(out)
            self.r.timeline("user", "Waived by you", ", ".join(ids))
        if wants_delivery(text) and not self.state.get("deliver_now"):
            self.state["deliver_now"] = True
            self.save()
            self.r.timeline("user", "Deliver now", "Relay runs its checks and opens the pull request; open items become follow-ups")

    # ------------------------------------------------------------ triage
    def start_triage(self):
        """Kick off the cheap rating in the background before the environment setup, when it will be needed."""
        self._rating = None
        self._rating_thread = None
        if self.state.get("triage"):
            return
        h = triage.heuristic({**self.task, "issue_text": ""}, self.cfg)
        self._heuristic = h
        mode = triage.team_mode(self.task, self.cfg)
        if mode != "auto" or h["confident"] or not self.cfg.get("triage_rating", True):
            return

        def rate():
            try:
                self._rating = triage.cheap_rating(self.task, self.cfg, Path(self.run_dir) / "triage",
                                                   float(self.cfg.get("triage_timeout_seconds") or 90))
            except Exception:
                self._rating = None
        self._rating_started = time.time()
        self._rating_thread = threading.Thread(target=rate, daemon=True)
        self._rating_thread.start()

    def finish_triage(self):
        """Decide the mode (solo or team) and the proportional phases. Resumed runs keep their decision."""
        if self.state.get("triage"):
            return self.state["triage"]
        self.phase_mark("triage")
        h = getattr(self, "_heuristic", None) or triage.heuristic({**self.task, "issue_text": self.issue_text}, self.cfg)
        th = getattr(self, "_rating_thread", None)
        if th is not None:
            if th.is_alive():
                self.r.status("planning", "Triage · rating the task")
            th.join(timeout=max(5.0, float(self.cfg.get("triage_timeout_seconds") or 90) + 10 - (time.time() - getattr(self, "_rating_started", time.time()))))
        rating = getattr(self, "_rating", None)
        tri = triage.decide({**self.task, "repos": [r["name"] for r in self.related]}, self.cfg, h, rating)
        self.state["triage"] = tri
        self.m.set_meta(self.tid, triage=tri)
        self.ceremony("mode", tri["mode"] == "team", tri["why"])
        detail = f"{tri['mode']} · {tri['level']} · {tri['why']}"
        self.r.timeline("system", f"Triage · {'Solo mode' if tri['mode'] == 'solo' else 'Team mode'}", truncate(detail, 300))
        self.r.msg(role="orchestrator", agent=None, kind="notice", turn=0,
                   content=(f"**Triage: {tri['mode']} mode** ({tri['level']}). {tri['why']}."
                            + (" One agent plans, builds and proves the change; Relay verifies it"
                               + (" and brings in a second agent only if the result looks risky." if tri['mode'] == "solo" else ".")
                               if tri["mode"] == "solo" else " Supervisor, worker and the phases below run as the task needs them.")
                            + (f"\n\nRating: {rating.get('agent')} {rating.get('model') or ''} in {rating.get('seconds')} s: {rating.get('reason')}" if rating else "")
                            + "\n\nChange it per task with **Team mode** (Auto, Solo, Team)."))
        self.save()
        return tri

    # ------------------------------------------------------------ the solo phase
    def solo_plan_file(self) -> Path:
        return Path(self.run_dir) / "SOLO_PLAN.md"

    def solo_turn(self, prompt: str, label: str, turn: int):
        self.phase_mark("build")
        self.r.status("implementing", f"{self.role_label('worker')} is working alone · {label}")
        res, env = self.run_role("worker", prompt, label, turn, expect={"report", "question"})
        while env.get("type") == "question":
            follow = self.ask_human("worker", {**env, "to": "user"})
            self.r.status("implementing", f"{self.role_label('worker')} continues")
            res, env = self.run_role("worker", follow, label, turn, expect={"report", "question"})
        return res, env

    def role_label(self, role: str) -> str:
        from .pipeline import _label
        return _label(self.role_agent(role)[0])

    def solo_record_report(self, env: dict, turn: int) -> dict:
        report = {"status": env.get("status", "complete"), "summary": env.get("summary", ""),
                  "report": env.get("report") or env.get("_text", ""), "files": env.get("files") or [],
                  "blocked_checks": protocol.blocked_checks(env), "blockers": protocol.blockers(env)}
        self.record_blocked(report["blocked_checks"])
        self.state["report"] = report
        self.artifact("implementation", "IMPLEMENTATION.md", f"# Solo report #{turn} · {report['status']}\n\n{report['report']}")
        self.handoff("worker", "orchestrator", f"Report · solo #{turn} · {report['status']}", report["report"],
                     subtype="report", status=report["status"], summary=report["summary"], files=report["files"][:60],
                     blocked_checks=report["blocked_checks"])
        self.r.timeline("worker", f"Solo work reported · {report['status']}", report["summary"])
        # Plan and contract: from the envelope, else from the plan file the worker wrote first.
        plan = dict(self.state.get("plan") or {})
        text = str(env.get("plan") or "").strip() or read_text(self.solo_plan_file()) if self.solo_plan_file().exists() else str(env.get("plan") or "")
        if text:
            plan.update(summary=plan.get("summary") or env.get("summary", ""), plan=text)
        rows = env.get("acceptance") if isinstance(env.get("acceptance"), list) else []
        if rows:
            criteria = judge.normalize_acceptance(rows, source="worker")
            old = {c["id"]: c for c in self.criteria()}
            merged = []
            for c in criteria:
                prev = old.get(c["id"])
                if prev and prev.get("set_by") == "user":
                    merged.append(prev)   # the human's verdict or waiver stands
                    continue
                merged.append({**c, "set_by": "worker"})
            merged += [c for c in old.values() if c.get("source") == "user" and c["id"] not in {m["id"] for m in merged}]
            self.set_criteria(merged)
            plan["acceptance"] = judge.criterion_texts(merged)
        if plan:
            self.state["plan"] = plan
            self.m.set_meta(self.tid, plan=plan)
            if text:
                self.artifact("plan", "PLAN.md", text)
        cx = env.get("complexity")
        if isinstance(cx, dict) and cx.get("level"):
            self.m.set_meta(self.tid, complexity={"level": str(cx["level"])[:20], "reason": truncate(str(cx.get("reason") or ""), 300), "by": "worker"})
        self.state["pr_summary"] = env.get("pr_summary") or env.get("summary") or self.state.get("pr_summary", "")
        self.state["summary"] = env.get("summary") or self.state.get("summary", "")
        self.add_followups(env.get("follow_ups") or env.get("followups"), "worker")
        self.save()
        return report

    def solo_phase(self):
        """One agent from plan to proof; Relay verifies, gates and (only when warranted) gets a second look."""
        wrk = self.role_agent("worker")[0]
        turn = int(self.state.get("turn") or 1)
        self.state.setdefault("turn", turn)
        if not self.state.get("solo_started"):
            self.state["solo_started"] = now()
            self.r.timeline("system", "Solo mode", f"{self.role_label('worker')} plans, builds and proves the change")
            prompt = protocol.solo_kickoff(self.task, self.wt, self.branch, self.issue_text, self.refs_text, self.take_guidance("worker"),
                                           self.cfg, self.gate_command(), self.env_text(), self.tools_text("worker"), self.verify_cmds,
                                           plan_file=self.solo_plan_file())
            prompt = protocol.with_lessons(prompt, self.lessons_text)
            prompt = protocol.with_block(prompt, self.playbook_text)
            prompt = protocol.with_block(prompt, self.context_text("worker"))
        else:
            prompt = protocol.resume_note({k: v for k, v in self.state.items() if k in ("phase", "turn", "solo_stage")}) + \
                "\n\nFinish the task and report with the solo report envelope."
        self.handoff("orchestrator", "worker", "Task briefing (solo)", self.task.get("requirements", ""), subtype="briefing")
        _, env = self.solo_turn(prompt, "Solo work", turn)
        report = self.solo_record_report(env, turn)
        fix_rounds = max(0, int(self.cfg.get("solo_fix_rounds") or 2))
        gate_nudges = max(0, int(self.cfg.get("solo_gate_nudges") or 1))
        used_fix = used_gate = 0
        while True:
            self.r.check_stop()
            if env.get("needs_team") or report["status"] == "blocked":
                why = "the engineer asked for a team" if env.get("needs_team") else "the engineer reported it is blocked"
                if self.escalate_to_team(why, report):
                    return
            actionable = [b for b in report["blockers"] + report["blocked_checks"] if b.get("action_required")]
            for b in actionable:
                what = b.get("check") or "Work"
                self.ask_human("worker", {"question": f"{what} is blocked: {b['reason']}" + (f"\nImpact: {b['impact']}" if b.get("impact") else "")
                                          + "\nHow should the team proceed?", "options": ["Continue without it", "I have fixed it, retry", "Stop the task"]})
            # Evidence first: a nudge for proof can change files, so it comes before the (slow) checks, not after them.
            criteria = self.criteria()
            if criteria and used_gate < gate_nudges:
                pre = judge.done_gate(criteria, None, check_verification=False)
                unproven = [m for m in pre["missing"] if m["id"] != "verification"]
                if unproven:
                    used_gate += 1
                    turn += 1
                    self.state["turn"] = turn
                    ids = ", ".join(m["id"] for m in unproven)
                    self.r.msg(role="orchestrator", agent=None, kind="gate", ok=False, missing=unproven, turn=turn, content=f"Done refused: {ids} not proven")
                    self.r.timeline("judge", "Done refused · criteria not proven", ids)
                    rows = "\n".join(f"- {m['id']}: {m['criterion']} · {m['reason']}" for m in unproven)
                    _, env = self.solo_turn(protocol.solo_followup("gate", rows + "\n\nCURRENT CONTRACT\n" + judge.acceptance_block(self.criteria()),
                                                                   self.take_guidance("worker")), "Proving the acceptance criteria", turn)
                    report = self.solo_record_report(env, turn)
                    continue
            vt = self.run_verification()
            self.state["last_verification"] = vt
            failing = judge.failed_checks(self.task_meta().get("verification"))
            if failing and used_fix < fix_rounds:
                used_fix += 1
                turn += 1
                self.state["turn"] = turn
                self.r.timeline("verify", "Verification failed · back to the engineer", ", ".join(f.get("command") or "" for f in failing))
                _, env = self.solo_turn(protocol.solo_followup("verify", vt, self.take_guidance("worker")), f"Fixing verification #{used_fix}", turn)
                report = self.solo_record_report(env, turn)
                continue
            if failing:
                self.record_blocked([{"check": f.get("command") or "Verification", "action_required": True,
                                      "reason": "failed in Relay's verification at delivery and not on the starting commit",
                                      "impact": "delivered with this check failing; fix before merging"} for f in failing])
                self.add_followups([{"severity": "should_fix", "problem": f"Verification failing at delivery: {f.get('command')} (exit {f.get('rc')})"}
                                    for f in failing], "verification")
            missing = []
            if self.criteria():
                gate = judge.done_gate(self.criteria(), self.task_meta().get("verification"), check_verification=True)
                self.set_criteria(gate["criteria"])
                missing = [m for m in gate["missing"] if m["id"] != "verification"]
                if missing:
                    self.add_followups([{"severity": "should_fix", "problem": f"{m['id']} not proven at delivery: {m['criterion']} ({m['reason']})"}
                                        for m in missing], "acceptance gate")
            break
        self.solo_independent_check(len(missing))
        if self.state.get("phase") == "solo":
            self.state["phase"] = "deliver"
            self.save()

    # ------------------------------------------------------------ escalation
    def escalate_to_team(self, why: str, report: dict) -> bool:
        """Hand a solo run to the full team, keeping the worker's session and working tree."""
        if not self.role_agent("supervisor")[0]:
            return False
        self.r.timeline("system", "Escalated to team mode", why)
        self.r.msg(role="orchestrator", agent=None, kind="notice", turn=self.state.get("turn"),
                   content=f"Solo run escalated to the team: {why}. The supervisor continues from the engineer's working tree and report.")
        tri = dict(self.triage_info())
        tri.update(mode="team", why=f"escalated from solo: {why}")
        self.state["triage"] = tri
        self.m.set_meta(self.tid, triage=tri)
        self.ceremony("mode", True, tri["why"])
        self.state["solo_handoff"] = (f"A solo attempt by {self.role_label('worker')} ran first and stopped: {why}.\n"
                                      f"Its report ({report.get('status')}): {truncate(report.get('summary') or '', 300)}\n"
                                      f"{truncate(report.get('report') or '', 3000)}\n"
                                      "Its changes are in the working tree (git status, git diff). Plan the remaining work only; "
                                      "the worker keeps its session and knows what it did.")
        self.state["phase"] = "kickoff"
        self.save()
        return True

    def solo_handoff_note(self) -> str:
        note = self.state.get("solo_handoff") or ""
        return ("SOLO ATTEMPT BEFORE YOU\n" + note) if note else ""

    # ------------------------------------------------------------ independent check
    def checker_role(self) -> tuple[str, str]:
        """(role, session key) for the light independent check: the reviewer, else the supervisor's agent in a fresh session."""
        if self.role_agent("reviewer")[0]:
            return "reviewer", "checker"
        if self.role_agent("supervisor")[0]:
            return "supervisor", "checker"
        return "", ""

    def solo_independent_check(self, unproven: int = 0):
        tri = self.triage_info()
        changed = self.all_changed_files()
        ds = self.all_diffstat()
        want, reasons = triage.check_wanted(tri, ds, changed, unproven=unproven)
        if not self.cfg.get("solo_independent_check", True):
            want, reasons = False, ["independent checks are off in settings"]
        role, key = self.checker_role()
        if want and not role:
            want, reasons = False, ["no second agent is configured"]
        self.ceremony("review", want, "; ".join(reasons) if reasons else "low risk: Relay's checks and the acceptance gate are enough")
        if not want:
            return
        self.phase_mark("review")
        agent = self.role_agent(role)[0]
        self.r.timeline("system", "Independent check", "; ".join(reasons))
        self.r.status("reviewing", f"{self.role_label(role)} is checking the result")
        crit = self.criteria()
        prompt = protocol.reviewer_kickoff(self.task, self.wt, self.branch, {**(self.state.get("plan") or {}), "criteria": crit},
                                           self.state.get("pr_summary", ""), self.state.get("last_verification", ""),
                                           self.review_diff({}, agent), 1, self.cfg, self.state.get("blocked_checks") or [],
                                           judge.acceptance_block(crit) if crit else "", self.tools_text("reviewer"))
        prompt += ("\n\nLIGHT CHECK · this is the only review of a solo run. Look for concrete defects against the request: wrong "
                   "behaviour, a missed requirement, a regression, a security problem. Keep it short; do not re-run the full suite.")
        try:
            _, env = self.run_role(role, prompt, "Independent check", self.state.get("turn"), expect={"review"}, session_key=key)
        except Exception as e:
            from .runner import Interrupted, Stopped
            if isinstance(e, (Stopped, Interrupted)):
                raise
            self.record_blocked([{"check": "Independent check", "action_required": False, "reason": truncate(str(e), 300),
                                  "impact": "the solo change was not independently checked"}])
            return
        findings = judge.normalize_findings(env.get("findings") if isinstance(env.get("findings"), list) else [])
        blocking, minor = judge.split_findings(findings)
        self.add_followups(minor, "independent check")
        verdict = "FAIL" if blocking else "PASS"
        self.r.msg(role="reviewer", agent=agent, kind="review", verdict=verdict, summary=env.get("summary", ""), findings=findings,
                   turn=self.state.get("turn"))
        self.m.set_meta(self.tid, review={"verdict": verdict, "round": 1, "summary": env.get("summary", ""), "findings": findings})
        self.artifact("review", "REVIEW.md", f"# Independent check · {verdict}\n\n{env.get('summary', '')}\n\n" + "\n".join(
            f"- [{f.get('severity')}] {f.get('file', '')}: {f.get('problem', '')} → {f.get('fix', '')}" for f in findings))
        self.r.timeline(role, f"Review round 1: {verdict}", env.get("summary", ""))
        if not blocking:
            return
        turn = int(self.state.get("turn") or 1) + 1
        self.state["turn"] = turn
        rows = "\n".join(f"- [{f.get('id', '')}] {f.get('file', '')}: {f.get('problem', '')}" + (f" → {f['fix']}" if f.get("fix") else "") for f in blocking)
        before = self.fingerprint()
        _, env2 = self.solo_turn(protocol.solo_followup("check", rows, self.take_guidance("worker")), "Fixing the check's findings", turn)
        self.solo_record_report(env2, turn)
        if self.fingerprint() != before:
            self.state["last_verification"] = self.run_verification()
            failing = judge.failed_checks(self.task_meta().get("verification"))
            if failing:
                self.record_blocked([{"check": f.get("command") or "Verification", "action_required": True,
                                      "reason": "failed after fixing the independent check's findings",
                                      "impact": "delivered with this check failing; fix before merging"} for f in failing])
        self.r.timeline("worker", "Independent check findings addressed", f"{len(blocking)} blocking finding(s) fixed in one round; not re-reviewed")
