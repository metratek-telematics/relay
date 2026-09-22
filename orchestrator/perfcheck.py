"""Performance tasks are judged by measurements, not by screenshots.

When a task is about performance (slow, lag, smooth, fps, jank, cpu, memory, optimise ...), the plan carries a
`perf` spec: what to load, how to drive it, and numeric targets. Relay then

1. measures the starting commit with relay-perf (tools/perf.cjs) right after the plan: the baseline;
2. re-measures the worktree during verification, compares with the baseline and checks the targets;
3. fails verification when there is no measurement, a target is missed, or a key metric regressed, so the judge
   (judge.done_gate) cannot accept the work without numbers.

Results go to the task meta (`perf`) for the task page's performance card, and the files stay in the run folder
(run_dir/perf/<label>/: PERF_REPORT.md, perf.json, trace-*.json.gz, *.cpuprofile).
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .util import now, quiet, truncate, write_text

APP_DIR = Path(__file__).resolve().parent.parent

# Words that make a task a performance task. Word starts, so "lag" does not match "flag".
_PERF_RE = re.compile(
    r"\b(performance|perf\b|slow|sluggish|lag|laggy|lagging|smooth|fps|frame ?rate|frames per second|jank|stutter|"
    r"freez|hangs?\b|cpu\b|memory (leak|usage)|optimi[sz]|long tasks?|total blocking time|tbt\b|lighthouse|"
    r"core web vitals|lcp\b|inp\b|too many cores)", re.I)

DEFAULT_TARGETS = ["4x.fps_p5>=30", "4x.long_task_max_ms<=100", "4x.tbt_ms<=-50%"]


def is_perf_task(task: dict) -> bool:
    wf = task.get("workflow") or {}
    if isinstance(wf.get("perf"), bool):
        return wf["perf"]
    text = f"{task.get('name') or ''}\n{task.get('requirements') or ''}"
    return bool(_PERF_RE.search(text))


def is_web_repo(wt) -> bool:
    """relay-perf measures pages in a browser: only repositories that serve web pages qualify."""
    if not wt or not Path(wt).is_dir():
        return False
    root = Path(wt)
    if (root / "package.json").exists() or (root / "index.html").exists():
        return True
    return any(True for _ in root.glob("*/*.html")) or any(True for _ in root.glob("*/*/*.html"))


def applies(p) -> bool:
    """A performance task on a web repository: measured, and judged by the measurement."""
    return getattr(p, "verify_mode", "") != "off" and is_perf_task(p.task) and is_web_repo(getattr(p, "wt", None))


def tool_command() -> str:
    exe = shutil.which("relay-perf")
    return exe if exe else f"node {APP_DIR / 'tools' / 'perf.cjs'}"


def normalize_spec(raw) -> dict | None:
    """{url, serve, steps, throttle, targets, lighthouse, settle} or None when there is nothing to load."""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or raw.get("path") or "").strip()
    if not url:
        return None
    steps = raw.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        steps = None
    throttle = ",".join(str(x).lower().rstrip("x") for x in raw.get("throttle")) if isinstance(raw.get("throttle"), list) \
        else str(raw.get("throttle") or "1,4").lower().replace("x", "")
    if not re.fullmatch(r"\d+(\.\d+)?(,\d+(\.\d+)?)*", throttle):
        throttle = "1,4"
    targets = [str(t).strip() for t in (raw.get("targets") or []) if str(t).strip()]
    return {"url": url, "serve": str(raw.get("serve") or "").strip(), "steps": steps, "throttle": throttle,
            "targets": targets[:10], "lighthouse": bool(raw.get("lighthouse")),
            "settle": int(raw.get("settle") or 3000)}


def kickoff_note(p) -> str:
    """Added to the supervisor's kickoff prompt for performance tasks."""
    if not applies(p):
        return ""
    return (
        "PERFORMANCE TASK — measure first, then change, then prove it with numbers (rules/PERFORMANCE_RELIABILITY.md).\n"
        "Your plan envelope MUST include a `perf` object; Relay measures the starting commit with it right after the plan "
        "(the baseline) and re-measures the worktree at verification:\n"
        '"perf": {"url": "/path?query (or a full http URL)", "serve": "command that serves the app on {port}, e.g. '
        "npx vite --host 127.0.0.1 --port {port} --strictPort (omit when the URL is already running)\", "
        '"steps": [relay-perf steps: {"wait":3000}, {"phase":"pan"}, {"drag":{"from":[1000,450],"to":[400,450],"steps":40,"duration":1000}}, '
        '{"phase":"zoom"}, {"wheel":{"x":720,"y":450,"deltaY":-240,"repeat":4,"interval":600}}, {"phase":null}] or null for the default map scenario, '
        '"throttle": "1,4", "targets": ["4x.pan.fps_p5>=45", "4x.long_task_max_ms<=100", "4x.tbt_ms<=-50%", "4x.heap_growth_mb<=5"]}\n'
        "Targets are `profile[.phase].metric OP number`, or `OP -N%` relative to the baseline. Metrics: fps_avg, fps_p5, jank_pct, "
        "long_tasks, long_task_max_ms, tbt_ms, main_busy_pct, scripting_ms, gc_ms, forced_layouts, inp_ms, heap_growth_mb, dom_nodes, "
        "requests, ws_msgs_per_s, load_tbt_ms, lcp_ms. Pick targets that reflect the complaint (e.g. smooth panning on an older device = "
        "4x throttle p5 FPS and max long task during the pan phase).\n"
        "Acceptance criteria for this work must be numeric and reference these targets "
        "(how_to_verify: \"command: relay-perf assert <run>/perf.json '4x.pan.fps_p5>=45' --baseline <baseline>/perf.json\"). "
        "The first work package is to profile: read the baseline PERF_REPORT.md Relay writes, name the dominant cost with evidence "
        "(function, file:line, % of main thread), and fix that first. If the URL, serve command or scenario turns out wrong, send the "
        "corrected `perf` object in any later envelope; Relay re-measures the starting commit with it."
    )


def env_line(run_dir) -> str:
    """How agents run the tool themselves (pipeline env_text)."""
    return ("- Performance: measure, do not guess. `relay-perf run <url|/path> [steps.json] --out "
            f"{Path(run_dir) / 'perf' / 'mine'} --throttle 1,4 [--serve \"npx vite --host 127.0.0.1 --port {{port}} --strictPort\"]` "
            "records a Chrome trace + CPU profile while it drives the page (steps like relay-browse plus "
            '{"drag":{"from":[x,y],"to":[x,y]}}, {"wheel":{"x":..,"y":..,"deltaY":-240}}, {"phase":"pan"}) at normal and 4x-throttled CPU, '
            "and writes PERF_REPORT.md: FPS avg/p5, long tasks and TBT, the top 15 functions with file:line, scripting/rendering/GC time, "
            "forced layouts, heap growth, every thread's CPU, network/polling/websocket rates, Lighthouse. "
            "`relay-perf compare before/perf.json after/perf.json` gives deltas and a verdict; "
            "`relay-perf assert after/perf.json '4x.fps_p5>=45' --baseline before/perf.json '4x.tbt_ms<=-50%'` checks targets. "
            "Paste numbers, not screenshots, as evidence.")


# ----------------------------------------------------------------------------- measuring
def _state(p) -> dict:
    return p.state.setdefault("perf", {})


def _measure(p, wt, label: str, spec: dict) -> dict:
    out = Path(p.run_dir) / "perf" / label
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    steps_arg = ""
    if spec.get("steps"):
        steps_file = Path(p.run_dir) / "perf" / "steps.json"
        write_text(steps_file, json.dumps(spec["steps"], indent=1))
        steps_arg = shlex.quote(str(steps_file))
    cmd = (f"{tool_command()} run {shlex.quote(spec['url'])} {steps_arg} --out {shlex.quote(str(out))} "
           f"--throttle {spec['throttle']} --settle {int(spec.get('settle') or 3000)} --label {shlex.quote(label)}"
           + ("" if spec.get("lighthouse") else " --no-lighthouse")
           + (f" --serve {shlex.quote(spec['serve'])}" if spec.get("serve") else ""))
    timeout = float(p.cfg.get("perf_timeout_minutes") or 15) * 60
    res = p.r.run_shell(cmd, wt, "verify", timeout=timeout, title=f"Performance · {label}")
    perf_json = out / "perf.json"
    if not perf_json.exists():
        tail = (res.get("output") or "").strip().splitlines()[-8:]
        return {"ok": False, "label": label, "dir": str(out), "error": "relay-perf produced no report: " + " | ".join(tail)[:600],
                "time": now()}
    data = json.loads(perf_json.read_text(encoding="utf-8"))
    problems = [pr.get("navigation_error") for pr in data.get("profiles") or [] if pr.get("navigation_error")]
    steps_ok = all(pr.get("steps_ok") for pr in data.get("profiles") or [])
    return {"ok": not problems and steps_ok, "label": label, "dir": str(out), "summary": data.get("summary") or {},
            "findings": (data.get("findings") or [])[:8], "time": now(),
            "error": ("navigation failed: " + problems[0]) if problems else ("" if steps_ok else "a scenario step failed (see PERF_REPORT.md)"),
            "top": [f"{f.get('self_pct')}% {f.get('function')} {f.get('where')}"
                    for f in ((data.get("profiles") or [{}])[-1].get("detail") or {}).get("scenario", {}).get("hot_functions", [])[:5]]}


def _node_json(args: list[str]) -> dict | None:
    cmd = tool_command().split() + args + ["--json"]
    r = quiet(cmd, timeout=120)
    try:
        return json.loads(r.stdout)
    except (ValueError, TypeError):
        return None


def _base_worktree(p):
    """A throwaway worktree at the starting commit, sharing node_modules, for a baseline taken after work began."""
    from . import gitops
    path = Path(str(p.wt) + "-perfbase")
    gitops.remove_worktree(p.task.get("repo"), path)
    add = quiet(["git", "worktree", "add", "--detach", str(path), p.base], cwd=p.task.get("repo"), timeout=300)
    if add.returncode != 0:
        return None
    if (Path(p.wt) / "node_modules").is_dir() and not (path / "node_modules").exists():
        (path / "node_modules").symlink_to(Path(p.wt) / "node_modules", target_is_directory=True)
    gitops.copy_ignored_root_files(p.task.get("repo"), path)
    return path


def _publish(p):
    st = _state(p)
    view = {k: st.get(k) for k in ("spec", "baseline", "latest", "compare", "asserts", "verdict", "ok", "reason", "time")}
    p.m.set_meta(p.tid, perf=view)


def take_baseline(p, pristine: bool) -> dict | None:
    st = _state(p)
    spec = st.get("spec")
    if not spec:
        return None
    p.r.timeline("verify", "Measuring performance on the starting commit", spec["url"])
    if pristine:
        base = _measure(p, p.wt, "baseline", spec)
    else:
        path = _base_worktree(p)
        if not path:
            base = {"ok": False, "label": "baseline", "error": "could not create a worktree at the starting commit", "time": now()}
        else:
            from . import gitops
            try:
                base = _measure(p, path, "baseline", spec)
            finally:
                gitops.remove_worktree(p.task.get("repo"), path)
    st["baseline"] = base
    st["baseline_spec"] = json.dumps(spec, sort_keys=True)
    p.save()
    _publish(p)
    p.r.timeline("verify", "Performance baseline recorded" if base.get("ok") else "Performance baseline failed",
                 "; ".join(base.get("top") or []) or base.get("error") or "")
    return base


def after_plan(p, env: dict) -> None:
    """Right after the plan: keep the perf spec, ask for it once if missing, and measure the starting commit."""
    if not applies(p):
        return
    st = _state(p)
    spec = normalize_spec(env.get("perf"))
    if not spec:
        p.r.timeline("supervisor", "Performance task without a measurement plan", "Asking the supervisor for a perf spec")
        _, env2 = p.supervisor_turn(
            "ORCHESTRATOR · this is a performance task and your plan has no `perf` object, so nothing can be measured. Reply with the same "
            "plan envelope plus \"perf\": {\"url\": …, \"serve\": …, \"steps\": … or null, \"throttle\": \"1,4\", \"targets\": [numeric targets]}. "
            + kickoff_note(p).split("\n", 1)[1],
            "Supervisor is adding the performance measurement plan", turn=0)
        spec = normalize_spec((env2 or {}).get("perf"))
    if not spec:
        st.update(ok=False, reason="the plan has no performance measurement (perf spec)", time=now())
        p.save()
        _publish(p)
        return
    if not spec["targets"]:
        spec["targets"] = list(DEFAULT_TARGETS)
    st["spec"] = spec
    take_baseline(p, pristine=True)


def update_spec(p, env: dict) -> None:
    """A later envelope may correct the perf spec (wrong URL, better scenario); the baseline is re-taken if needed."""
    if not applies(p) or not isinstance((env or {}).get("perf"), dict):
        return
    spec = normalize_spec(env["perf"])
    if not spec:
        return
    st = _state(p)
    if not spec["targets"]:
        spec["targets"] = (st.get("spec") or {}).get("targets") or list(DEFAULT_TARGETS)
    if spec == st.get("spec"):
        return
    st["spec"] = spec
    p.r.timeline("supervisor", "Performance measurement plan updated", spec["url"])
    changed = json.dumps({k: v for k, v in spec.items() if k != "targets"}, sort_keys=True) != \
        json.dumps({k: v for k, v in json.loads(st.get("baseline_spec") or "{}").items() if k != "targets"}, sort_keys=True)
    if changed or not (st.get("baseline") or {}).get("ok"):
        take_baseline(p, pristine=False)
    p.save()


def pipeline_verify(p) -> tuple[list[dict], list[str]]:
    """Measure the worktree, compare with the baseline, check targets. A performance task without numbers fails."""
    if not applies(p):
        return [], []
    st = _state(p)
    spec = st.get("spec")
    name = "relay-perf · performance targets"
    if not spec:
        reason = st.get("reason") or "no perf spec in the plan"
        st.update(ok=False, reason=reason, time=now())
        _publish(p)
        return ([{"command": name, "ok": False, "passed": False, "rc": 1, "skipped": False, "pre_existing": False, "optional": False,
                  "duration": 0, "perf": True}],
                [f"$ {name}\nFAIL: performance task without a measurement — {reason}. Add a `perf` object "
                 "(url, serve, steps, throttle, targets) to your next envelope; Relay measures the starting commit and the worktree with it."])
    if not (st.get("baseline") or {}).get("ok"):
        take_baseline(p, pristine=False)
    from . import judge
    fp = judge.worktree_fingerprint(p.wt, p.base)
    latest = st.get("latest") or {}
    if latest.get("fingerprint") != fp or latest.get("spec") != json.dumps(spec, sort_keys=True):
        n = int(st.get("runs") or 0) + 1
        st["runs"] = n
        latest = _measure(p, p.wt, f"after-{n}", spec)
        latest["fingerprint"] = fp
        latest["spec"] = json.dumps(spec, sort_keys=True)
        st["latest"] = latest
    base = st.get("baseline") or {}
    started = latest.get("time")
    reasons, parts = [], []
    compare = asserts = None
    if not latest.get("ok"):
        reasons.append(latest.get("error") or "the measurement failed")
    if not base.get("ok"):
        reasons.append("no baseline: " + (base.get("error") or "the starting commit could not be measured"))
    if latest.get("dir") and base.get("dir") and (Path(latest["dir"]) / "perf.json").exists() and (Path(base["dir"]) / "perf.json").exists():
        b, a = str(Path(base["dir"]) / "perf.json"), str(Path(latest["dir"]) / "perf.json")
        compare = _node_json(["compare", b, a, "--md", str(Path(latest["dir"]) / "COMPARE.md"), "--out", str(Path(latest["dir"]) / "compare.json")])
        asserts = _node_json(["assert", a, *spec["targets"], "--baseline", b])
        if compare and compare.get("verdict") == "regressed":
            reasons.append("regressed: " + ", ".join(compare.get("regressed") or []))
        if asserts and not asserts.get("ok"):
            reasons.append("targets missed: " + "; ".join(f"{r.get('expr')} (got {r.get('value')}{', target ' + str(r.get('target')) if r.get('target') is not None else ''}{', ' + r['error'] if r.get('error') else ''})"
                                                          for r in asserts.get("results") or [] if not r.get("ok")))
        if compare is None or asserts is None:
            reasons.append("the comparison could not be computed")
    ok = not reasons
    st.update(ok=ok, reason="; ".join(reasons), compare=_compact_compare(compare), asserts=(asserts or {}).get("results"),
              verdict=(compare or {}).get("verdict"), time=started or now())
    p.save()
    _publish(p)
    head = f"$ {name}\n{'PASS' if ok else 'FAIL'} ({(compare or {}).get('verdict') or 'not compared'})"
    body = []
    if reasons:
        body.append("Why: " + "; ".join(reasons))
    for r in (asserts or {}).get("results") or []:
        body.append(f"  {'ok ' if r.get('ok') else 'MISS'} {r.get('expr')} → {r.get('value')}" + (f" (target {r.get('target')})" if r.get("target") is not None else ""))
    if compare:
        key = [r for r in compare.get("rows") or [] if r.get("key") and r.get("verdict") != "unchanged"][:12]
        for r in key:
            body.append(f"  {r['verdict']:<9} {r['metric']}: {r['before']} → {r['after']} ({'+' if (r.get('delta') or 0) > 0 else ''}{r.get('delta')})")
    if latest.get("top"):
        body.append("Hottest functions now: " + " | ".join(latest["top"]))
    if latest.get("dir"):
        body.append(f"Report: {Path(latest['dir']) / 'PERF_REPORT.md'}; comparison: {Path(latest['dir']) / 'COMPARE.md'}; baseline: {Path(base.get('dir') or '') / 'PERF_REPORT.md'}")
    parts.append(head + ("\n" + truncate("\n".join(body), 3500) if body else ""))
    item = {"command": name, "ok": ok, "passed": ok, "rc": 0 if ok else 1, "skipped": False, "pre_existing": False, "optional": False,
            "duration": 0, "perf": True, "verdict": (compare or {}).get("verdict")}
    return [item], parts


def _compact_compare(c):
    if not c:
        return None
    return {"verdict": c.get("verdict"), "improved": c.get("improved"), "regressed": c.get("regressed"),
            "rows": [r for r in c.get("rows") or [] if r.get("key") or r.get("verdict") != "unchanged"][:60]}
