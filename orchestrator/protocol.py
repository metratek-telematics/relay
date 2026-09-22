"""Inter-agent protocol: envelope parsing and prompt construction."""
from __future__ import annotations

import json
import re
import time

from . import config as C
from . import judge, tokens
from .util import RULES_DIR, read_text, truncate

_rules_cache = {"at": 0, "data": {}}

RULE_FILES = ["CORE", "PROTOCOL", "SUPERVISOR", "WORKER", "REVIEW", "PLANNER", "ENGINEERING", "FRONTEND",
              "DESIGN", "DESIGN_RESEARCH", "TESTING", "SECURITY", "GIT_GITHUB", "PERFORMANCE_RELIABILITY", "DATA_API", "DOCUMENTATION"]


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
    "supervisor": ["CORE", "SUPERVISOR", "PROTOCOL", "PLANNER", "ENGINEERING", "FRONTEND", "DESIGN", "DESIGN_RESEARCH", "TESTING",
                   "SECURITY", "GIT_GITHUB", "PERFORMANCE_RELIABILITY", "DATA_API", "DOCUMENTATION"],
    "worker": ["CORE", "WORKER", "PROTOCOL", "ENGINEERING", "FRONTEND", "DESIGN", "DESIGN_RESEARCH", "TESTING", "SECURITY",
               "GIT_GITHUB", "PERFORMANCE_RELIABILITY", "DATA_API", "DOCUMENTATION"],
    "reviewer": ["CORE", "REVIEW", "PROTOCOL", "ENGINEERING", "FRONTEND", "DESIGN", "TESTING", "SECURITY", "DATA_API"],
}
# Extra rule files pulled in only when the task/repository actually touches that area.
CONTEXTUAL = {
    "FRONTEND": ("frontend", "ui", "css", "overhaul", "look and feel", "react", "vue", "svelte", "component", "page", "style", "html", "tsx", "jsx",
                 "layout", "design", "screen", "dashboard", "modal", "button", "form", "table", "responsive", "theme"),
    "DESIGN": ("frontend", "ui", "css", "overhaul", "look and feel", "react", "vue", "svelte", "component", "page", "style", "html", "tsx", "jsx",
               "layout", "design", "screen", "dashboard", "modal", "button", "form", "table", "responsive", "theme"),
    "DESIGN_RESEARCH": ("redesign", "new design", "fresh design", "new layout", "layout", "look and feel", "overhaul", "visual",
                        "restyle", "modernize", "modernise", "ui design", "ux", "new idea", "symbols", "icons", "theme", "design"),
    "TESTING": ("test", "spec", "coverage", "pytest", "jest", "vitest"),
    "SECURITY": ("auth", "token", "password", "secret", "permission", "crypt", "login", "session", "sql"),
    "DATA_API": ("api", "endpoint", "schema", "database", "migration", "sql", "query", "rest", "graphql"),
    "GIT_GITHUB": ("commit", "branch", "pull request", "merge", "rebase"),
    "DOCUMENTATION": ("doc", "readme", "changelog", "guide"),
    "PERFORMANCE_RELIABILITY": ("performance", "slow", "latency", "memory", "leak", "cache", "concurren", "race", "lag", "smooth",
                                "fps", "frame rate", "jank", "stutter", "freez", "cpu", "optimi", "sluggish", "lighthouse"),
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
        if key == "acceptance" and plan.get("criteria"):
            lines += ["ACCEPTANCE CONTRACT (ids are referenced by revisions and the done verdict; required ones must be met with evidence)",
                      judge.acceptance_block(plan["criteria"], with_status=False), ""]
        elif plan.get(key):
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


def _session_block(lines) -> str:
    return "HOW THIS SESSION WORKS\n" + "\n".join(lines)


# Kickoff prompts put what is identical across tasks first (role, rules, how the session works) and what changes
# last (task, context, the first job), so provider prompt caches that match on a prefix can reuse it; see
# docs/TOKEN_EFFICIENCY.md. Context blocks (lessons, repositories, system map) go in front of the environment.
SUPERVISOR_SESSION = _session_block([
    "- This session is persistent. Every future message you receive is from the worker, the reviewer, the human, or the orchestrator. You remember everything, so never repeat the plan back; just act on the new message.",
    "- Use your tools freely to inspect the repository, read files, run tests and view diffs. Do NOT implement the task yourself.",
    "- Every reply must end with exactly one fenced ```json envelope as defined in the protocol.",
])


def supervisor_kickoff(task, wt, branch, issue_text, refs_text, guidance, verify_cmds, cfg=None, env_text="", tools_text=""):
    cfg = cfg or {}
    rules_text = rules_block(rule_names_for("supervisor", cfg, task.get("requirements", "") + " " + (issue_text or "")))
    verify = ("VERIFICATION COMMANDS THE ORCHESTRATOR WILL RUN AFTER EACH WORKER REPORT\n"
              + ("\n".join("- " + c for c in verify_cmds) if verify_cmds else
                 "(none detected — ask the worker to add or name the right checks if the repository has any)"))
    job = """YOUR FIRST JOB
Inspect the repository enough to plan: the files the request touches, existing local changes, build/test setup. Then reply with the "plan" envelope. It is the team's shared context packet, so the worker does not repeat your discovery:
- requirements: the user's request restated as short items, nothing added;
- acceptance: the acceptance contract, 2 to 6 checkable criteria derived from those requirements only, each
  {"id":"A1","criterion":"observable outcome","how_to_verify":"test: python -m pytest tests/test_x.py | command: … | screenshot: … | inspection: …","required":true}.
  Done is gated on these: every required criterion needs status met with concrete evidence, so write criteria you can prove;
- optional: improvements you would suggest but the user did not ask for (they never block done);
- known_files (primary, supporting), findings (file + what you saw), constraints, unknowns;
- summary, a short markdown plan listing the work packages in order, and the first work package as instruction.
- complexity: {"level":"simple|moderate|complex","reason":"one line"}. complex: several services or repositories, new contracts
  between components, data migrations, or a new user flow across layers; simple: a local change in one place. Rate honestly: the
  assessment decides whether a reviewed system design comes before implementation.
Rule files describe how to work; never copy their checklists into requirements or acceptance.
Keep the instruction short: name the concern, the files, the expected result. The packet already carries the context."""
    return tokens.build([
        ("rules", "You are the SUPERVISOR in a multi-agent engineering team run by Relay.\n\n" + rules_text),
        ("protocol", SUPERVISOR_SESSION),
        ("tools", tools_text),
        ("task", team_intro(task, wt, branch) + "\n\n" + task_block(task, issue_text, refs_text)),
        ("environment", ("ENVIRONMENT PREPARED BY RELAY\n" + env_text + "\n\n" if env_text else "") + verify),
        ("guidance", ("USER GUIDANCE RECEIVED SO FAR\n" + guidance) if guidance else ""),
        ("instruction", job),
    ], insert_before="environment")


def supervisor_after_report(worker_label, turn, report_env, report_text, verification, changed, diffstat, guidance, remaining, cfg=None,
                            judge_text="", brief=False):
    cfg = cfg or {}
    max_report = int(cfg.get("budget_report_chars") or 14000)
    max_files = int(cfg.get("budget_file_list") or 80)
    status = (report_env or {}).get("status", "unknown")
    files = (report_env or {}).get("files") or []
    body = (report_env or {}).get("report") or report_text
    lines = [
        f"REPORT FROM WORKER ({worker_label}) · work package #{turn} · status: {status}",
        f"Summary: {truncate((report_env or {}).get('summary', ''), 400)}",
        "",
        truncate(body, max_report),
    ]
    listed = {c["path"] for c in changed}
    extra_files = [f for f in files if str(f) not in listed] if changed else files
    if extra_files:
        # Files git status already shows below are not listed twice.
        shown = extra_files[:max_files]
        lines += ["", "Files the worker says it changed" + (" (not in git status)" if changed else "") + ":", *[f"- {f}" for f in shown]]
        if len(extra_files) > len(shown):
            lines.append(f"- …and {len(extra_files) - len(shown)} more")
    checks = blocked_checks(report_env or {})
    stops = blockers(report_env or {})
    if checks:
        lines += ["", "Checks the worker could not run", blocked_block(checks)]
    if stops:
        lines += ["", "Blockers the worker reported", *[f"- {b['reason']}" + (f" · impact: {b['impact']}" if b['impact'] else "")
                                                         + (" · needs the user" if b['action_required'] else "") for b in stops]]
    git = [f"ORCHESTRATOR · git status ({diffstat.get('files',0)} files, +{diffstat.get('insertions',0)} / -{diffstat.get('deletions',0)})"]
    shown_changed = changed[:max_files]
    git += [f"- {c['status']} {c['path']}" for c in shown_changed] or ["(clean working tree)"]
    if len(changed) > len(shown_changed):
        git.append(f"- …and {len(changed) - len(shown_changed)} more (run git status yourself)")
    if brief:
        reply = ["Inspect what this package changed. Reply with ONE envelope as before: revise (blocking defects only, with addresses), "
                 "the next instruction, done (criteria with evidence), or a question. should_fix and nit items go in follow_ups."]
    else:
        reply = ["Inspect the diff and the files this package changed (not the whole repository again). Judge against the acceptance contract and the evidence only; a failure matching a recorded blocked check is not the worker's defect.",
                 "Then reply with ONE envelope:",
                 '- {"type":"decision","decision":"revise","addresses":["A2","F1"],"findings":[{"severity":"blocking","file":"path:line","problem":"…","fix":"…","criterion":"A2"}],...} only for blocking defects, or',
                 '- {"type":"instruction",...} with the next planned work package, or',
                 '- {"type":"decision","decision":"done","criteria":[{"id":"A1","status":"met","evidence":"`python -m pytest -q` → 12 passed"}],"follow_ups":[…],"pr_summary":"…"} when every required criterion is met with evidence, or',
                 '- {"type":"question",...} if you are genuinely blocked.',
                 "should_fix and nit items never justify a revision: list them in follow_ups and move on."]
    return tokens.build([
        ("report", "\n".join(lines)),
        ("verification", "ORCHESTRATOR · verification results\n" + (verification or "(verification not run at this point)")),
        ("report", "\n".join(git)),
        ("guidance", ("USER GUIDANCE (new)\n" + guidance) if guidance else ""),
        ("judge", judge_text),
        ("instruction", f"ORCHESTRATOR · {remaining} work package(s) remain before the turn budget is exhausted.\n\n" + "\n".join(reply)),
    ])


def supervisor_after_review(reviewer_label, review_env, review_text, round_no, max_rounds, followups=None, guidance=""):
    verdict = (review_env or {}).get("verdict", "FAIL")
    findings = (review_env or {}).get("findings") or []
    lines = [f"REVIEW FROM REVIEWER ({reviewer_label}) · round {round_no}/{max_rounds} · verdict: {verdict}",
             f"Summary: {(review_env or {}).get('summary','')}", ""]
    if findings:
        lines.append("BLOCKING FINDINGS")
        for i, f in enumerate(findings, 1):
            lines.append(f"{f.get('id') or i}. [{f.get('severity','blocking')}] {f.get('file','')}: {f.get('problem','')}"
                         + (f" · criterion {f['criterion']}" if f.get("criterion") else "") + f"\n   Fix: {f.get('fix','')}"
                         + (f"\n   Fix attempts so far: {f['attempts']}" if f.get("attempts") else ""))
    else:
        lines.append(truncate(review_text, 8000))
    if followups:
        lines += ["", f"{len(followups)} should_fix/nit item(s) were recorded as follow-ups for the pull request. Do not revise for them."]
    tail = ["Verify each blocking finding against the repository. If it is real, reply with ONE",
            '{"type":"decision","decision":"revise","addresses":["F1"],...} envelope whose instruction fixes all of them at once.',
            "If a finding is wrong (not a defect against the acceptance contract, or already fixed), reply with a done decision whose",
            "criteria evidence shows why; the reviewer re-checks it."]
    return tokens.build([("report", "\n".join(lines)), ("guidance", ("HUMAN GUIDANCE ON THESE FINDINGS\n" + guidance) if guidance else ""),
                         ("instruction", "\n".join(tail))])


def supervisor_worker_question(worker_label, question) -> str:
    return (f"QUESTION FROM WORKER ({worker_label})\n{question}\n\n"
            "Answer precisely from the repository and the plan. Reply with an {\"type\":\"instruction\",...} envelope "
            "containing your answer and how to proceed, or escalate with a question to the user if this needs a human decision.")


WORKER_SESSION = _session_block([
    "- This session is persistent. Future messages are work packages, answers, or guidance. You remember everything.",
    "- Treat the context packet as prior repository inspection. Verify the files and assumptions your package depends on; do not repeat broad discovery unless the packet is missing, contradictory or stale.",
    "- Implement only the concern in the current work package. Run fast checks focused on what you changed; the orchestrator runs the full verification.",
    "- In your report, give evidence for the acceptance criteria your package touches (criterion id, the command you ran and its result, or file:line). Claims without output do not count.",
    "- If a check cannot run because of the environment (credentials, private registries, unreachable services), record it in blocked_checks and keep implementing. Do not build workaround environments.",
    "- Do not commit; leave changes in the working tree. Never push, merge or deploy.",
    '- Every reply must end with exactly one fenced ```json envelope: a "report" (status complete | partial | blocked) or a "question".',
])


def worker_kickoff(task, wt, branch, issue_text, refs_text, plan_env, instruction, guidance, cfg=None, gate_cmd="", env_text="", tools_text=""):
    cfg = cfg or {}
    rules_text = rules_block(rule_names_for("worker", cfg, task.get("requirements", "") + " " + str(plan_env.get("plan", ""))))
    gate = ("DESIGN GATE\nThe design gate is enforced, not advisory: hard-coded colours outside token files, non-design-system fonts, gradient text "
            "and forbidden terms fail verification and block delivery. Run it before every report and fix every error it lists:\n  " + gate_cmd) if gate_cmd else ""
    return tokens.build([
        ("rules", "You are the WORKER (implementation engineer) in a multi-agent engineering team run by Relay.\n\n" + rules_text),
        ("protocol", WORKER_SESSION),
        ("tools", tools_text),
        ("task", team_intro(task, wt, branch) + "\n\n" + task_block(task, issue_text, refs_text)),
        ("packet", "PLAN AGREED BY THE SUPERVISOR\n" + str(plan_env.get("plan", "")) + "\n\nCONTEXT PACKET FROM THE SUPERVISOR\n" + packet_block(plan_env)),
        ("environment", ("ENVIRONMENT PREPARED BY RELAY\n" + env_text) if env_text else ""),
        ("environment", gate),
        ("guidance", ("USER GUIDANCE\n" + guidance) if guidance else ""),
        ("instruction", "WORK PACKAGE #1 FROM SUPERVISOR\n" + str(instruction or "")),
    ], insert_before="packet")


def worker_followup(sup_label, turn, instruction, guidance, kind="instruction"):
    head = {"instruction": f"MESSAGE FROM SUPERVISOR ({sup_label}) · work package #{turn}",
            "revise": f"MESSAGE FROM SUPERVISOR ({sup_label}) · revision request · work package #{turn}",
            "answer": f"ANSWER FROM SUPERVISOR ({sup_label})"}.get(kind, f"MESSAGE FROM SUPERVISOR ({sup_label})")
    return tokens.build([("instruction", head + "\n" + str(instruction or "").strip()),
                         ("guidance", ("USER GUIDANCE (new)\n" + guidance) if guidance else ""),
                         ("instruction", "Do the work, verify it locally, and end with a report or question envelope.")])


REVIEWER_SESSION = _session_block([
    "- This session is persistent; on later rounds you receive the new state and can check whether earlier findings were fixed.",
    "- Compare the implementation against the requirements and acceptance criteria. Inspect the diff and the code around it, the verification results and the checks that could not run. Do not modify files.",
    "- The diff you receive is targeted: a diffstat, whole hunks for the files that fit, and the git command for the rest. Fetch only the files you need.",
    "- You are a gate, not a second designer: request corrections only for concrete defects (a missed requirement or acceptance criterion, a regression, a bug, a security problem). Optional items, style preferences and alternative designs are never blocking.",
    "- Check the evidence behind each required criterion yourself; an unproven or false claim is a blocking finding naming the criterion id.",
    "- Classify every finding: blocking (wrong behaviour against the contract, failing required check, security issue, data loss, broken build), should_fix or nit. Only blocking findings return the work; the others become pull-request follow-ups. FAIL requires at least one blocking finding.",
    '- Reply with exactly one fenced ```json envelope of type "review" with verdict PASS or FAIL and concrete findings.',
])


def reviewer_kickoff(task, wt, branch, plan_env, pr_summary, verification, diff_text, round_no, cfg=None, blocked=None, contract="", tools_text=""):
    cfg = cfg or {}
    rules_text = rules_block(rule_names_for("reviewer", cfg, task.get("requirements", "")))
    return tokens.build([
        ("rules", "You are the INDEPENDENT REVIEWER in a multi-agent engineering team run by Relay.\n\n" + rules_text),
        ("protocol", REVIEWER_SESSION),
        ("tools", tools_text),
        ("task", team_intro(task, wt, branch) + "\n\n" + task_block(task)),
        ("packet", "PLAN\n" + str(plan_env.get("plan", "")) + "\n\n" + packet_block(plan_env)
         + "\n\nCHECKS THAT COULD NOT RUN\n" + blocked_block(blocked or [])),
        ("judge", ("ACCEPTANCE CONTRACT WITH THE SUPERVISOR'S VERDICTS\n" + contract) if contract else ""),
        ("judge", "SUPERVISOR'S COMPLETION SUMMARY\n" + (pr_summary or "(none provided)")),
        ("verification", "ORCHESTRATOR VERIFICATION\n" + (verification or "(no verification commands)")),
        ("diff", "DIFF (targeted — inspect the repository yourself for more)\n" + str(diff_text or "")),
        ("instruction", f"Review round {round_no}. Reply with the review envelope."),
    ], insert_before="packet")


def reviewer_followup(round_no, verification, diff_text, sup_note, contract="", open_findings=""):
    return tokens.build([
        ("instruction", f"RE-REVIEW REQUEST · round {round_no}\nThe supervisor and worker addressed your previous findings.\n\nSupervisor note:\n{sup_note or '(none)'}"),
        ("judge", ("ACCEPTANCE CONTRACT WITH THE SUPERVISOR'S VERDICTS\n" + contract) if contract else ""),
        ("judge", ("YOUR EARLIER BLOCKING FINDINGS (check each; keep the same wording if one persists)\n" + open_findings) if open_findings else ""),
        ("verification", "ORCHESTRATOR VERIFICATION\n" + (verification or "(no verification commands)")),
        ("diff", "DIFF (targeted — inspect the repository yourself for more)\n" + str(diff_text or "")),
        ("instruction", "Check each earlier finding and look for regressions caused by the fix. Do not raise new requirements the contract does not contain;\n"
                        'new non-blocking observations are should_fix or nit. Reply with a "review" envelope (PASS or FAIL).'),
    ])


ROLE_HEADER = {
    "supervisor": "You are the SUPERVISOR in a multi-agent engineering team run by Relay.",
    "worker": "You are the WORKER (implementation engineer) in a multi-agent engineering team run by Relay.",
    "reviewer": "You are the INDEPENDENT REVIEWER in a multi-agent engineering team run by Relay.",
}


def resume_kickoff(role, task, wt, branch, cfg, handoff, message, env_text="", tools_text="", gate_cmd=""):
    """A fresh session for a role that already worked on this task: same stable prefix as its kickoff, then Relay's handoff."""
    session = {"supervisor": SUPERVISOR_SESSION, "worker": WORKER_SESSION, "reviewer": REVIEWER_SESSION}.get(role, SUPERVISOR_SESSION)
    rules_text = rules_block(rule_names_for(role, cfg or {}, task.get("requirements", "")))
    return tokens.build([
        ("rules", ROLE_HEADER.get(role, ROLE_HEADER["supervisor"]) + "\n\n" + rules_text),
        ("protocol", session),
        ("tools", tools_text),
        ("task", team_intro(task, wt, branch) + "\n\n" + task_block(task)),
        ("environment", ("ENVIRONMENT PREPARED BY RELAY\n" + env_text) if env_text else ""),
        ("environment", ("DESIGN GATE (enforced; run before every report)\n  " + gate_cmd) if gate_cmd else ""),
        ("packet", handoff),
        ("instruction", "CURRENT MESSAGE\n" + str(message or "")),
    ], insert_before="packet")


def human_answer(question, answer, extra=None) -> str:
    return (f"ANSWER FROM HUMAN\nYour question: {question}\nAnswer: {answer or '(no text — proceed with your best judgment)'}\n\n"
            "Continue with the task and end your reply with the appropriate protocol envelope.")


def guidance_only(guidance) -> str:
    return f"USER GUIDANCE\n{guidance}\n\nIncorporate this into your work and end your reply with the appropriate protocol envelope."


def nudge(role, types=None) -> str:
    if types and "design" in types:
        return ("ORCHESTRATOR · your last reply did not end with a valid JSON envelope. Do not redo the inspection. "
                'Reply now with ONLY the {"type":"design",...} envelope inside a fenced ```json block, reflecting the design you worked out.')
    if types and "mockups" in types:
        return ("ORCHESTRATOR · your last reply did not end with a valid JSON envelope. Do not redraw anything. Reply now with ONLY "
                '{"type":"mockups","summary":"…","directions":[{"id":"A","title":"…","idea":"…","optimises_for":"…","differs_by":"…"}]} inside a fenced ```json block.')
    if types and "focus_review" in types:
        return ("ORCHESTRATOR · your last reply did not end with a valid JSON envelope. Do not start over. Reply now with ONLY the "
                '{"type":"focus_review",…} envelope (scores 1 to 10 per criterion and direction, preferred, borrow) inside a fenced ```json block.')
    if types and "review" in types and role != "reviewer":
        role = "reviewer"
    expect = {"supervisor": 'a "plan", "instruction", "decision" or "question" envelope',
              "worker": 'a "report" or "question" envelope',
              "reviewer": 'a "review" envelope with verdict PASS or FAIL'}.get(role, "a protocol envelope")
    return (f"ORCHESTRATOR · your last reply did not end with a valid JSON envelope. Do not redo the work. "
            f"Reply now with ONLY {expect} inside a fenced ```json block, reflecting what you already did.")


def done_gate_nudge(missing, contract, attempt, limit) -> str:
    rows = "\n".join(f"- {m['id']}: {m['criterion']} · {m['reason']}" for m in missing)
    return (f"ORCHESTRATOR · done was NOT accepted (attempt {attempt}/{limit}). The acceptance contract gates delivery and these required "
            f"criteria are not proven:\n{rows}\n\nCURRENT CONTRACT\n{contract}\n\n"
            "Evidence must be concrete: the command and its result (`python -m pytest tests/test_x.py -q` → 5 passed), a file:line you "
            "read, or a screenshot path. Relay's own failing verification cannot be overridden by a claim.\n"
            "Reply with ONE envelope: done again with a `criteria` list for every required id if you can now prove them; a revise "
            "addressing the unmet ids if work is missing; or a question to the user if a criterion should be waived or cannot be met.")


def revise_nudge(reason, contract, ledger_text) -> str:
    return (f"ORCHESTRATOR · this revision was not dispatched: {reason}.\n\n"
            "A revision must address unmet acceptance criteria or blocking findings, named in `addresses` (criterion ids like A2, "
            "finding ids like F1) or given as findings with severity blocking. Blocking means: wrong behaviour against the contract, "
            "a failing required check, a security issue, data loss or a broken build.\n"
            f"\nCURRENT CONTRACT\n{contract}\n\nOPEN BLOCKING FINDINGS\n{ledger_text}\n\n"
            "If nothing blocking remains, reply with a done decision (criteria with evidence) and put the rest in follow_ups. "
            "Otherwise resend the revise with `addresses`.")


def human_decision_note(summary) -> str:
    return (f"ORCHESTRATOR · the human decided: {summary}\n"
            "Act on it and reply with ONE envelope: the next instruction, a revise for anything still blocking, or done with criteria evidence.")


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
    """Add approved lessons from earlier tasks to a kickoff prompt, in front of its task-specific tail."""
    return tokens.insert_block(prompt, block, "lessons")


def with_block(prompt: str, block: str, section: str = "context") -> str:
    """Add a context block (repositories, system map, system design) to a kickoff prompt, in front of its task-specific tail."""
    return tokens.insert_block(prompt, block, section)


def pr_body(cfg, task, summary, details, issue_number=None) -> str:
    issue_close = f"Closes #{issue_number}" if issue_number else ""
    tpl = cfg.get("github_pr_body_template") or "{summary}\n\n{details}\n\n{issue_close}\n"
    try:
        return tpl.format(summary=summary or task.get("name", ""), details=details or "", issue_close=issue_close)
    except Exception:
        return f"{summary}\n\n{details}\n\n{issue_close}\n"
