"""The learning engine: turns finished runs into better next runs.

    task created ──► preflight: team recommendation + risk prediction, stored on the task
    task starts  ──► prompt/rule version hashes · relevant lessons (lessons.kickoff_block) · playbook (planning)
    autopilot    ──► auto-pick team (optional, ε-exploration, never for critical tasks)
    task ends    ──► scorecard (learning.py) ──► outcome record ──► autopsy + improvement proposals
    retro done   ──► autopsy re-read with the retrospective · lesson support / auto-approval
    periodic     ──► reverts of merged pull requests · playbook seeding and cheap agent refresh

The math lives in pure modules, each with its formula in its docstring:
    outcomes.py (dataset) · recommend.py (teams) · risk.py (pre-flight, calibration)
    autopsy.py (root cause, proposals) · lesson_effect.py (effect, relevance, support) · playbooks.py

State under DATA_DIR/learning: outcomes.jsonl, proposals.json, playbooks/.
"""
from __future__ import annotations

import copy
import logging
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import autopsy, config as C, lesson_effect, lessons, outcomes, playbooks, recommend, risk, scorecard
from .autopilot import limit_state
from .learning_defaults import DEFAULTS
from .outcomes import LEARNING_DIR
from .util import APP_DIR, RUNTIME_DIR, new_id, now, read_json, read_text, truncate, write_json

log = logging.getLogger("relay.learning")

PROPOSALS_FILE = LEARNING_DIR / "proposals.json"



def _read(path) -> str:
    try:
        return read_text(path) or ""
    except OSError:
        return ""


def _already_installed(p: dict) -> bool:
    from . import repo_env
    path = p.get("repo_path")
    if not path or not Path(path).exists():
        return False
    try:
        have = set(repo_env.load(path).get("system_packages") or [])
    except Exception:
        return False
    return set(p.get("packages") or []) <= have


def parse_retro_md(text: str) -> dict:
    """The lists of a RETRO.md (orchestrator/retro.py `markdown`), for runs whose task record is gone."""
    out, cur = {}, None
    keys = {"what went well": "what_went_well", "what went wrong": "what_went_wrong", "root causes": "root_causes"}
    for line in (text or "").splitlines():
        if line.startswith("## "):
            cur = keys.get(line[3:].strip().lower())
            continue
        if cur and line.startswith("- "):
            out.setdefault(cur, []).append(line[2:].strip())
    return out


def settings(cfg: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in (cfg.get("learning") or {}).items() if k in DEFAULTS})
    return out


class LearningEngine:
    def __init__(self, manager):
        self.m = manager
        self.store = outcomes.OutcomeStore()
        self._lock = threading.RLock()
        self._effects = (None, None)
        self._rng = random.Random()
        self._playbook_busy = set()

    # ------------------------------------------------------------ basics
    def settings(self) -> dict:
        return settings(self.m.cfg())

    def records(self) -> list[dict]:
        """Every recorded run, including tasks deleted from the list afterwards: what happened still teaches."""
        return self.store.all()

    # ------------------------------------------------------------ task start
    def task_started(self, tid: str, rule_names=None):
        """Stamp the prompt and rule versions a run starts with (once per run)."""
        t = self.m.store.get(tid) or {}
        pv = t.get("prompt_versions") or {}
        if pv.get("run") == t.get("started_at"):
            return
        try:
            v = outcomes.prompt_versions(APP_DIR, rule_names)
            self.m.store.update(tid, touch=False, prompt_versions={**v, "run": t.get("started_at")})
        except Exception:
            log.exception("prompt versions for %s", tid)

    def playbook_for_task(self, task: dict) -> str:
        if not self.settings().get("playbooks_inject", True):
            return ""
        key = scorecard.repo_key(task)
        pb = playbooks.load(key)
        if not pb:
            try:
                pb = self.seed_playbook(key, task.get("repo") or "", quiet=True)
            except Exception:
                return ""
        block = playbooks.prompt_block(pb)
        if block:
            self.m.set_meta(task["id"], playbook_used=True)
        return block

    # ------------------------------------------------------------ outcomes
    def _messages(self, tid):
        if tid in getattr(self.m, "runners", {}) or tid in getattr(self.m.store, "_messages", {}):
            return self.m.store.messages(tid)
        return scorecard.read_messages(RUNTIME_DIR / tid / "messages.jsonl")

    def record(self, tid: str, card: dict | None = None, run_autopsy: bool = True) -> dict | None:
        """Append the outcome of a finished run; diagnose it when it failed or scored low. Never raises."""
        try:
            t = self.m.store.get(tid)
            card = card or (t or {}).get("scorecard")
            if not t or not card:
                return None
            msgs = self._messages(tid)
            if run_autopsy:
                self.diagnose(tid, t, card, msgs)
                t = self.m.store.get(tid) or t
            rec = outcomes.build(t, card, msgs)
            self.store.append(rec)
            self._effects = (None, None)
            return rec
        except Exception:
            log.exception("outcome record for %s failed", tid)
            return None

    def backfill(self) -> int:
        """Outcome records for every scored run that has none (task records first, then orphan scorecards)."""
        have = {r["run_key"] for r in self.store.all()}
        n = 0
        seen = set()
        for t in self.m.store.list():
            card = t.get("scorecard") or self.m.learning.cards.get(t["id"])
            if not card or card.get("finished_at") != t.get("finished_at"):
                continue
            seen.add(t["id"])
            if f"{t['id']}@{t.get('started_at') or card.get('started_at') or ''}" in have:
                continue
            if self.record(t["id"], card):
                n += 1
        for card in self.m.learning.cards.all():
            if card.get("task_id") in seen or card.get("outcome") is None:
                continue
            tid = card["task_id"]
            if f"{tid}@{card.get('started_at') or ''}" in have:
                continue
            # The task record is gone (deleted): rebuild what we can from the card, its message log and RETRO.md.
            msgs = scorecard.read_messages(RUNTIME_DIR / tid / "messages.jsonl")
            task = {"id": tid, "name": card.get("name"), "repo": card.get("repo_path"), "github_repo": card.get("github_repo"),
                    "started_at": card.get("started_at"), "finished_at": card.get("finished_at"), "template": card.get("template"),
                    "error": card.get("failure_detail") or "", "retro": parse_retro_md(_read(RUNTIME_DIR / tid / "RETRO.md") or "")}
            res = None
            if autopsy.needs_autopsy(card, int(self.settings().get("autopsy_score_threshold") or 60), task):
                res = self.diagnose(tid, task, card, msgs, persist=False)
            rec = outcomes.build({**task, "autopsy": res}, card, msgs)
            rec.update(deleted=True, lessons_known=False)
            if self.store.append(rec):
                n += 1
        return n

    # ------------------------------------------------------------ autopsy + proposals
    def diagnose(self, tid: str, task: dict, card: dict, messages=None, persist: bool = True) -> dict | None:
        s = self.settings()
        if not autopsy.needs_autopsy(card, int(s.get("autopsy_score_threshold") or 60), task):
            if task.get("autopsy") and persist:
                self.m.store.update(tid, touch=False, autopsy=None)
            return None
        messages = messages if messages is not None else self._messages(tid)
        extra = ""
        vpath = (task.get("artifacts") or {}).get("verification")
        if vpath:
            extra = truncate(_read(Path(vpath)), 20000, tail=True)
        retro = task.get("retro") or {}
        extra += "\n" + "\n".join(str(x) for k in ("what_went_wrong", "root_causes") for x in retro.get(k) or [])
        res = autopsy.classify(card, task, messages, autopsy.evidence_text(task, messages, extra), retro)
        props = list(res.pop("proposals"))
        if res["cause"] == "agent_capability":
            props = self._team_proposal(task, card) + props
        stored = self._file_proposals(tid, task, card, res, props)
        res.update(time=now(), finished_at=card.get("finished_at"), proposals=[p["id"] for p in stored])
        if not persist:
            return res
        prev = task.get("autopsy") or {}
        # The scorecard refresh re-reads finished runs every half hour and files the proposals again under new ids;
        # only a different diagnosis is news on the timeline (the same one was appended 300+ times to a single task).
        res["signature"] = "|".join([str(res.get("cause")), str(res.get("finished_at"))]
                                    + sorted(str(p.get("text") or p.get("kind") or "") for p in stored))
        self.m.store.update(tid, touch=False, autopsy=res)
        if stored and prev.get("signature") != res["signature"] and not (not prev.get("signature") and prev.get("cause") == res.get("cause")
                                                                           and prev.get("finished_at") == res.get("finished_at")):
            self.m.timeline(tid, "system", f"Autopsy · {res['label'].lower()}", f"{len(stored)} improvement proposal(s) in Learning")
            self.m.emit("learning", {"proposals": self.open_proposals_count()})
        return res

    def _team_proposal(self, task: dict, card: dict) -> list[dict]:
        ctx = self._ctx_from_task(task)
        cur = outcomes.team_key(card.get("team") or {})
        ranking = self.rank(ctx, current_roles=(task.get("workflow") or {}).get("roles"), mode="best")
        alt = next((e for e in ranking["candidates"] if e["team_key"] != cur and e["available"] and e["runs"]), None)
        mine = next((e for e in ranking["candidates"] if e["team_key"] == cur), None)
        if not alt or (mine and alt["expected_score"] <= mine["expected_score"] + 3):
            return []
        return [{"kind": "team_default", "title": f"Use {outcomes.team_label(alt['team_key'], C.AGENTS)} for {card.get('repo_label') or 'this repository'}",
                 "detail": alt["why"] + (f" (current team: expected {mine['expected_score']:.0f})" if mine else ""),
                 "repo": card.get("repo"), "team_key": alt["team_key"]}]

    def _file_proposals(self, tid, task, card, res, props) -> list[dict]:
        with self._lock:
            rows = read_json(PROPOSALS_FILE, []) or []
            # A new diagnosis of the same run replaces its open proposals.
            rows = [p for p in rows if not (p.get("task_id") == tid and p.get("status") == "proposed")]
            stored = []
            for p in props:
                if p.get("kind") == "system_packages" and _already_installed(p):
                    continue  # the environment already lists them (fixed since this run)
                sig = (p.get("kind"), p.get("repo") or p.get("repo_path") or "", p.get("text") or p.get("packages") or p.get("command") or p.get("team_key") or p.get("path"))
                dup = next((x for x in rows if x.get("status") in ("proposed", "applied") and
                            (x.get("kind"), x.get("repo") or x.get("repo_path") or "", x.get("text") or x.get("packages") or x.get("command") or x.get("team_key") or x.get("path")) == sig), None)
                if dup:
                    if tid not in dup.setdefault("task_ids", []):
                        dup["task_ids"].append(tid)
                    if dup.get("status") == "applied":
                        dup["recurred_after_apply"] = int(dup.get("recurred_after_apply") or 0) + 1
                    continue
                row = {**p, "id": new_id("p_"), "status": "proposed", "created_at": now(), "task_id": tid, "task_ids": [tid],
                       "task_name": task.get("name"), "cause": res["cause"], "cause_label": res["label"], "confidence": res["confidence"],
                       "evidence": res.get("evidence") or [], "repo": p.get("repo") or card.get("repo"), "repo_label": card.get("repo_label")}
                rows.append(row)
                stored.append(row)
            write_json(PROPOSALS_FILE, rows[-500:])
            return stored

    def proposals(self) -> list[dict]:
        return read_json(PROPOSALS_FILE, []) or []

    def open_proposals_count(self) -> int:
        return sum(1 for p in self.proposals() if p.get("status") == "proposed")

    def dismiss_proposal(self, pid: str) -> dict:
        with self._lock:
            rows = self.proposals()
            p = next((x for x in rows if x.get("id") == pid), None)
            if not p:
                raise KeyError("Proposal not found")
            p.update(status="dismissed", dismissed_at=now())
            write_json(PROPOSALS_FILE, rows)
            return p

    def apply_proposal(self, pid: str) -> dict:
        """One click: make the proposed change. Returns the updated proposal with what was done."""
        from . import repo_env
        with self._lock:
            rows = self.proposals()
            p = next((x for x in rows if x.get("id") == pid), None)
            if not p:
                raise KeyError("Proposal not found")
            if p.get("status") == "applied":
                return p
            kind = p.get("kind")
            if kind == "system_packages":
                path = p.get("repo_path")
                if not path or not Path(path).exists():
                    raise ValueError("The repository folder no longer exists on this machine.")
                env = repo_env.public(repo_env.load(path))
                pkgs = list(dict.fromkeys(list(env.get("system_packages") or []) + list(p.get("packages") or [])))
                repo_env.save(path, {**env, "system_packages": pkgs})
                done = f"System packages for {Path(path).name}: {' '.join(pkgs)}"
            elif kind == "lesson":
                scope = p.get("scope") or ("repo" if p.get("repo") else "global")
                row = lessons.add(p["text"], scope, p.get("repo") or "", p.get("category") or "", source="autopsy",
                                  evidence="; ".join(p.get("evidence") or [])[:500])
                p["lesson_id"] = row["id"]
                done = f"Lesson added ({lesson_effect.CATEGORY_LABEL.get(row.get('category'), row.get('category'))})"
                self.m.emit("lessons", {"pending": lessons.pending_count()})
            elif kind == "rule_tweak":
                name = Path(str(p.get("rule") or "")).stem
                path = APP_DIR / "rules" / f"{name}.md"
                if not name or not path.exists():
                    raise ValueError(f"Rule file {name}.md not found")
                text = read_text(path) or ""
                line = f"- {p['text']}"
                if p["text"] not in text:
                    head = "\n## Learned from outcomes\n"
                    text = text.rstrip() + ("\n" if head in text else "\n" + head) + line + "\n"
                    path.write_text(text, encoding="utf-8")
                done = f"rules/{name}.md updated"
            elif kind == "optional_check":
                path = p.get("repo_path")
                if not path or not Path(path).exists():
                    raise ValueError("The repository folder no longer exists on this machine.")
                env = repo_env.public(repo_env.load(path))
                checks = list(env.get("checks") or [])
                hit = next((c for c in checks if c.get("command") == p["command"]), None)
                if hit:
                    hit["required"] = False
                else:
                    checks.append({"command": p["command"], "required": False, "name": ""})
                repo_env.save(path, {**env, "checks": checks})
                done = f"`{p['command']}` is optional for {Path(path).name}"
            elif kind == "setting":
                cfg = self.m.cfg()
                path = list(p.get("path") or [])
                cur = cfg
                for k in path[:-1]:
                    cur = (cur or {}).get(k) or {}
                old = (cur or {}).get(path[-1])
                value = p["value"] if "value" in p else (float(old or 0) + float(p.get("delta") or 0))
                if isinstance(old, int) and not isinstance(old, bool) and isinstance(value, float) and value.is_integer():
                    value = int(value)
                patch = value
                for k in reversed(path):
                    patch = {k: patch}
                C.update(patch)
                self.m.config_changed()
                done = f"{'.'.join(path)}: {old} → {value}"
            elif kind == "team_default":
                teams = dict(self.settings().get("repo_teams") or {})
                teams[p["repo"]] = p["team_key"]
                C.update({"learning": {"repo_teams": teams}})
                self.m.config_changed()
                done = f"{outcomes.team_label(p['team_key'], C.AGENTS)} pinned for {p.get('repo_label') or p['repo']}"
            else:
                raise ValueError(f"Unknown proposal kind {kind}")
            p.update(status="applied", applied_at=now(), applied_result=done)
            write_json(PROPOSALS_FILE, rows)
        self.m.emit("learning", {"proposals": self.open_proposals_count()})
        if p.get("repo"):
            try:
                self.seed_playbook(p["repo"], p.get("repo_path") or "", quiet=True)
            except Exception:
                pass
        return p

    def after_retro(self, tid: str):
        """The retrospective may reveal the cause (unclear scope); re-read the autopsy and check lesson support."""
        t = self.m.store.get(tid) or {}
        card = t.get("scorecard")
        if card and t.get("autopsy") and (t.get("retro") or {}).get("status") == "done":
            try:
                self.diagnose(tid, t, card)
                self.record(tid, card, run_autopsy=False)
            except Exception:
                log.exception("autopsy after retro for %s", tid)
        self.auto_approve()

    def auto_approve(self) -> list[dict]:
        s = self.settings()
        if not s.get("auto_approve_lessons"):
            return []
        need = max(2, int(s.get("auto_approve_min_tasks") or 3))
        out = []
        for row in lessons.listing()["queue"]:
            if lessons.support_count(row) >= need:
                try:
                    out.append(lessons.approve(row["id"], auto=True))
                except Exception:
                    log.exception("auto-approving lesson %s", row.get("id"))
        if out:
            self.m.emit("lessons", {"pending": lessons.pending_count()})
        return out

    # ------------------------------------------------------------ lessons
    def lesson_effects(self) -> dict:
        key = self.store._mtime
        recs = self.records()
        if self._effects[0] is not None and self._effects[0] == (key, len(recs)):
            return self._effects[1]
        n = int(self.settings().get("effect_min_tasks") or 3)
        eff = {x["id"]: lesson_effect.effect(x, recs, n) for x in lessons.approved()}
        self._effects = ((key, len(recs)), eff)
        return eff

    def lessons_report(self) -> dict:
        eff = self.lesson_effects()
        rows = []
        for x in lessons.approved():
            e = eff.get(x["id"]) or {}
            rows.append({"id": x["id"], "text": x["text"], "scope": x["scope"], "repo": x.get("repo"), "category": x.get("category") or lesson_effect.categorize(x["text"]),
                         "enabled": x.get("enabled", True), "retired_at": x.get("retired_at"), "source": x.get("source"), "auto_approved": x.get("auto_approved"),
                         "approved_at": x.get("approved_at"), **{k: e.get(k) for k in ("with", "without", "delta", "verdict", "retire", "first_pass_delta", "revisions_delta")}})
        need = max(2, int(self.settings().get("auto_approve_min_tasks") or 3))
        queue = [{"id": q["id"], "text": q["text"], "support": lessons.support_count(q), "high_confidence": lessons.support_count(q) >= need,
                  "category": q.get("category") or lesson_effect.categorize(q["text"])} for q in lessons.listing()["queue"]]
        order = {"hurts": 0, "no_effect": 1, "helps": 2, "unproven": 3}
        rows.sort(key=lambda r: (order.get(r.get("verdict"), 4), r.get("approved_at") or ""))
        return {"lessons": rows, "queue": queue, "categories": lesson_effect.CATEGORY_LABEL}

    # ------------------------------------------------------------ recommendations + risk
    def _available(self, key: str) -> bool:
        ap = getattr(self.m, "autopilot", None)
        for r in outcomes.team_from_key(key).values():
            if r["agent"] not in C.AGENTS:
                return False
            if r.get("provider") == "openrouter":
                from . import openrouter as OR
                if not OR.supports(r["agent"]) or not OR.capacity_state(r["model"], None, None, OR.account_cached()).get("ok"):
                    return False
                continue
            if ap:
                try:
                    # Signed in and installed always; limits only when an account reading is already cached (never slow).
                    acc = (getattr(ap, "_acc_cache", {}).get(r["agent"]) or (0, None))[1]
                    st = limit_state(r["agent"], r["model"], acc, ap._health(r["agent"]),
                                     float(ap.settings().get("limit_threshold_percent") or 90), time.time())
                    if not st.get("ok"):
                        return False
                except Exception:
                    pass
        return True

    def rank(self, ctx: dict, current_roles: dict | None = None, mode: str | None = None, available=True) -> dict:
        cfg = self.m.cfg()
        s = settings(cfg)
        recs = self.records()
        default_roles = copy.deepcopy(cfg.get("roles") or {})
        preset = C.preset(cfg.get("workflow_preset"))
        if preset:
            for r, v in preset["roles"].items():
                default_roles.setdefault(r, {})["agent"] = v.get("agent", "")
        cands = recommend.candidate_keys(recs, C.PRESETS, default_roles, current_roles)
        pinned = (s.get("repo_teams") or {}).get(ctx.get("repo") or "")
        return recommend.rank(recs, cands, ctx, mode or s.get("recommend_mode"), self._available if available else None, C.AGENTS, pinned)

    def _ctx_from_task(self, task: dict) -> dict:
        key = scorecard.repo_key(task)
        sig = outcomes.request_signals(task.get("requirements") or "", task.get("issue") or "", len(task.get("repos") or []) or 1)
        return {"repo": key, "repo_label": scorecard.repo_label(key), "template": task.get("template") or "feature",
                "size": sig["size"], "requirements": task.get("requirements") or "", "issue": task.get("issue") or "",
                "repos": sig["repos"], "roles": (task.get("workflow") or {}).get("roles") or {}}

    def repo_autopsies(self, repo: str) -> list[dict]:
        props = [p for p in self.proposals() if p.get("repo") == repo]
        out = []
        for r in self.records():
            if r.get("repo") != repo or not r.get("autopsy_cause"):
                continue
            mine = [p for p in props if r.get("task_id") in (p.get("task_ids") or [])]
            out.append({"task_id": r.get("task_id"), "cause": r["autopsy_cause"], "fixed": any(p.get("status") == "applied" for p in mine)})
        return out

    def preflight(self, payload: dict) -> dict:
        """Recommendation and risk for a draft task (the wizard) or a new task."""
        repo = (payload.get("repo") or "").strip()
        from . import github
        gh = payload.get("github_repo") or (github.remote_repo_name(repo) if repo and Path(repo).exists() else "")
        task_like = {"repo": repo, "github_repo": gh, "requirements": payload.get("requirements") or "", "issue": payload.get("issue") or "",
                     "template": payload.get("template") or "feature", "repos": payload.get("repos") or [],
                     "workflow": payload.get("workflow") or {}}
        ctx = self._ctx_from_task(task_like)
        ctx["repos"] = max(1, 1 + len([r for r in payload.get("repos") or [] if (r or {}).get("repo") and r.get("repo") != repo]))
        roles = (payload.get("workflow") or {}).get("roles") or {}
        ranking = self.rank(ctx, current_roles=roles or None, mode=payload.get("mode"))
        cur_key = outcomes.team_key(roles) if roles else ""
        cur_eval = next((e for e in ranking["candidates"] if e["team_key"] == cur_key), None) if cur_key else None
        r = risk.assess({**ctx, "roles": roles}, self.records(), cur_eval, self.repo_autopsies(ctx["repo"])) if self.settings().get("risk_check", True) else None
        playbook = playbooks.load(ctx["repo"])
        return {"context": {k: ctx[k] for k in ("repo", "repo_label", "template", "size", "repos")},
                "recommendation": ranking, "current": cur_eval, "risk": r,
                "playbook": bool(playbook and any((s.get("text") or "").strip() for s in (playbook.get("sections") or {}).values())),
                "records": len(self.records())}

    def preflight_record(self, task: dict, payload: dict) -> dict:
        """What gets stored on a new task: the forecast, and whether the chosen team is the recommended one."""
        pf = self.preflight({**payload, "repo": task["repo"], "github_repo": task.get("github_repo"), "workflow": task.get("workflow"),
                             "requirements": task.get("requirements"), "template": task.get("template"), "repos": task.get("repos") or []})
        rec = pf["recommendation"]["pick"] or {}
        chosen = outcomes.team_key((task.get("workflow") or {}).get("roles") or {})
        risk_ = pf["risk"] or {}
        return {"time": now(), "records": pf["records"],
                "risk": {k: risk_.get(k) for k in ("level", "p_fail", "base", "base_text", "factors", "mitigations", "questions")} if risk_ else None,
                "recommendation": {"team_key": rec.get("team_key"), "why": rec.get("why"), "mode": pf["recommendation"]["mode"],
                                   "expected_score": rec.get("expected_score")},
                "followed": bool(rec.get("team_key")) and rec.get("team_key") == chosen, "chosen": chosen}

    def auto_pick(self, tid: str) -> dict | None:
        """Autopilot's team choice for a queued task that nobody picked a team for by hand."""
        s = self.settings()
        t = self.m.store.get(tid) or {}
        if not s.get("auto_pick_team") or t.get("team_locked") or t.get("team_pick") or t.get("follow_up_of") or int(t.get("runs") or 1) > 1:
            return None
        ranking = self.rank(self._ctx_from_task(t), current_roles=(t.get("workflow") or {}).get("roles"))
        choice = recommend.auto_pick(ranking, t, float(s.get("explore_rate") or 0), self._rng)
        if not choice.get("team_key"):
            return None
        wf = copy.deepcopy(t.get("workflow") or {})
        before = outcomes.team_key(wf.get("roles") or {})
        roles = outcomes.team_from_key(choice["team_key"])
        wf["roles"] = {r: roles.get(r) or {"agent": "", "model": "", "effort": ""} for r in C.ROLES}
        wf["preset"] = "custom"
        pick = {"source": "explore" if choice["explored"] else "auto", "team_key": choice["team_key"], "previous": before,
                "explored": choice["explored"], "reason": choice["reason"], "mode": ranking["mode"], "time": now()}
        pf = dict(t.get("preflight") or {})
        if pf:
            pf["followed"] = choice["team_key"] == (pf.get("recommendation") or {}).get("team_key")
        self.m.store.update(tid, immediate=True, workflow=wf, team_pick=pick, **({"preflight": pf} if pf else {}))
        self.m.timeline(tid, "system", "Autopilot picked the team" + (" (exploring)" if choice["explored"] else ""),
                        f"{outcomes.team_label(choice['team_key'], C.AGENTS)} · {choice['reason']}")
        return pick

    # ------------------------------------------------------------ playbooks
    def seed_playbook(self, repo: str, repo_path: str = "", quiet: bool = False) -> dict:
        from . import gitops, repo_env, systemmap
        recs = [r for r in self.records() if r.get("repo") == repo]
        if not repo_path:
            repo_path = next((r.get("repo_path") for r in reversed(recs) if r.get("repo_path")), "")
        exists = bool(repo_path) and Path(repo_path).exists()
        env = repo_env.load(repo_path) if exists else {}
        comp = None
        try:
            view = systemmap.view()
            base = systemmap.for_repo(systemmap.load(), repo_path) if exists else systemmap.find(systemmap.load(), repo)
            comp = next((c for c in view["components"] if base and c["id"] == base["id"]), None)
        except Exception:
            comp = None
        detected = []
        if exists:
            try:
                detected = gitops.detect_verify(Path(repo_path))
            except Exception:
                detected = []
        les = [x for x in lessons.approved(repo) if x.get("scope") == "repo"]
        props = [p for p in self.proposals() if p.get("repo") == repo]
        sections = playbooks.seed_sections(scorecard.repo_label(repo), comp, env, detected, les, recs, props)
        pb = playbooks.merge_seed(playbooks.load(repo), repo, scorecard.repo_label(repo), sections, "seed")
        pb["repo_path"] = repo_path or pb.get("repo_path") or ""
        pb["evidence"] = {"runs": len(recs), "successful": sum(1 for r in recs if r.get("success")), "lessons": len(les)}
        return playbooks.save(pb)

    def edit_playbook(self, repo: str, section: str, text: str, accept_suggestion: bool = False) -> dict:
        pb = playbooks.load(repo)
        if not pb:
            raise KeyError("Playbook not found")
        if accept_suggestion:
            text = ((pb["sections"].get(section) or {}).get("suggestion")) or text
        return playbooks.save(playbooks.edit(pb, section, text))

    def refresh_playbook_with_agent(self, repo: str) -> dict:
        from . import retro
        pb = playbooks.load(repo) or self.seed_playbook(repo)
        cfg = self.m.cfg()
        recs = [r for r in self.records() if r.get("repo") == repo]
        tasks = [self.m.store.get(r["task_id"]) for r in recs[-8:]]
        retros = [(t or {}).get("retro") or {} for t in tasks if t and (t.get("retro") or {}).get("status") == "done"]
        roles = cfg.get("roles") or {}
        agent = (cfg.get("retro_agent") or "").strip() or (roles.get("supervisor") or {}).get("agent") or ""
        if agent not in C.AGENTS:
            raise RuntimeError("No agent configured for the playbook refresh (Settings → retrospective agent or the default supervisor).")
        model = (cfg.get("retro_model") or "").strip() or ((cfg.get("subagent_models") or {}).get(agent) or "").strip()
        efforts = C.AGENTS.get(agent, {}).get("efforts") or []
        res = retro.run_agent(agent, model, efforts[0] if efforts else "", playbooks.refresh_prompt(pb, recs, retros), cfg,
                              RUNTIME_DIR / "_playbooks", float(cfg.get("retro_timeout_seconds") or 300))
        sections = playbooks.parse_refresh(res.get("last_message") or res.get("text") or "")
        if not sections:
            raise RuntimeError("The agent did not return a playbook: " + truncate(res.get("error") or res.get("text") or "", 300))
        pb = playbooks.merge_seed(playbooks.load(repo) or pb, repo, pb.get("repo_label"), sections, "agent")
        usage = res.get("usage") or {}
        cost, est = self.m.estimate_cost(agent, usage)
        pb["agent_refresh"] = {"agent": agent, "model": res.get("model") or model, "cost_usd": round(cost, 4), "estimated": est, "time": now(),
                               "runs_seen": len(recs)}
        return playbooks.save(pb)

    def maybe_refresh_playbooks(self):
        s = self.settings()
        recs = self.records()
        repos = {}
        for r in recs:
            if r.get("repo"):
                repos.setdefault(r["repo"], []).append(r)
        hours = max(1.0, float(s.get("playbook_refresh_hours") or 24))
        for repo, rows in repos.items():
            pb = playbooks.load(repo)
            if not pb:
                self.seed_playbook(repo, quiet=True)
                continue
            last = (pb.get("agent_refresh") or {})
            new_runs = len(rows) - int(last.get("runs_seen") or 0)
            due = not last.get("time") or datetime.fromisoformat(last["time"]) < datetime.now() - timedelta(hours=hours)
            if (pb.get("seed_at") or "") < (rows[-1].get("recorded_at") or ""):
                self.seed_playbook(repo, quiet=True)
            if s.get("playbook_agent_refresh", True) and due and new_runs >= 2 and sum(1 for r in rows if r.get("success")) >= 2 and repo not in self._playbook_busy:
                self._playbook_busy.add(repo)
                try:
                    self.refresh_playbook_with_agent(repo)
                except Exception as e:
                    log.warning("playbook refresh for %s: %s", repo, e)
                finally:
                    self._playbook_busy.discard(repo)

    # ------------------------------------------------------------ view
    def view(self) -> dict:
        recs = self.records()
        weeks = 12
        today = datetime.now().date()
        first = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)

        def bucket(rows):
            n = len(rows)
            return {"n": n, "success_rate": round(sum(1 for r in rows if r.get("success")) / n, 3) if n else None,
                    # clean: a success with a score of 60+ and every required criterion proven (outcomes.failed)
                    "clean_rate": round(sum(1 for r in rows if not outcomes.failed(r)) / n, 3) if n else None,
                    "blocked_avg": round(sum(int(r.get("blocked_checks") or 0) for r in rows) / n, 2) if n else None,
                    "avg_score": round(sum(r.get("score") or 0 for r in rows) / n, 1) if n else None,
                    "median_cost": round(sorted(float(r.get("cost_usd") or 0) for r in rows)[n // 2], 2) if n else None,
                    "first_pass": (lambda fp: round(sum(1 for r in fp if r["verification"]["first_ok"]) / len(fp), 2) if fp else None)(
                        [r for r in rows if (r.get("verification") or {}).get("first_ok") is not None])}

        trend = []
        for i in range(weeks):
            ws = first + timedelta(weeks=i)
            rows = [r for r in recs if (r.get("finished_at") or "")[:10] >= ws.isoformat() and (r.get("finished_at") or "")[:10] < (ws + timedelta(days=7)).isoformat()]
            trend.append({"week": ws.isoformat(), **bucket(rows)})

        def group(keyf, labelf):
            g = {}
            for r in recs:
                k = keyf(r)
                if k:
                    g.setdefault(k, []).append(r)
            out = [{"key": k, "label": labelf(k, rows), **bucket(rows)} for k, rows in g.items()]
            return sorted(out, key=lambda x: (-x["n"], -(x["avg_score"] or 0)))

        props = self.proposals()
        for p in props:
            if p.get("status") == "applied" and p.get("repo"):
                before = [r for r in recs if r.get("repo") == p["repo"] and (r.get("finished_at") or "") < (p.get("applied_at") or "")]
                after = [r for r in recs if r.get("repo") == p["repo"] and (r.get("started_at") or "") >= (p.get("applied_at") or "")]
                p["impact"] = {"before": bucket(before[-10:]), "after": bucket(after)}
        pbs = []
        for pb in playbooks.all_playbooks():
            pbs.append({**pb, "labels": playbooks.SECTION_LABEL})
        recent = []
        for t in sorted(self.m.store.list(), key=lambda t: t.get("created_at") or "", reverse=True)[:40]:
            pf = t.get("preflight") or {}
            if not pf:
                continue
            card = t.get("scorecard") or {}
            recent.append({"task_id": t["id"], "name": t.get("name"), "status": t.get("status"), "created_at": t.get("created_at"),
                           "risk": (pf.get("risk") or {}).get("level"), "p_fail": (pf.get("risk") or {}).get("p_fail"),
                           "recommended": (pf.get("recommendation") or {}).get("team_key"), "chosen": pf.get("chosen"), "followed": pf.get("followed"),
                           "team_pick": t.get("team_pick"), "score": card.get("score") if card.get("finished_at") == t.get("finished_at") else None,
                           "success": card.get("success") if card.get("finished_at") == t.get("finished_at") else None})
        return {
            "records": len(recs), "settings": self.settings(), "agents": {k: {"label": v["label"]} for k, v in C.AGENTS.items()},
            "overall": bucket(recs), "trend": trend,
            "by_repo": group(lambda r: r.get("repo"), lambda k, rows: rows[-1].get("repo_label") or k)[:12],
            "by_team": group(lambda r: r.get("team_key"), lambda k, rows: outcomes.team_label(k, C.AGENTS))[:12],
            "by_type": group(lambda r: r.get("template"), lambda k, rows: next((x["name"] for x in C.TASK_TEMPLATES if x["id"] == k), k)),
            "causes": group(lambda r: r.get("autopsy_cause"), lambda k, rows: autopsy.CAUSE_LABEL.get(k, k)),
            "lessons": self.lessons_report(),
            "playbooks": pbs,
            "calibration": risk.calibration(recs),
            "recent_predictions": recent,
            "proposals": sorted(sorted(props, key=lambda p: p.get("created_at") or "", reverse=True), key=lambda p: p.get("status") != "proposed")[:120],
            "open_proposals": sum(1 for p in props if p.get("status") == "proposed"),
        }
