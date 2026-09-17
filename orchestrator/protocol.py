"""Inter-agent protocol: envelope parsing and prompt construction."""
from __future__ import annotations

import json
import re
import time

from . import config as C
from .util import RULES_DIR, read_text, truncate

_rules_cache = {"at": 0, "data": {}}

RULE_FILES = ["CORE", "PROTOCOL", "SUPERVISOR", "WORKER", "REVIEW", "PLANNER", "ENGINEERING", "FRONTEND",
              "DESIGN", "TESTING", "SECURITY", "GIT_GITHUB", "PERFORMANCE_RELIABILITY", "DATA_API", "DOCUMENTATION"]


def rules() -> dict:
    if time.time() - _rules_cache["at"] > 30:
        data = {}
        for name in RULE_FILES:
            p = RULES_DIR / f"{name}.md"
            data[name] = read_text(p) if p.exists() else ""
        _rules_cache["data"] = data
        _rules_cache["at"] = time.time()
    return _rules_cache["data"]


# Rule sets per role. "lean" keeps only what the role must have; "full" sends everything.
LEAN_RULES = {
    "supervisor": ["CORE", "SUPERVISOR", "PROTOCOL", "PLANNER"],
    "worker": ["CORE", "WORKER", "PROTOCOL", "ENGINEERING"],
    "reviewer": ["CORE", "REVIEW", "PROTOCOL"],
}
FULL_RULES = {
    "supervisor": ["CORE", "SUPERVISOR", "PROTOCOL", "PLANNER", "ENGINEERING", "FRONTEND", "DESIGN", "TESTING",
                   "SECURITY", "GIT_GITHUB", "PERFORMANCE_RELIABILITY", "DATA_API", "DOCUMENTATION"],
    "worker": ["CORE", "WORKER", "PROTOCOL", "ENGINEERING", "FRONTEND", "DESIGN", "TESTING", "SECURITY",
               "GIT_GITHUB", "PERFORMANCE_RELIABILITY", "DATA_API", "DOCUMENTATION"],
    "reviewer": ["CORE", "REVIEW", "PROTOCOL", "ENGINEERING", "FRONTEND", "DESIGN", "TESTING", "SECURITY", "DATA_API"],
}
# Extra rule files pulled in only when the task/repository actually touches that area.
CONTEXTUAL = {
    "FRONTEND": ("frontend", "ui", "css", "overhaul", "look and feel", "react", "vue", "svelte", "component", "page", "style", "html", "tsx", "jsx",
                 "layout", "design", "screen", "dashboard", "modal", "button", "form", "table", "responsive", "theme"),
    "DESIGN": ("frontend", "ui", "css", "overhaul", "look and feel", "react", "vue", "svelte", "component", "page", "style", "html", "tsx", "jsx",
               "layout", "design", "screen", "dashboard", "modal", "button", "form", "table", "responsive", "theme"),
    "TESTING": ("test", "spec", "coverage", "pytest", "jest", "vitest"),
    "SECURITY": ("auth", "token", "password", "secret", "permission", "crypt", "login", "session", "sql"),
    "DATA_API": ("api", "endpoint", "schema", "database", "migration", "sql", "query", "rest", "graphql"),
    "GIT_GITHUB": ("commit", "branch", "pull request", "merge", "rebase"),
    "DOCUMENTATION": ("doc", "readme", "changelog", "guide"),
    "PERFORMANCE_RELIABILITY": ("performance", "slow", "latency", "memory", "leak", "cache", "concurren", "race"),
}


def rule_names_for(role: str, cfg: dict, task_text: str = "") -> list[str]:
    """Pick the rule files for a role, optionally trimmed to what the task needs."""
    if not cfg.get("lean_prompts", True):
        return FULL_RULES.get(role, ["CORE", "PROTOCOL"])
    names = list(LEAN_RULES.get(role, ["CORE", "PROTOCOL"]))
    low = (task_text or "").lower()
    for extra, words in CONTEXTUAL.items():
        # Match at word starts: a bare substring test let "ui" match "build" and "require",
        # pulling the frontend rules into nearly every task.
        if extra not in names and any(re.search(r"\b" + re.escape(w), low) for w in words):
            names.append(extra)
    return names


def rules_block(names: list[str]) -> str:
    r = rules()
    parts = []
    for n in names:
        if r.get(n):
            parts.append(f"### {n.replace('_', ' ')}\n{r[n].strip()}")
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------- envelopes
_FENCE = re.compile(r"```(?:json|JSON|jsonc)?\s*\n(.*?)\n\s*```", re.S)


def _loads(s: str):
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) and obj.get("type") else None
    except Exception:
        return None


def parse_envelope(text: str):
    """Return the last valid envelope dict in text, or None."""
    if not text:
        return None
    for block in reversed(_FENCE.findall(text)):
        obj = _loads(block.strip())
        if obj:
            return obj
    # Fallback: scan for the last balanced JSON object containing "type"
    s = text
    end = len(s)
    attempts = 0
    while attempts < 40:
        close = s.rfind("}", 0, end)
        if close < 0:
            break
        depth = 0
        start = -1
        i = close
        in_str = False
        esc = False
        while i >= 0:
            ch = s[i]
            if in_str:
                if ch == '"' and not esc:
                    in_str = False
                esc = (ch == "\\" and not esc)
            else:
                if ch == '"':
                    in_str = True
                elif ch == "}":
                    depth += 1
                elif ch == "{":
                    depth -= 1
                    if depth == 0:
                        start = i
                        break
            i -= 1
        if start >= 0:
            obj = _loads(s[start:close + 1])
            if obj:
                return obj
        end = close
        attempts += 1
    return None


def envelope_from_result(res: dict):
    for key in ("last_message", "text"):
        env = parse_envelope(res.get(key) or "")
        if env:
            return env
    return None


def verdict_from_text(text: str):
    m = re.search(r"VERDICT:\s*(PASS|FAIL)", text or "", re.I)
    return m.group(1).upper() if m else None


# ----------------------------------------------------------------------------- context packet
def _str_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [line.strip(" -*\t") for line in value.splitlines() if line.strip(" -*\t")]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def normalize_packet(env: dict) -> dict:
    """The supervisor's plan as structured evidence the worker and reviewer can build on.

    Agents write envelopes by hand, so every field is tolerated in loose shapes:
    strings or lists, findings as objects or plain lines.
    """
    files = env.get("known_files") or {}
    if isinstance(files, list):
        files = {"primary": files}
    findings = []
    for f in env.get("findings") or []:
        if isinstance(f, dict):
            text = str(f.get("finding") or f.get("note") or "").strip()
            if text:
                findings.append({"file": str(f.get("file") or "").strip(), "finding": text})
        elif str(f).strip():
            findings.append({"file": "", "finding": str(f).strip()})
    return {
        "requirements": _str_list(env.get("requirements")),
        "acceptance": _str_list(env.get("acceptance")),
        "optional": _str_list(env.get("optional")),
        "known_files": {"primary": _str_list(files.get("primary")), "supporting": _str_list(files.get("supporting"))},
        "findings": findings,
        "constraints": _str_list(env.get("constraints")),
        "unknowns": _str_list(env.get("unknowns")),
    }


def packet_block(plan: dict) -> str:
    """Render the context packet compactly; empty sections are left out."""
    lines = []
    for key, title in (("requirements", "REQUIREMENTS (the user's request; the only mandatory scope)"),
                       ("acceptance", "ACCEPTANCE (derived from the requirements; must all hold for done)"),
                       ("optional", "OPTIONAL (only if cheap and clearly inside the request; never blocks done)")):
        if plan.get(key):
            lines += [title, *[f"- {x}" for x in plan[key]], ""]
    files = plan.get("known_files") or {}
    if files.get("primary") or files.get("supporting"):
        lines.append("KNOWN FILES")
        if files.get("primary"):
            lines += ["  primary:", *[f"  - {x}" for x in files["primary"]]]
        if files.get("supporting"):
            lines += ["  supporting:", *[f"  - {x}" for x in files["supporting"]]]
        lines.append("")
    if plan.get("findings"):
        lines += ["FINDINGS (what the supervisor observed; verify what your package depends on)",
                  *[f"- {f['file'] + ': ' if f.get('file') else ''}{f['finding']}" for f in plan["findings"]], ""]
    for key, title in (("constraints", "CONSTRAINTS"), ("unknowns", "UNKNOWNS")):
        if plan.get(key):
            lines += [title, *[f"- {x}" for x in plan[key]], ""]
    return "\n".join(lines).strip() or "(the supervisor did not provide a context packet)"


def blocked_checks(env: dict) -> list[dict]:
    out = []
    for b in env.get("blocked_checks") or []:
        if isinstance(b, dict) and (b.get("check") or b.get("reason")):
            out.append({"check": str(b.get("check") or "").strip(), "reason": str(b.get("reason") or "").strip(),
                        "impact": str(b.get("impact") or "").strip(), "action_required": bool(b.get("action_required"))})
    return out


def blockers(env: dict) -> list[dict]:
    out = []
    for b in env.get("blockers") or []:
        if isinstance(b, dict) and b.get("reason"):
            out.append({"reason": str(b.get("reason")).strip(), "impact": str(b.get("impact") or "").strip(),
                        "action_required": bool(b.get("action_required"))})
        elif isinstance(b, str) and b.strip():
            out.append({"reason": b.strip(), "impact": "", "action_required": False})
    return out


def blocked_block(checks: list[dict]) -> str:
    if not checks:
        return "(none)"
    return "\n".join(f"- {c['check'] or '(check)'}: {c['reason']}" + (f" · impact: {c['impact']}" if c.get("impact") else "")
                     + (" · needs the user" if c.get("action_required") else "") for c in checks)


# ----------------------------------------------------------------------------- prompts
def _label(agent):
    return C.AGENTS.get(agent, {}).get("label", agent or "none")


def team_intro(task: dict, wt, branch) -> str:
    roles = task.get("workflow", {}).get("roles", {})
    sup = roles.get("supervisor", {}).get("agent")
    wrk = roles.get("worker", {}).get("agent")
    rev = roles.get("reviewer", {}).get("agent")
    lines = [
        "TEAM",
        f"- Supervisor / technical lead: {_label(sup)} — plans, delegates, verifies, decides.",
        f"- Worker / implementation engineer: {_label(wrk)} — implements work packages with full tool access.",
        f"- Independent reviewer: {_label(rev) if rev else 'none (the supervisor is the final gate)'}.",
        "- Human operator: can answer questions and send guidance at any time through the Relay UI.",
        "- Orchestrator: relays messages, runs verification commands, commits, pushes and opens the pull request.",
        "",
        f"REPOSITORY: {wt}",
        f"BRANCH: {branch} (isolated git worktree; the user's main checkout is untouched)",
        f"TASK ID: {task['id']}",
    ]
    return "\n".join(lines)


def task_block(task: dict, issue_text: str = "", refs_text: str = "") -> str:
    parts = ["TASK", task.get("requirements", "").strip() or "(see issue)"]
    if issue_text:
        parts += ["", "GITHUB ISSUE", issue_text]
    if refs_text:
        parts += ["", "ATTACHMENTS", refs_text]
    return "\n".join(parts)


def supervisor_kickoff(task, wt, branch, issue_text, refs_text, guidance, verify_cmds, cfg=None, env_text="") -> str:
    cfg = cfg or {}
    return f"""You are the SUPERVISOR in a multi-agent engineering team run by Relay.

{team_intro(task, wt, branch)}

{task_block(task, issue_text, refs_text)}

USER GUIDANCE RECEIVED SO FAR
{guidance or "(none)"}

{("ENVIRONMENT PREPARED BY RELAY" + chr(10) + env_text + chr(10) + chr(10)) if env_text else ""}VERIFICATION COMMANDS THE ORCHESTRATOR WILL RUN AFTER EACH WORKER REPORT
{chr(10).join('- ' + c for c in verify_cmds) if verify_cmds else "(none detected — ask the worker to add or name the right checks if the repository has any)"}

{rules_block(rule_names_for("supervisor", cfg, task.get("requirements", "") + " " + (issue_text or "")))}

HOW THIS SESSION WORKS
- This session is persistent. Every future message you receive is from the worker, the reviewer, the human, or the orchestrator. You remember everything, so never repeat the plan back; just act on the new message.
- Use your tools freely to inspect the repository, read files, run tests and view diffs. Do NOT implement the task yourself.
- Every reply must end with exactly one fenced ```json envelope as defined in the protocol.

YOUR FIRST JOB
Inspect the repository enough to plan: the files the request touches, existing local changes, build/test setup. Then reply with the "plan" envelope. It is the team's shared context packet, so the worker does not repeat your discovery:
- requirements: the user's request restated as short items, nothing added;
- acceptance: observable criteria derived from those requirements only;
- optional: improvements you would suggest but the user did not ask for (they never block done);
- known_files (primary, supporting), findings (file + what you saw), constraints, unknowns;
- summary, a short markdown plan listing the work packages in order, and the first work package as instruction.
Rule files describe how to work; never copy their checklists into requirements or acceptance.
Keep the instruction short: name the concern, the files, the expected result. The packet already carries the context.
"""


def supervisor_after_report(worker_label, turn, report_env, report_text, verification, changed, diffstat, guidance, remaining, cfg=None) -> str:
    cfg = cfg or {}
    max_report = int(cfg.get("budget_report_chars") or 14000)
    max_files = int(cfg.get("budget_file_list") or 80)
    status = (report_env or {}).get("status", "unknown")
    files = (report_env or {}).get("files") or []
    body = (report_env or {}).get("report") or report_text
    lines = [
        f"REPORT FROM WORKER ({worker_label}) · work package #{turn} · status: {status}",
        f"Summary: {(report_env or {}).get('summary','')}",
        "",
        truncate(body, max_report),
    ]
    if files:
        shown = files[:max_files]
        lines += ["", "Files the worker says it changed:", *[f"- {f}" for f in shown]]
        if len(files) > len(shown):
            lines.append(f"- …and {len(files) - len(shown)} more")
    checks = blocked_checks(report_env or {})
    stops = blockers(report_env or {})
    if checks:
        lines += ["", "Checks the worker could not run", blocked_block(checks)]
    if stops:
        lines += ["", "Blockers the worker reported", *[f"- {b['reason']}" + (f" · impact: {b['impact']}" if b['impact'] else "")
                                                         + (" · needs the user" if b['action_required'] else "") for b in stops]]
    lines += ["", "ORCHESTRATOR · verification results", verification or "(verification not run at this point)"]
    lines += ["", f"ORCHESTRATOR · git status ({diffstat.get('files',0)} files, +{diffstat.get('insertions',0)} / -{diffstat.get('deletions',0)})"]
    shown_changed = changed[:max_files]
    lines += [f"- {c['status']} {c['path']}" for c in shown_changed] or ["(clean working tree)"]
    if len(changed) > len(shown_changed):
        lines.append(f"- …and {len(changed) - len(shown_changed)} more (run git status yourself)")
    if guidance:
        lines += ["", "USER GUIDANCE (new)", guidance]
    lines += ["", f"ORCHESTRATOR · {remaining} work package(s) remain before the turn budget is exhausted."]
    lines += ["", "Inspect the diff and the files this package changed (not the whole repository again). Judge against requirements and acceptance only; a failure matching a recorded blocked check is not the worker's defect.",
              "Then reply with ONE envelope:",
              '- {"type":"decision","decision":"revise",...} with exact required fixes, or',
              '- {"type":"instruction",...} with the next work package, or',
              '- {"type":"decision","decision":"done",...} with a pr_summary when every acceptance criterion is met with evidence, or',
              '- {"type":"question",...} if you are genuinely blocked.']
    return "\n".join(lines)


def supervisor_after_review(reviewer_label, review_env, review_text, round_no, max_rounds) -> str:
    verdict = (review_env or {}).get("verdict", "FAIL")
    findings = (review_env or {}).get("findings") or []
    lines = [f"REVIEW FROM REVIEWER ({reviewer_label}) · round {round_no}/{max_rounds} · verdict: {verdict}",
             f"Summary: {(review_env or {}).get('summary','')}", ""]
    if findings:
        for i, f in enumerate(findings, 1):
            lines.append(f"{i}. [{f.get('severity','blocking')}] {f.get('file','')}: {f.get('problem','')}\n   Fix: {f.get('fix','')}")
    else:
        lines.append(truncate(review_text, 8000))
    lines += ["", "The reviewer blocked delivery. Verify each finding against the repository. Reply with a",
              '{"type":"decision","decision":"revise",...} envelope containing the exact fixes for the worker.',
              "If you believe a finding is wrong, still address it with evidence in the instruction so the reviewer can re-check."]
    return "\n".join(lines)


def supervisor_worker_question(worker_label, question) -> str:
    return (f"QUESTION FROM WORKER ({worker_label})\n{question}\n\n"
            "Answer precisely from the repository and the plan. Reply with an {\"type\":\"instruction\",...} envelope "
            "containing your answer and how to proceed, or escalate with a question to the user if this needs a human decision.")


def worker_kickoff(task, wt, branch, issue_text, refs_text, plan_env, instruction, guidance, cfg=None, gate_cmd="", env_text="") -> str:
    cfg = cfg or {}
    return f"""You are the WORKER (implementation engineer) in a multi-agent engineering team run by Relay.

{team_intro(task, wt, branch)}

{task_block(task, issue_text, refs_text)}

PLAN AGREED BY THE SUPERVISOR
{plan_env.get('plan','')}

CONTEXT PACKET FROM THE SUPERVISOR
{packet_block(plan_env)}
{(chr(10) + "ENVIRONMENT PREPARED BY RELAY" + chr(10) + env_text + chr(10)) if env_text else ""}
{rules_block(rule_names_for("worker", cfg, task.get("requirements", "") + " " + str(plan_env.get("plan", ""))))}

HOW THIS SESSION WORKS
- This session is persistent. Future messages are work packages, answers, or guidance. You remember everything.
- Treat the context packet as prior repository inspection. Verify the files and assumptions your package depends on; do not repeat broad discovery unless the packet is missing, contradictory or stale.
- Implement only the concern in the current work package. Run fast checks focused on what you changed; the orchestrator runs the full verification.
- If a check cannot run because of the environment (credentials, private registries, unreachable services), record it in blocked_checks and keep implementing. Do not build workaround environments.{(chr(10) + "- The design gate is enforced, not advisory: hard-coded colours outside token files, non-design-system fonts, gradient text and forbidden terms fail verification and block delivery. Run it before every report and fix every error it lists:" + chr(10) + "  " + gate_cmd) if gate_cmd else ""}
- Do not commit; leave changes in the working tree. Never push, merge or deploy.
- Every reply must end with exactly one fenced ```json envelope: a "report" (status complete | partial | blocked) or a "question".

{("USER GUIDANCE" + chr(10) + guidance + chr(10)) if guidance else ""}
WORK PACKAGE #1 FROM SUPERVISOR
{instruction}
"""


def worker_followup(sup_label, turn, instruction, guidance, kind="instruction") -> str:
    head = {"instruction": f"MESSAGE FROM SUPERVISOR ({sup_label}) · work package #{turn}",
            "revise": f"MESSAGE FROM SUPERVISOR ({sup_label}) · revision request · work package #{turn}",
            "answer": f"ANSWER FROM SUPERVISOR ({sup_label})"}.get(kind, f"MESSAGE FROM SUPERVISOR ({sup_label})")
    parts = [head, instruction.strip()]
    if guidance:
        parts += ["", "USER GUIDANCE (new)", guidance]
    parts += ["", "Do the work, verify it locally, and end with a report or question envelope."]
    return "\n".join(parts)


def reviewer_kickoff(task, wt, branch, plan_env, pr_summary, verification, diff_text, round_no, cfg=None, blocked=None) -> str:
    cfg = cfg or {}
    return f"""You are the INDEPENDENT REVIEWER in a multi-agent engineering team run by Relay.

{team_intro(task, wt, branch)}

{task_block(task)}

PLAN
{plan_env.get('plan','')}

{packet_block(plan_env)}

CHECKS THAT COULD NOT RUN
{blocked_block(blocked or [])}

SUPERVISOR'S COMPLETION SUMMARY
{pr_summary or '(none provided)'}

ORCHESTRATOR VERIFICATION
{verification or '(no verification commands)'}

DIFF (may be truncated — inspect the repository yourself)
{diff_text}

{rules_block(rule_names_for("reviewer", cfg, task.get("requirements", "")))}

HOW THIS SESSION WORKS
- This session is persistent; on later rounds you receive the new state and can check whether earlier findings were fixed.
- Compare the implementation against the requirements and acceptance criteria. Inspect the diff and the code around it, the verification results and the checks that could not run. Do not modify files.
- You are a gate, not a second designer: request corrections only for concrete defects (a missed requirement or acceptance criterion, a regression, a bug, a security problem). Optional items, style preferences and alternative designs are never blocking.
- Review round {round_no}. Reply with exactly one fenced ```json envelope of type "review" with verdict PASS or FAIL and concrete findings.
"""


def reviewer_followup(round_no, verification, diff_text, sup_note) -> str:
    return f"""RE-REVIEW REQUEST · round {round_no}
The supervisor and worker addressed your previous findings.

Supervisor note:
{sup_note or '(none)'}

ORCHESTRATOR VERIFICATION
{verification or '(no verification commands)'}

DIFF (may be truncated — inspect the repository yourself)
{diff_text}

Check each earlier finding and look for regressions. Reply with a "review" envelope (PASS or FAIL).
"""


def human_answer(question, answer, extra=None) -> str:
    return (f"ANSWER FROM HUMAN\nYour question: {question}\nAnswer: {answer or '(no text — proceed with your best judgment)'}\n\n"
            "Continue with the task and end your reply with the appropriate protocol envelope.")


def guidance_only(guidance) -> str:
    return f"USER GUIDANCE\n{guidance}\n\nIncorporate this into your work and end your reply with the appropriate protocol envelope."


def nudge(role) -> str:
    expect = {"supervisor": 'a "plan", "instruction", "decision" or "question" envelope',
              "worker": 'a "report" or "question" envelope',
              "reviewer": 'a "review" envelope with verdict PASS or FAIL'}.get(role, "a protocol envelope")
    return (f"ORCHESTRATOR · your last reply did not end with a valid JSON envelope. Do not redo the work. "
            f"Reply now with ONLY {expect} inside a fenced ```json block, reflecting what you already did.")


def interrupted_note(note) -> str:
    return ("ORCHESTRATOR · the human interrupted your previous turn"
            + (f" with this instruction:\n{note}\n\n" if note else ".\n\n")
            + "Some of your earlier work in that turn may be incomplete. Check the working tree, incorporate the instruction, continue, "
              "and end with the appropriate protocol envelope.")


def resume_note(checkpoint) -> str:
    return ("ORCHESTRATOR · the orchestrator restarted and this run is being resumed from a checkpoint.\n"
            f"Last known state: {json.dumps(checkpoint, ensure_ascii=False)[:2000]}\n"
            "Re-inspect the working tree before continuing. End with the appropriate protocol envelope.")


def timeout_note(minutes) -> str:
    return (f"ORCHESTRATOR · your previous turn was terminated after {minutes} minutes without finishing. "
            "Summarize the state of the working tree and continue in smaller steps. End with the appropriate protocol envelope.")


# ----------------------------------------------------------------------------- lessons (orchestrator/lessons.py)
def with_lessons(prompt: str, block: str) -> str:
    """Add approved lessons from earlier tasks to a kickoff prompt, just before the session instructions."""
    if not block:
        return prompt
    at = prompt.find("HOW THIS SESSION WORKS")
    if at < 0:
        return prompt.rstrip() + "\n\n" + block + "\n"
    return prompt[:at] + block + "\n\n" + prompt[at:]


def pr_body(cfg, task, summary, details, issue_number=None) -> str:
    issue_close = f"Closes #{issue_number}" if issue_number else ""
    tpl = cfg.get("github_pr_body_template") or "{summary}\n\n{details}\n\n{issue_close}\n"
    try:
        return tpl.format(summary=summary or task.get("name", ""), details=details or "", issue_close=issue_close)
    except Exception:
        return f"{summary}\n\n{details}\n\n{issue_close}\n"
