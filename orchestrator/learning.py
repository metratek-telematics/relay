"""Measure every finished task and learn from it: scorecards, retrospectives and lessons.

The manager calls `task_ended` once a run has recorded its outcome. Everything
after that happens on background threads, so a slow agent or an unreachable
GitHub never delays a task's completion:

    task_ended ─┬─ scorecard (orchestrator/scorecard.py), stored on the task and in DATA_DIR/state/scorecards.json
                └─ retrospective queue ─► one cheap agent turn (orchestrator/retro.py) ─► lessons review queue

`start` backfills scorecards for finished tasks that have none (no agent calls)
and refreshes the pull request state of recent deliveries every 30 minutes, so a
merge or a rejection a week later still reaches the score.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta

from . import github, lessons, retro, scorecard
from .learning_engine import LearningEngine
from .util import RUNTIME_DIR

log = logging.getLogger("relay.learning")


class Learning:
    def __init__(self, manager):
        self.m = manager
        self.cards = scorecard.ScorecardStore()
        self._retros: queue.Queue = queue.Queue()
        self._retro_thread = None
        self._started = False
        self._lock = threading.Lock()
        self.engine = LearningEngine(manager)  # outcomes, recommendations, risk, autopsies, lesson effects, playbooks

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._background, name="relay-learning", daemon=True).start()
        try:
            from . import knowledge
            knowledge.start_daily(self.m)   # daily staleness check and refresh of the repository knowledge docs
        except Exception:
            log.exception("knowledge refresh loop did not start")

    def task_ended(self, tid: str, retro_turn: bool = True):
        """Record the scorecard of a run that just ended and queue its retrospective. Never raises."""
        try:
            t = self.m.store.get(tid)
            # A task stopped before it ever started has nothing to measure.
            if not t or self.m.store.is_deleted(tid) or t.get("status") not in scorecard.FINISHED or not t.get("started_at"):
                return
            self.score(tid)
            if retro_turn and self.m.cfg().get("retro_enabled", True):
                self.queue_retro(tid)
        except Exception:
            log.exception("scorecard for %s failed", tid)

    def queue_retro(self, tid: str):
        self.m.set_meta(tid, retro={"status": "queued"})
        self._retros.put(tid)
        with self._lock:
            if not self._retro_thread or not self._retro_thread.is_alive():
                self._retro_thread = threading.Thread(target=self._retro_loop, name="relay-retro", daemon=True)
                self._retro_thread.start()

    def _retro_loop(self):
        # One at a time: several tasks finishing together must not start a burst of agent processes.
        while True:
            try:
                tid = self._retros.get(timeout=60)
            except queue.Empty:
                return
            try:
                retro.run(self.m, tid)
            except Exception:
                log.exception("retrospective for %s failed", tid)
            try:
                self.engine.after_retro(tid)
            except Exception:
                log.exception("learning after the retrospective of %s failed", tid)

    # ------------------------------------------------------------ scorecards
    def score(self, tid: str, messages=None, pr=None) -> dict | None:
        t = self.m.store.get(tid)
        if not t or t.get("status") not in scorecard.FINISHED or not t.get("started_at"):
            return None
        prev = self.cards.get(tid) or t.get("scorecard") or {}
        if messages is None:
            messages = self.m.store.messages(tid) if tid in self.m.runners or tid in getattr(self.m.store, "_messages", {}) \
                else scorecard.read_messages(RUNTIME_DIR / tid / "messages.jsonl")
        same_run = prev.get("finished_at") == t.get("finished_at")
        card = scorecard.compute(t, messages, pr=pr if pr is not None else ((prev.get("pr") or None) if same_run else None))
        self.cards.put(card)
        self.m.store.update(tid, touch=False, scorecard=card)
        self.engine.record(tid, card)  # the outcome dataset, and an autopsy when the run failed or scored low
        self.m.emit_task(tid)
        return card

    def backfill(self) -> int:
        n = 0
        for t in self.m.store.list():
            if t.get("status") not in scorecard.FINISHED or not t.get("started_at"):
                continue
            have = self.cards.get(t["id"])
            if have and have.get("version") == scorecard.VERSION and have.get("finished_at") == t.get("finished_at"):
                if not t.get("scorecard"):
                    self.m.store.update(t["id"], touch=False, scorecard=have)
                continue
            try:
                self.score(t["id"])
                n += 1
            except Exception:
                log.exception("backfilling the scorecard of %s failed", t["id"])
        return n

    def refresh_prs(self, force_tid: str | None = None) -> int:
        """Ask GitHub about delivered pull requests that are still open (or all recent ones for one task)."""
        cfg = self.m.cfg()
        days = int(cfg.get("scorecard_refresh_days") or 14)
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        n = 0
        for t in self.m.store.list():
            if force_tid and t["id"] != force_tid:
                continue
            card = self.cards.get(t["id"])
            if not card or not card.get("pr") or card.get("finished_at") != t.get("finished_at"):
                continue
            if not force_tid and card["pr"].get("state") == "merged" and self._revert_due(card, cfg):
                n += self.check_revert(t["id"], card, cfg)
                continue
            if not force_tid and ((card.get("finished_at") or "") < cutoff or card["pr"].get("state") in ("merged", "closed")):
                continue
            pr = scorecard.fetch_pr(card, cfg, github.gh_json)
            if pr != card.get("pr"):
                card = self.score(t["id"], pr=pr) or card
                n += 1
            if pr.get("state") == "merged":
                n += self.check_revert(t["id"], card, cfg)
        return n

    def _revert_due(self, card: dict, cfg: dict) -> bool:
        pr = card.get("pr") or {}
        days = int(((cfg.get("learning") or {}).get("revert_window_days")) or 14)
        if pr.get("reverted") or not pr.get("merged_at"):
            return False
        try:
            merged = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return False
        if datetime.utcnow() - merged > timedelta(days=days + 1):
            return False
        last = pr.get("revert_checked_at") or ""
        return not last or last < (datetime.now() - timedelta(hours=6)).isoformat(timespec="seconds")

    def check_revert(self, tid: str, card: dict, cfg: dict) -> int:
        """A merged pull request reverted on its base branch within the window loses its success."""
        days = int(((cfg.get("learning") or {}).get("revert_window_days")) or 14)
        try:
            hit = scorecard.fetch_revert(card, github.gh_json, days)
        except Exception as e:
            log.info("revert check for %s: %s", tid, e)
            return 0
        if hit is None:
            return 0
        pr = {**(card.get("pr") or {}), "revert_checked_at": datetime.now().isoformat(timespec="seconds")}
        if hit:
            pr.update(reverted=True, revert_sha=hit.get("sha"), revert_subject=hit.get("subject"))
            self.m.timeline(tid, "github", "Pull request reverted", hit.get("subject") or "")
        self.score(tid, pr=pr)
        return 1 if hit else 0

    def _background(self):
        try:
            done = self.backfill()
            if done:
                log.info("backfilled %d scorecard(s)", done)
        except Exception:
            log.exception("scorecard backfill failed")
        try:
            done = self.engine.backfill()
            if done:
                log.info("backfilled %d outcome record(s)", done)
        except Exception:
            log.exception("outcome backfill failed")
        while True:
            try:
                self.refresh_prs()
            except Exception:
                log.exception("pull request refresh failed")
            try:
                self.engine.maybe_refresh_playbooks()
            except Exception:
                log.exception("playbook refresh failed")
            minutes = max(5, int(self.m.cfg().get("scorecard_refresh_minutes") or 30))
            time.sleep(minutes * 60)

    # ------------------------------------------------------------ views
    def summary(self) -> dict:
        cards = [c for c in self.cards.all() if not self.m.store.is_deleted(c.get("task_id"))]
        out = scorecard.summarize(cards)
        out["lessons_pending"] = lessons.pending_count()
        out["lessons_approved"] = len(lessons.approved())
        return out
