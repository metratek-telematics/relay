"""Retrospective: one short, cheap agent turn after a task ends, looking back at how it went.

The agent gets a compact digest (the scorecard, timeline, review findings, failed
verification, errors and what humans had to do) and returns what went well, what
went wrong, the root causes, and at most a few one-sentence lessons. Lessons go to
the review queue in orchestrator/lessons.py; a person decides what future tasks see.

It runs in a scratch folder outside the task's worktree, never blocks the task
(Learning runs it on a background thread after the result is recorded), and a
failure is only logged on the task.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import agents, config as C, lessons, protocol
from .util import RUNTIME_DIR, kill_tree, now, popen_group_kwargs, quiet, truncate, write_text

DIGEST_CHARS = 14000


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def digest(task: dict, messages: list[dict], card: dict, existing: list[dict]) -> str:
    """Everything the retrospective may use, compact enough for a cheap model."""
    plan = task.get("plan") or (task.get("checkpoint") or {}).get("plan") or {}
    run_start = task.get("started_at") or ""
    lines = [f"TASK: {task.get('name')}", f"Type: {task.get('template') or 'feature'} · repository: {card.get('repo_label')}",
             f"Team: {card.get('pairing_label') or '(unknown)'}" + "".join(
                 f" · {r} model {v['model']}" for r, v in (card.get("team") or {}).items() if v.get("model")),
             "", "REQUEST", _clip(task.get("requirements") or task.get("github_issue_title") or "", 1500)]
    if plan.get("acceptance"):
        lines += ["", "ACCEPTANCE CRITERIA"] + [f"- {_clip(a, 200)}" for a in plan["acceptance"][:10]]
    h, tr, v = card.get("human") or {}, card.get("agent_trouble") or {}, card.get("verification") or {}
    lines += ["", "SCORECARD (computed by Relay; the base score depends on how Relay delivered, not on the agents)",
              f"Outcome: {card.get('outcome_label')}" + (f" · {card.get('failure_label')}: {card.get('failure_detail')}" if card.get("failure_label") else ""),
              f"Score: {card.get('score')}/100 (" + "; ".join("%s %+d" % (p["label"], p["points"]) for p in card.get("score_parts") or []) + ")",
              f"Duration: {round((card.get('duration_seconds') or 0) / 60, 1)} min · work packages {card.get('work_packages')} · turns {card.get('turns')}"
              f" · revisions {card.get('revisions')} · review rounds {card.get('review_rounds')}",
              f"Verification runs {v.get('runs')} (first passed: {v.get('first_ok')}, last passed: {v.get('final_ok')}) · blocked checks {card.get('blocked_checks')}",
              f"Humans: {h.get('questions', 0)} question(s) answered, {h.get('guidance', 0)} guidance message(s), {h.get('interrupts', 0)} interrupt(s), "
              f"{h.get('rejections', 0)} rejected approval(s), {h.get('file_edits', 0)} manual file edit(s), {h.get('retries', 0)} retry(ies)",
              f"Agent trouble: {tr.get('failures', 0)} failed turn(s), {tr.get('timeouts', 0)} timeout(s), {tr.get('envelope_nudges', 0)} missing envelope(s), "
              f"{tr.get('fresh_sessions', 0)} fresh session(s)",
              f"Cost: ${card.get('cost_usd', 0):.2f}{' (estimated)' if card.get('cost_estimated') else ''}"]
    if card.get("outcome") == "done_no_pr":
        lines.append("Delivery: " + ("the repository has no GitHub remote, so Relay committed to a local branch. That is expected, not a problem to fix."
                                     if not card.get("github_repo") else "Relay did not open a pull request (a Relay setting, not an agent decision)."))
    if card.get("pr"):
        p = card["pr"]
        lines.append(f"Pull request: {p.get('state')}" + (f", {p['human_commits']} human commit(s) after delivery" if p.get("human_commits") else ""))

    events = [e for e in task.get("events") or [] if (e.get("time") or "") >= run_start[:19]]
    lines += ["", "TIMELINE (oldest first)"] + [f"- [{e.get('role')}] {_clip(e.get('title'), 90)}" + (f": {_clip(e.get('detail'), 180)}" if e.get("detail") else "")
                                              for e in events[-60:]]
    msgs = [m for m in messages if not run_start or (m.get("time") or "") >= run_start[:19]]
    decisions = [m for m in msgs if m.get("kind") == "decision" and m.get("decision") == "revise"]
    if decisions:
        lines += ["", "REVISION REQUESTS FROM THE SUPERVISOR"] + [f"- {_clip(m.get('summary') or m.get('content'), 300)}" for m in decisions[-6:]]
    reviews = [m for m in msgs if m.get("kind") == "review"]
    if reviews:
        lines += ["", "INDEPENDENT REVIEW"]
        for m in reviews[-4:]:
            lines.append(f"- {m.get('verdict')}: {_clip(m.get('summary'), 240)}")
            lines += [f"  · [{f.get('severity', 'blocking')}] {_clip(f.get('file'), 80)}: {_clip(f.get('problem'), 220)}" for f in (m.get("findings") or [])[:6]]
    failed = [(m, i) for m in msgs if m.get("kind") == "verification" for i in (m.get("items") or []) if not i.get("ok")]
    if failed:
        lines += ["", "FAILED VERIFICATION COMMANDS"] + sorted({f"- {i.get('command')} (exit {i.get('rc')})" for _, i in failed})[:10]
    blocked = task.get("blocked_checks") or []
    if blocked:
        lines += ["", "CHECKS THAT COULD NOT RUN"] + [f"- {_clip(b.get('check'), 80)}: {_clip(b.get('reason'), 200)}" for b in blocked[:8]]
    errors = [m for m in msgs if m.get("kind") == "error"]
    if errors:
        lines += ["", "ERRORS"] + [f"- [{m.get('role')}] {_clip(m.get('content'), 300)}" for m in errors[-8:]]
    humans = [m for m in msgs if m.get("kind") in ("user", "question")]
    if humans:
        lines += ["", "HUMAN INVOLVEMENT"]
        for m in humans[-10:]:
            if m.get("kind") == "question":
                lines.append(f"- {m.get('role')} asked: {_clip(m.get('content'), 200)} → answer: {_clip(m.get('answer'), 160)}")
            else:
                lines.append(f"- human to {m.get('to') or 'team'}: {_clip(m.get('content'), 240)}")
    if existing:
        lines += ["", "EXISTING LESSONS (already approved; do not propose these again)"] + [f"- {_clip(x.get('text'), 200)}" for x in existing[:20]]
    return truncate("\n".join(lines), DIGEST_CHARS)


def prompt(dig: str, max_lessons: int) -> str:
    return f"""You are writing a short, blameless retrospective of one finished run of Relay, a multi-agent coding orchestrator: a supervisor agent plans and checks, a worker agent implements, an optional reviewer gates delivery, and humans can answer questions or steer.

Do not use any tools and do not read or change files: everything you need is in the digest below. Be brief.

Reply with ONLY one fenced ```json block of this shape:
{{"type":"retrospective","what_went_well":["..."],"what_went_wrong":["..."],"root_causes":["..."],"lessons":[{{"scope":"repo","text":"...","evidence":"..."}}]}}

- what_went_well, what_went_wrong, root_causes: at most 4 short items each, grounded in the digest.
- lessons: at most {max_lessons}. Propose none when nothing would change how a future task is run; never pad.
- Each lesson text is ONE actionable sentence an agent on a future task can follow (an instruction, not an observation), under 200 characters.
- Lessons are only about how the agents should work: facts about this codebase, commands, conventions, pitfalls, and how to plan, check or report. Relay itself decides delivery, pull requests, retries and scoring, and a Relay crash is not the agents' fault: mention those under what_went_wrong or root_causes, never as a lesson.
- scope "repo" for anything about this repository (its build, tests, layout, conventions, environment); "global" only if it would help on any repository.
- evidence names what in the digest supports the lesson (a timeline entry, finding, error or human message).
- Do not repeat EXISTING LESSONS or generic advice such as "write tests" or "be careful". Never include secrets, tokens or credentials.

RUN DIGEST
{dig}
"""


def _strings(value, n=4, chars=300) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [_clip(x, chars) for x in (value or []) if isinstance(x, (str, int, float)) and str(x).strip()][:n]


def parse(text: str, max_lessons: int) -> dict | None:
    env = protocol.parse_envelope(text or "")
    if not env:
        try:
            obj = json.loads((text or "").strip())
            env = obj if isinstance(obj, dict) else None
        except ValueError:
            env = None
    if not env or not any(k in env for k in ("lessons", "what_went_well", "what_went_wrong")):
        return None
    out = {"what_went_well": _strings(env.get("what_went_well")), "what_went_wrong": _strings(env.get("what_went_wrong")),
           "root_causes": _strings(env.get("root_causes")), "lessons": []}
    for x in env.get("lessons") or []:
        if isinstance(x, str):
            x = {"text": x}
        if not isinstance(x, dict) or not str(x.get("text") or "").strip():
            continue
        out["lessons"].append({"scope": "global" if str(x.get("scope")).lower() == "global" else "repo",
                               "text": _clip(x["text"], 300), "evidence": _clip(x.get("evidence"), 400)})
    out["lessons"] = out["lessons"][:max(0, max_lessons)]
    return out


def markdown(task: dict, card: dict, result: dict, proposed: list[dict], meta: dict) -> str:
    sec = lambda title, rows: f"## {title}\n" + ("\n".join(f"- {r}" for r in rows) if rows else "_(none)_") + "\n"
    lessons_md = "\n".join(f"- **{x['scope']}** · {x['text']}" + (f"  \n  _Evidence:_ {x['evidence']}" if x.get("evidence") else "")
                           for x in result["lessons"]) or "_(none proposed)_"
    return (f"# Retrospective · {task.get('name')}\n\n"
            f"**Outcome:** {card.get('outcome_label')} · **Score:** {card.get('score')}/100 · "
            f"**By:** {C.AGENTS.get(meta['agent'], {}).get('label', meta['agent'])}{' · ' + meta['model'] if meta.get('model') else ''}"
            f"{' · ' + meta['effort'] + ' effort' if meta.get('effort') else ''} · {meta['time']}\n\n"
            + sec("What went well", result["what_went_well"]) + "\n" + sec("What went wrong", result["what_went_wrong"]) + "\n"
            + sec("Root causes", result["root_causes"]) + "\n"
            + f"## Proposed lessons\n{lessons_md}\n\n"
            + f"{len(proposed)} new lesson(s) queued for review; duplicates of existing lessons are dropped. Nothing reaches a prompt until someone approves it.\n")


def choose_agent(task: dict, cfg: dict) -> tuple[str, str, str]:
    """(agent, model, effort): the configured retrospective agent, else the task's supervisor, at its lowest effort.

    Without a configured model it uses the agent's cheap subagent model (Settings → Usage, e.g. haiku for Claude),
    then the supervisor's own model, then the agent's default.
    """
    roles = (task.get("workflow") or {}).get("roles") or {}
    sup = roles.get("supervisor") or {}
    agent = (cfg.get("retro_agent") or "").strip() or sup.get("agent") or ""
    model = (cfg.get("retro_model") or "").strip() or ((cfg.get("subagent_models") or {}).get(agent) or "").strip()
    if not model:
        model = (sup.get("model") or "") if agent == sup.get("agent") else ((cfg.get("agent_defaults") or {}).get(agent) or {}).get("model", "")
    efforts = C.AGENTS.get(agent, {}).get("efforts") or []
    effort = (cfg.get("retro_effort") or "").strip()
    effort = effort if effort in efforts else (efforts[0] if efforts else "")
    return agent, model, effort


def run_agent(agent: str, model: str, effort: str, text: str, cfg: dict, workdir: Path, timeout: float) -> dict:
    """One turn through the agent's adapter, the way the Agents page tests an agent."""
    ad = agents.adapter(agent)
    workdir.mkdir(parents=True, exist_ok=True)
    if not (workdir / ".git").exists():
        quiet(["git", "init", "-q"], cwd=workdir, timeout=30)
    cfg = {**cfg, "claude_max_turns_per_call": 3}
    args, env, stdin, session = ad.build(text, workdir, cfg, model, None, workdir, "retro", effort=effort)
    ctx = agents.TurnContext()
    started = time.time()
    p = subprocess.Popen(args, cwd=str(workdir), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                         env=env, **popen_group_kwargs())
    try:
        out, _ = p.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        raise TimeoutError(f"The retrospective took longer than {int(timeout)} s")
    tail = []
    for line in (out or "").splitlines():
        try:
            ad.parse_line(line, ctx)
        except Exception:
            tail.append(line)
    res = ad.finalize(ctx, p.returncode, workdir, session)
    res["seconds"] = round(time.time() - started, 1)
    if p.returncode != 0 and not res.get("text"):
        res["ok"] = False
        res["error"] = res.get("error") or truncate("\n".join([l for l in (out or "").splitlines() if l.strip()][-8:]), 800)
    return res


def run(manager, tid: str) -> dict | None:
    """Run the retrospective for a finished task and record the result on it. Raises nothing."""
    cfg = manager.cfg()
    task = manager.store.get(tid)
    if not task or manager.store.is_deleted(tid) or task.get("status") not in ("done", "failed", "stopped"):
        return None
    card = task.get("scorecard") or {}
    agent, model, effort = choose_agent(task, cfg)
    meta = {"status": "running", "time": now(), "agent": agent, "model": model, "effort": effort}
    if not agent or agent not in C.AGENTS:
        manager.set_meta(tid, retro={**meta, "status": "failed", "error": f"No agent for the retrospective ('{agent}')."})
        return None
    manager.set_meta(tid, retro=meta)
    try:
        existing = lessons.approved(card.get("repo") or "")
        text = prompt(digest(task, manager.store.messages(tid), card, existing), int(cfg.get("retro_max_lessons") or 3))
        res = run_agent(agent, model, effort, text, cfg, RUNTIME_DIR / tid / "retro", float(cfg.get("retro_timeout_seconds") or 300))
        usage = res.get("usage") or {}
        cost, estimated = manager.estimate_cost(agent, usage)
        meta.update(seconds=res.get("seconds"), cost_usd=round(cost, 4), estimated=estimated, model=res.get("model") or model,
                    tokens={"input": int(usage.get("input") or 0), "output": int(usage.get("output") or 0)})
        if not res.get("ok") and not res.get("text"):
            raise RuntimeError(res.get("error") or "the agent turn failed")
        result = parse(res.get("last_message") or res.get("text") or "", int(cfg.get("retro_max_lessons") or 3))
        if not result:
            raise RuntimeError("The agent did not return a retrospective JSON block: " + _clip(res.get("text"), 300))
        latest = manager.store.get(tid) or task
        proposed = lessons.propose(result["lessons"], latest, card.get("repo") or "", card.get("repo_label") or "")
        meta.update(status="done", finished=now(), what_went_well=result["what_went_well"], what_went_wrong=result["what_went_wrong"],
                    root_causes=result["root_causes"], lessons_suggested=len(result["lessons"]), lessons_proposed=[x["id"] for x in proposed])
        path = RUNTIME_DIR / tid / "RETRO.md"
        write_text(path, markdown(latest, card, result, proposed, meta))
        manager.artifact(tid, "retro", str(path))
        manager.set_meta(tid, retro=meta)
        manager.timeline(tid, "system", "Retrospective written",
                         f"{len(proposed)} lesson(s) proposed for review" if proposed else "no new lessons proposed")
        if proposed:
            manager.emit("lessons", {"pending": lessons.pending_count()})
        return meta
    except Exception as e:  # never surfaces as a task failure
        meta.update(status="failed", error=truncate(str(e), 500), finished=now())
        manager.set_meta(tid, retro=meta)
        manager.timeline(tid, "system", "Retrospective could not run", truncate(str(e), 200))
        return meta
