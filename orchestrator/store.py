"""Persistent task store.

tasks.json holds task metadata only. Per-task conversation messages and
timeline events are append-only JSONL files under runtime/<task-id>/ so a
busy stream never rewrites the whole state file.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from .util import DELETED_FILE, RUNTIME_DIR, TASKS_FILE, append_line, now, read_json, write_json

ACTIVE = {"running", "preparing", "planning", "implementing", "verifying", "reviewing", "delivering"}
WAITING = {"needs_input", "paused"}
TERMINAL = {"done", "failed", "stopped", "interrupted"}

MAX_MEM_MESSAGES = 6000
MAX_EVENTS = 400


class TaskStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks: list[dict] = []
        self._messages: dict[str, list] = {}
        self._seq: dict[str, int] = {}
        self._save_timer = None
        self._dirty = False
        self._deleted: set[str] = self._load_tombstones()
        self._load()

    # ------------------------------------------------------------------ tombstones
    # Deleted ids are recorded so the merge in save_now never resurrects them
    # from the copy still sitting in tasks.json.
    def _load_tombstones(self) -> set[str]:
        rows = read_json(DELETED_FILE, []) or []
        return {str(x) for x in rows if x}

    def _save_tombstones(self):
        try:
            write_json(DELETED_FILE, sorted(self._deleted)[-5000:])
        except Exception:
            pass

    # ------------------------------------------------------------------ tasks
    def _load(self):
        rows = [t for t in (read_json(TASKS_FILE, []) or []) if t.get("id") not in self._deleted]
        for t in rows:
            if t.get("status") in ACTIVE or t.get("status") in WAITING:
                t["status"] = "interrupted"
                t["detail"] = "Orchestrator restarted during execution. Resume to continue from the checkpoint."
                t["process"] = {"state": "idle"}
            t.setdefault("events", [])
            t.setdefault("metrics", {})
            t.setdefault("sessions", {})
            t.setdefault("artifacts", {})
            t.pop("streams", None)  # v13 field
        self.tasks = rows

    def save_now(self):
        """Persist tasks, merging in any rows written by another process.

        The file is the shared source of truth. Writing our in-memory list blindly
        would drop tasks created by a second instance (or by a newer instance while
        this one was shutting down), so unknown ids are always preserved.
        """
        with self.lock:
            self._dirty = False
            rows = list(self.tasks)
            try:
                on_disk = read_json(TASKS_FILE, []) or []
                self._deleted |= self._load_tombstones()  # honour deletes made by another instance
                known = {t.get("id") for t in rows}
                foreign = [t for t in on_disk
                           if t.get("id") and t["id"] not in known and t["id"] not in self._deleted]
                if foreign:
                    rows = rows + foreign
                    self.tasks = rows
            except Exception:
                pass
            self._backup_once(rows)
            write_json(TASKS_FILE, rows)

    def _backup_once(self, rows):
        """Keep the last few good task files so a bad write is never fatal."""
        try:
            if not rows or not TASKS_FILE.exists():
                return
            now_min = int(time.time() // 300)  # at most one backup per 5 minutes
            if getattr(self, "_last_backup", None) == now_min:
                return
            self._last_backup = now_min
            bdir = TASKS_FILE.parent / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(TASKS_FILE, bdir / f"tasks-{now_min}.json")
            old = sorted(bdir.glob("tasks-*.json"))[:-12]
            for p in old:
                p.unlink(missing_ok=True)
        except Exception:
            pass

    def save_soon(self, delay=0.3):
        with self.lock:
            self._dirty = True
            if self._save_timer:
                return
            self._save_timer = threading.Timer(delay, self._flush)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _flush(self):
        with self.lock:
            self._save_timer = None
            if self._dirty:
                self.save_now()

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(t) for t in self.tasks]

    def get(self, tid: str):
        with self.lock:
            for t in self.tasks:
                if t["id"] == tid:
                    return json.loads(json.dumps(t))
        return None

    def add(self, t: dict):
        with self.lock:
            self.tasks.insert(0, t)
            self.save_now()

    def update(self, tid: str, immediate=False, touch=True, **kw):
        # touch=False records derived data (scorecards) without making the task look recently active.
        with self.lock:
            for t in self.tasks:
                if t["id"] == tid:
                    t.update(kw)
                    if touch:
                        t["updated_at"] = now()
                    break
            if immediate:
                self.save_now()
            else:
                self.save_soon()

    def patch_dict(self, tid: str, field: str, **kw):
        with self.lock:
            for t in self.tasks:
                if t["id"] == tid:
                    d = dict(t.get(field) or {})
                    d.update(kw)
                    t[field] = d
                    t["updated_at"] = now()
                    break
            self.save_soon()

    def is_deleted(self, tid: str) -> bool:
        return tid in self._deleted

    def remove(self, tid: str):
        with self.lock:
            self._deleted.add(tid)
            self._save_tombstones()
            self.tasks = [t for t in self.tasks if t["id"] != tid]
            self._messages.pop(tid, None)
            self._seq.pop(tid, None)
            self.save_now()

    # -------------------------------------------------------------- timeline
    def add_event(self, tid: str, ev: dict):
        if tid in self._deleted:
            return
        with self.lock:
            for t in self.tasks:
                if t["id"] == tid:
                    evs = list(t.get("events") or [])
                    evs.append(ev)
                    t["events"] = evs[-MAX_EVENTS:]
                    t["updated_at"] = now()
                    break
            self.save_soon()
        append_line(self.task_dir(tid) / "events.jsonl", json.dumps(ev, ensure_ascii=False))

    # -------------------------------------------------------------- messages
    def task_dir(self, tid: str) -> Path:
        p = RUNTIME_DIR / tid
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _messages_file(self, tid):
        return self.task_dir(tid) / "messages.jsonl"

    def _ensure_loaded(self, tid: str):
        if tid in self._messages:
            return
        rows = []
        f = self._messages_file(tid)
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        # apply patches (message_update rows) onto originals
        by_id = {}
        final = []
        for r in rows:
            if r.get("_patch"):
                tgt = by_id.get(r.get("id"))
                if tgt is not None:
                    tgt.update({k: v for k, v in r.items() if k != "_patch"})
                continue
            by_id[r.get("id")] = r
            final.append(r)
        self._messages[tid] = final[-MAX_MEM_MESSAGES:]
        self._seq[tid] = (final[-1]["seq"] if final else 0)

    def messages(self, tid: str, after: int = 0, limit: int = 0) -> list[dict]:
        with self.lock:
            self._ensure_loaded(tid)
            rows = [m for m in self._messages[tid] if m.get("seq", 0) > after]
            if limit and len(rows) > limit:
                rows = rows[-limit:]
            return json.loads(json.dumps(rows))

    def message_count(self, tid: str) -> int:
        with self.lock:
            self._ensure_loaded(tid)
            return self._seq.get(tid, 0)

    def append_message(self, tid: str, msg: dict) -> dict:
        if tid in self._deleted:
            return msg
        with self.lock:
            self._ensure_loaded(tid)
            self._seq[tid] = self._seq.get(tid, 0) + 1
            msg["seq"] = self._seq[tid]
            msg.setdefault("ts", time.time())
            msg.setdefault("time", now())
            self._messages[tid].append(msg)
            if len(self._messages[tid]) > MAX_MEM_MESSAGES:
                self._messages[tid] = self._messages[tid][-MAX_MEM_MESSAGES:]
        append_line(self._messages_file(tid), json.dumps(msg, ensure_ascii=False))
        return msg

    def update_message(self, tid: str, mid: str, patch: dict):
        if tid in self._deleted:
            return
        with self.lock:
            self._ensure_loaded(tid)
            for m in reversed(self._messages[tid]):
                if m.get("id") == mid:
                    m.update(patch)
                    break
        row = {"_patch": True, "id": mid, **patch}
        append_line(self._messages_file(tid), json.dumps(row, ensure_ascii=False))

    def find_message(self, tid: str, mid: str):
        with self.lock:
            self._ensure_loaded(tid)
            for m in reversed(self._messages[tid]):
                if m.get("id") == mid:
                    return dict(m)
        return None
