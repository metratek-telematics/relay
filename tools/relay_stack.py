#!/usr/bin/env python3
"""relay-stack: run a task's integration stack (orchestrator/stacks.py) from a shell.

    relay-stack up [--service NAME]     build changed services from the task's worktrees and start them
    relay-stack down                    stop and remove the stack (logs are saved to the run folder)
    relay-stack status [--json]         services, health and URLs
    relay-stack logs SERVICE [--tail N] recent log lines of one service
    relay-stack url SERVICE [--host]    the service URL (reachable from here; --host for the 127.0.0.1 port)
    relay-stack restart SERVICE         rebuild and recreate one service
    relay-stack check                   run the stack's end-to-end checks

The task comes from RELAY_TASK_ID (set for agents and commands Relay runs) or --task.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import config as C, stacks  # noqa: E402
from orchestrator.util import TASKS_FILE, read_json  # noqa: E402


def find_task(tid: str) -> dict:
    for t in read_json(TASKS_FILE, []) or []:
        if t.get("id") == tid:
            return t
    raise SystemExit(f"relay-stack: task {tid!r} not found")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="relay-stack", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", default=os.environ.get("RELAY_TASK_ID", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("up"); p.add_argument("--service", action="append")
    sub.add_parser("down")
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("logs"); p.add_argument("service"); p.add_argument("--tail", type=int, default=200)
    p = sub.add_parser("url"); p.add_argument("service"); p.add_argument("--host", action="store_true")
    p = sub.add_parser("restart"); p.add_argument("service")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    if not a.task:
        ap.error("no task: run inside a Relay task (RELAY_TASK_ID) or pass --task")
    task = find_task(a.task)
    if a.cmd not in ("down", "status", "logs", "url") and not task.get("worktree"):
        print("relay-stack: note: the task has no worktree yet; repositories are built from their checkout", file=sys.stderr)
    cfg = C.load()
    try:
        if a.cmd == "up":
            st = stacks.up(task, cfg, log=print, only=a.service, owner="cli")
            return 0 if st.get("status") == "up" else 1
        if a.cmd == "down":
            stacks.down(task["id"], log=print, reason="relay-stack down")
            return 0
        if a.cmd == "status":
            st = stacks.status(task["id"])
            if a.json:
                print(json.dumps(st, indent=2))
                return 0
            if not st:
                d = stacks.def_for_task(task)
                print(f"No stack running. Definition: {d['name'] if d else 'none'}")
                return 0
            print(f"{st.get('stack_name')} · {st.get('project')} · {st.get('status')}" + (f" · {st['error'].splitlines()[0]}" if st.get("error") else ""))
            for name, s in (st.get("services") or {}).items():
                print(f"  {name:<16} {s.get('status', ''):<10} {s.get('url') or '-':<28} {s.get('source_kind', '')}")
            return 0
        if a.cmd == "logs":
            print(stacks.logs(task["id"], a.service, a.tail))
            return 0
        if a.cmd == "url":
            s = (stacks.load_state(task["id"]).get("services") or {}).get(a.service)
            if not s or not s.get("url"):
                print(f"relay-stack: {a.service} has no URL (is the stack up? does it expose a port?)", file=sys.stderr)
                return 1
            print(s["host_url"] if a.host else s["url"])
            return 0
        if a.cmd == "restart":
            stacks.restart(task, cfg, a.service, log=print)
            return 0
        if a.cmd == "check":
            d = stacks.def_for_task(task)
            if not d:
                raise stacks.StackError("This task has no integration stack")
            if stacks.load_state(task["id"]).get("status") != "up":
                stacks.up(task, cfg, log=print, owner="cli")
            items = stacks.run_checks(task, d, cfg, log=lambda s: None)
            for i in items:
                print(f"{'PASS' if i['passed'] else 'FAIL'} {i['name']}{'' if i['required'] else ' (optional)'} ({i['duration']}s)")
                if not i["passed"]:
                    print("  " + i["output"].replace("\n", "\n  "))
                    for svc, text in (i.get("logs") or {}).items():
                        print(f"  --- {svc} ---\n  " + text.strip().replace("\n", "\n  "))
            return 0 if all(i["passed"] or not i["required"] for i in items) else 1
    except stacks.StackError as e:
        print(f"relay-stack: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
