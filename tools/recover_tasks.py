"""Rebuild task records in state/tasks.json from the runtime/ folders.

Task metadata lives in state/tasks.json, but the full history of every run
(messages, events, artifacts, raw log) lives under runtime/<task-id>/. If the
state file is lost or truncated, this reconstructs the missing entries from
that history so the task reappears in the UI with its conversation intact.

Usage:  python tools/recover_tasks.py [--apply]
Without --apply it only reports what it would restore.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.util import DELETED_FILE, RUNTIME_DIR, TASKS_FILE, read_json, write_json  # noqa: E402


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def rebuild(task_dir: Path) -> dict | None:
    tid = task_dir.name
    if not re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", tid):
        return None
    msgs = read_jsonl(task_dir / "messages.jsonl")
    events = [e for e in read_jsonl(task_dir / "events.jsonl") if not e.get("_patch")]
    if not msgs and not events:
        return None

    raw = ""
    rp = task_dir / "raw.log"
    if rp.exists():
        raw = rp.read_text(encoding="utf-8", errors="replace")[:20000]

    def find(pattern, default=None):
        m = re.search(pattern, raw)
        return m.group(1).strip() if m else default

    worktree = find(r"REPOSITORY:\s*(.+)")
    branch = find(r"BRANCH:\s*(\S+)")

    # The orchestrator's opening briefing carries the original request.
    briefing = next((m for m in msgs if m.get("kind") == "handoff" and m.get("subtype") == "briefing"), None)
    requirements = (briefing or {}).get("content") or ""
    name = (requirements.strip().splitlines() or ["recovered task"])[0][:60]

    # Infer the team from who actually spoke.
    roles = {"supervisor": {"agent": "", "model": "", "effort": ""},
             "worker": {"agent": "", "model": "", "effort": ""},
             "reviewer": {"agent": "", "model": "", "effort": ""}}
    for m in msgs:
        r, a = m.get("role"), m.get("agent")
        if r in roles and a and not roles[r]["agent"]:
            roles[r]["agent"] = a
    # A role that never spoke before the run stopped is still required; use the default team.
    defaults = (read_json(ROOT / "config.json", {}) or {}).get("roles") or {}
    for r in ("supervisor", "worker"):
        if not roles[r]["agent"]:
            roles[r]["agent"] = (defaults.get(r) or {}).get("agent") or ("codex" if r == "supervisor" else "claude")

    # Source repo: a linked worktree's .git file points back at the real repository,
    # e.g. "gitdir: C:/proj/.git/worktrees/src-968587".
    repo = ""
    if worktree:
        gitfile = Path(worktree) / ".git"
        try:
            if gitfile.is_file():
                m = re.search(r"gitdir:\s*(.+)", gitfile.read_text(encoding="utf-8", errors="replace"))
                if m:
                    gitdir = Path(m.group(1).strip())
                    # .../<repo>/.git/worktrees/<name>  ->  <repo>
                    for parent in gitdir.parents:
                        if parent.name == ".git":
                            repo = str(parent.parent)
                            break
        except Exception:
            pass

    created = (msgs[0].get("time") if msgs else None) or (events[0].get("time") if events else None)
    updated = (msgs[-1].get("time") if msgs else None) or (events[-1].get("time") if events else None)

    artifacts = {}
    for kind, fname in (("plan", "PLAN.md"), ("acceptance", "ACCEPTANCE.md"), ("implementation", "IMPLEMENTATION.md"),
                        ("verification", "VERIFICATION.md"), ("review", "REVIEW.md"), ("report", "REPORT.md")):
        if (task_dir / fname).exists():
            artifacts[kind] = str(task_dir / fname)

    plan = None
    if (task_dir / "PLAN.md").exists():
        acc = []
        if (task_dir / "ACCEPTANCE.md").exists():
            acc = [l.lstrip("- [ ]x").strip() for l in (task_dir / "ACCEPTANCE.md").read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        plan = {"summary": "", "plan": (task_dir / "PLAN.md").read_text(encoding="utf-8", errors="replace"), "acceptance": acc}

    # Highest work-package number seen tells us roughly where the run got to.
    turn = max([int(m.get("turn") or 0) for m in msgs] or [0])

    return {
        "id": tid,
        "name": name or "recovered task",
        "repo": repo or str(worktree or ""),
        "requirements": requirements,
        "issue": "",
        "template": "feature",
        "priority": "normal",
        "tags": ["recovered"],
        "attachments": [],
        "workflow": {"preset": "custom", "roles": roles, "max_turns": 12, "max_review_rounds": 3,
                     "verify_mode": "each_report", "approval_before_delivery": False,
                     "allow_agent_questions": True, "verification_commands": [], "auto_detect_verification": True},
        "status": "interrupted",
        "detail": "Recovered from runtime history. Resume to continue, or Retry fresh.",
        "created_at": created, "updated_at": updated, "started_at": created, "finished_at": None,
        "events": events[-400:], "artifacts": artifacts, "sessions": {}, "metrics": {}, "guidance": [],
        "checkpoint": {"phase": "dialogue", "turn": max(1, turn), "awaiting": "worker", "plan": plan} if plan else None,
        "pending": None, "archived": False,
        "worktree": str(worktree) if worktree else None,
        "branch": branch,
        "run_dir": str(task_dir),
        "recovered": True,
    }


def main():
    apply = "--apply" in sys.argv
    existing = read_json(TASKS_FILE, []) or []
    known = {t.get("id") for t in existing}
    known |= set(read_json(DELETED_FILE, []) or [])  # never bring back a task the user deleted
    restored = []
    for d in sorted(RUNTIME_DIR.iterdir()):
        if not d.is_dir() or d.name in known or d.name.startswith("_"):
            continue
        rec = rebuild(d)
        if rec:
            restored.append(rec)

    if not restored:
        print("Nothing to recover — every runtime folder already has a task entry.")
        return

    for r in restored:
        print(f"  {r['id']}  {r['name'][:60]}")
        print(f"      branch={r['branch']}  messages/events recovered, worktree={'yes' if r['worktree'] else 'no'}")

    if not apply:
        print(f"\n{len(restored)} task(s) can be restored. Re-run with --apply to write them.")
        return

    write_json(TASKS_FILE, restored + existing)
    print(f"\nRestored {len(restored)} task(s) into {TASKS_FILE}.")


if __name__ == "__main__":
    main()
