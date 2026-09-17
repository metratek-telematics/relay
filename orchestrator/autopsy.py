"""Failure autopsy: why a run failed or scored low, and one concrete change that would prevent it.

Root causes (the first that scores highest wins; ties go in this order)

    environment       the code could not be run or checked: a missing system library or module, a command
                      that does not exist (exit 126/127), a failed dependency install, a suite that never ran
    requirements      the team did not know what was wanted: humans had to answer questions or steer,
                      delivery was rejected, the retrospective names unclear scope
    flaky_checks      the same check passed and failed on and off during the run
    protocol          agents broke the turn protocol (missing envelopes, no plan)
    infra             agent crashes, timeouts, rate limits, Relay errors, git or GitHub failures
    agent_capability  the team had what it needed and still could not get there: budget used up,
                      reviewer kept blocking, verification kept failing on the code, many revisions

Each cause scores points from evidence found in the scorecard, the task record, the message log and
command output; `confidence` is the winner's share of all points. Proposals are concrete and can be
applied with one click (orchestrator/learning_engine.py `apply_proposal`):

    system_packages   add apt packages to the repository environment        (environment)
    lesson            file an approved lesson with a category               (requirements, environment, …)
    rule_tweak        append one line to a rules/*.md file                   (protocol)
    optional_check    mark a flaky check optional for the repository         (flaky_checks)
    setting           raise a limit such as the agent turn timeout            (infra)
    team_default      pin a better team for the repository (added by the engine, which has the rankings)

Pure: no I/O.
"""
from __future__ import annotations

import re

from . import syspkgs

CAUSES = ("environment", "requirements", "flaky_checks", "protocol", "infra", "agent_capability")
CAUSE_LABEL = {"environment": "Environment", "requirements": "Requirements", "flaky_checks": "Flaky checks",
               "protocol": "Protocol", "infra": "Infrastructure", "agent_capability": "Agent capability"}

_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '([A-Za-z0-9_.]+)'")
_NODE_MODULE = re.compile(r"Cannot find module '([^']+)'")
_NOT_FOUND = re.compile(r"(?:^|\s)([A-Za-z0-9_.-]+): (?:command )?not found", re.M)
_REQ_WORDS = re.compile(r"requirement|unclear|ambigu|misunderst|scope|expectation|what the (?:user|owner) wanted|clarif", re.I)
_LIB_LOOSE = re.compile(r"\b(lib[A-Za-z0-9_+-]+\.so(?:\.[0-9]+)*)\b(?=[^\n]{0,40}?\b(?:missing|not found|not installed|not available|unresolved|could not be loaded)\b)|"
                        r"\b(?:missing|unresolved)\s+(lib[A-Za-z0-9_+-]+\.so(?:\.[0-9]+)*)", re.I)
_RATE = re.compile(r"rate.?limit|usage limit|quota|too many requests|\b429\b|overloaded", re.I)


def libraries(text: str) -> list[dict]:
    """Missing shared libraries: the loader's own message, or a retrospective or report saying one is missing."""
    libs = syspkgs.missing_libraries(text)
    seen = {x["library"] for x in libs}
    for m in _LIB_LOOSE.finditer(text or ""):
        lib = m.group(1) or m.group(2)
        if lib and lib not in seen:
            seen.add(lib)
            libs.append({"library": lib, "package": next((p for prefix, p in syspkgs.LIBRARY_PACKAGES.items() if lib.startswith(prefix)), "")})
    return libs


def unproven_criteria(task: dict | None) -> list[dict]:
    """Required acceptance criteria that were neither met nor waived when the run ended."""
    return [c for c in (task or {}).get("acceptance") or [] if c.get("required", True) and c.get("status") not in ("met", "waived")]


def needs_autopsy(card: dict, threshold: int = 60, task: dict | None = None) -> bool:
    """Failed, stopped after real work, scored low, or delivered blind: a blocked check or an unproven required criterion."""
    if not card:
        return False
    if card.get("outcome") in ("delivered_pr", "done_no_pr") and (int(card.get("blocked_checks") or 0) or unproven_criteria(task)):
        return True
    if card.get("outcome") == "failed":
        return True
    if card.get("outcome") == "stopped":
        # A task a person stopped straight away tells us nothing; one stopped after real work usually does.
        return int(card.get("total_turns") or 0) >= 2
    return int(card.get("score") or 0) < threshold or int(card.get("blocked_action_required") or 0) > 0


def evidence_text(task: dict, messages: list[dict], extra: str = "") -> str:
    """Everything worth searching for environment and infra symptoms, newest last."""
    parts = [str(task.get("error") or "")]
    parts += [f"{b.get('check')}: {b.get('reason')}" for b in task.get("blocked_checks") or []]
    env = task.get("environment") or {}
    if env.get("output"):
        parts.append(str(env["output"]))
    for m in messages or []:
        if m.get("kind") == "command" and (m.get("status") == "error" or (m.get("rc") not in (None, 0))):
            parts.append(f"$ {m.get('content')}\n{str(m.get('output') or '')[-3000:]}")
        elif m.get("kind") == "error":
            parts.append(str(m.get("content") or "")[:1500])
    parts.append(extra or "")
    return "\n".join(p for p in parts if p)


def _flaky(messages: list[dict]) -> list[str]:
    seq = {}
    for m in messages or []:
        if m.get("kind") != "verification":
            continue
        for i in m.get("items") or []:
            if i.get("kind") in ("e2e", "design"):
                continue
            seq.setdefault(i.get("command"), []).append(bool(i.get("passed", i.get("ok"))))
    out = []
    for cmd, s in seq.items():
        flips = sum(1 for a, b in zip(s, s[1:]) if a != b)
        # pass → fail → pass (or the reverse) during one run
        if flips >= 2 and cmd:
            out.append(cmd)
    return out


def classify(card: dict, task: dict, messages: list[dict], text: str = "", retro: dict | None = None) -> dict:
    task = task or {}
    retro = retro or task.get("retro") or {}
    blob = text or evidence_text(task, messages)
    pts = {c: 0.0 for c in CAUSES}
    ev = {c: [] for c in CAUSES}

    def hit(cause, points, why):
        pts[cause] += points
        ev[cause].append(why)

    cat = card.get("failure_category")
    h = card.get("human") or {}
    tr = card.get("agent_trouble") or {}

    # environment
    libs = libraries(blob)
    if libs:
        hit("environment", 4, "missing system librar" + ("ies " if len(libs) > 1 else "y ") + ", ".join(x["library"] for x in libs))
    mods = sorted(set(_MODULE.findall(blob)))
    if mods:
        hit("environment", 3, "missing module(s) " + ", ".join(mods[:4]))
    node_mods = sorted({m for m in _NODE_MODULE.findall(blob) if not m.startswith(".")})
    if node_mods:
        hit("environment", 2.5, "missing Node module(s) " + ", ".join(node_mods[:4]))
    cmds = sorted({c for c in _NOT_FOUND.findall(blob) if c not in ("line", "bash", "sh")})
    if cmds:
        hit("environment", 2.5, "command(s) not installed: " + ", ".join(cmds[:4]))
    blocked = task.get("blocked_checks") or []
    env_blocked = [b for b in blocked if re.search(r"environment|system package|did not run|could not run|setup|services", f"{b.get('check')} {b.get('reason')} {b.get('impact')}", re.I)]
    if env_blocked:
        hit("environment", 1.5 + 0.5 * min(3, len(env_blocked)), f"{len(env_blocked)} check(s) blocked by the environment")
    unproven = unproven_criteria(task)
    if unproven and (libs or mods or node_mods or cmds or env_blocked):
        hit("environment", 1, f"{len(unproven)} required criteria delivered unproven while the environment blocked checks")
    elif unproven:
        hit("agent_capability", 1.5, f"{len(unproven)} required criteria delivered unproven")
    if card.get("failure_category") == "setup":
        hit("environment", 2, "worktree or environment setup failed")
    if (task.get("environment") or {}).get("ok") is False:
        hit("environment", 2, "dependency install failed before the agents started")

    # requirements
    if h.get("questions"):
        hit("requirements", 1.0 * min(3, h["questions"]), f"{h['questions']} question(s) a human had to answer")
    if h.get("guidance") or h.get("interrupts"):
        n = (h.get("guidance") or 0) + (h.get("interrupts") or 0)
        hit("requirements", 1.2 * min(3, n), f"{n} guidance message(s) or interrupt(s) to steer the team")
    if h.get("rejections"):
        hit("requirements", 2.5, f"delivery rejected {h['rejections']} time(s)")
    retro_text = " ".join(str(x) for x in (retro.get("root_causes") or []) + (retro.get("what_went_wrong") or []))
    if _REQ_WORDS.search(retro_text):
        hit("requirements", 2, "the retrospective names unclear requirements or scope")
    unknowns = len((task.get("plan") or {}).get("unknowns") or [])
    if unknowns >= 3:
        hit("requirements", 1, f"the plan listed {unknowns} unknowns")
    if card.get("outcome") == "stopped" and (h.get("guidance") or h.get("questions")):
        hit("requirements", 1, "a person stopped the task after steering it")

    # flaky checks
    flaky = _flaky(messages)
    if flaky:
        hit("flaky_checks", 3 + len(flaky), "passed and failed on and off: " + ", ".join(flaky[:3]))

    # protocol
    if cat == "protocol":
        hit("protocol", 4, "the run failed on the protocol")
    if tr.get("envelope_nudges", 0) >= 2:
        hit("protocol", 1.0 * min(4, tr["envelope_nudges"]), f"{tr['envelope_nudges']} missing protocol envelope(s)")

    # infra
    if cat in ("agent_timeout", "agent_error", "relay_bug", "delivery", "configuration"):
        hit("infra", 4, f"failure category {cat}")
    if tr.get("timeouts"):
        hit("infra", 1.5 * min(3, tr["timeouts"]), f"{tr['timeouts']} agent turn timeout(s)")
    if tr.get("failures", 0) >= 2:
        hit("infra", 1.0 * min(4, tr["failures"]), f"{tr['failures']} failed agent turn(s)")
    if _RATE.search(blob):
        hit("infra", 2, "rate or usage limits in the output")

    # agent capability
    if cat in ("turn_budget", "review_rejected", "verification"):
        hit("agent_capability", 2.5, f"failure category {cat}")
    if int(card.get("revisions") or 0) >= 3:
        hit("agent_capability", 0.5 * min(6, card["revisions"]), f"{card['revisions']} revision requests")
    if int(card.get("review_rounds") or 0) >= 3:
        hit("agent_capability", 1, f"{card['review_rounds']} review rounds")
    v = card.get("verification") or {}
    if v.get("final_ok") is False and not pts["environment"]:
        hit("agent_capability", 1.5, "the last verification failed on the code")

    total = sum(pts.values())
    if total <= 0:
        cause = "requirements" if card.get("outcome") == "stopped" else "agent_capability"
        return {"cause": cause, "label": CAUSE_LABEL[cause], "confidence": 0.3, "points": pts,
                "evidence": ["no specific symptom found; judged from the outcome alone"], "proposals": base_proposals(cause, card, task, {}),
                "facts": {}}
    order = {c: i for i, c in enumerate(CAUSES)}
    cause = max(CAUSES, key=lambda c: (pts[c], -order[c]))
    facts = {"libraries": libs, "modules": mods, "node_modules": node_mods, "commands": cmds, "flaky": flaky}
    return {"cause": cause, "label": CAUSE_LABEL[cause], "confidence": round(pts[cause] / total, 2),
            "points": {k: round(v, 1) for k, v in pts.items() if v}, "evidence": ev[cause][:6],
            "other_evidence": {c: ev[c][:3] for c in CAUSES if c != cause and ev[c]},
            "proposals": base_proposals(cause, card, task, facts), "facts": facts}


def base_proposals(cause: str, card: dict, task: dict, facts: dict) -> list[dict]:
    repo, label, path = card.get("repo") or "", card.get("repo_label") or "", card.get("repo_path") or task.get("repo") or ""
    out = []
    if cause == "environment":
        pkgs = sorted({x["package"] for x in facts.get("libraries") or [] if x.get("package")})
        if pkgs and path:
            out.append({"kind": "system_packages", "title": f"Install {' '.join(pkgs)} for {label or 'this repository'}",
                        "detail": "Missing shared librar" + ("ies" if len(facts["libraries"]) > 1 else "y") + ": "
                                  + ", ".join(x["library"] for x in facts["libraries"]) + ". Relay installs the packages before every task on this repository.",
                        "repo": repo, "repo_path": path, "packages": pkgs})
        for mod in (facts.get("modules") or [])[:2]:
            top = mod.split(".")[0]
            out.append({"kind": "lesson", "title": f"Lesson: install the Python module {top} before testing",
                        "detail": f"Tests could not import {mod}.", "repo": repo, "scope": "repo", "category": "environment",
                        "text": f"Tests import {top}; make sure it is declared in the project's dependencies and installed in the virtualenv before running the suite."})
        for mod in (facts.get("node_modules") or [])[:1]:
            out.append({"kind": "lesson", "title": f"Lesson: the Node module {mod} must be installed", "repo": repo, "scope": "repo", "category": "environment",
                        "detail": f"Node could not resolve {mod}.",
                        "text": f"The code needs the Node module {mod}; add it to package.json and run the package install before building or testing."})
        for c in (facts.get("commands") or [])[:1]:
            out.append({"kind": "lesson", "title": f"Lesson: {c} is not installed where checks run", "repo": repo, "scope": "repo", "category": "environment",
                        "detail": f"'{c}' was not found.",
                        "text": f"'{c}' is not available in Relay's environment; use the project's own scripts or add it under Repositories → Environment instead of calling it directly."})
        if not out:
            out.append({"kind": "lesson", "title": "Lesson: prove the environment works before implementing", "repo": repo, "scope": "repo",
                        "category": "environment", "detail": "Checks were blocked by the environment.",
                        "text": "Run the repository's checks once before changing code; if they cannot run, report the blocker in blocked_checks straight away instead of working blind."})
    elif cause == "requirements":
        out.append({"kind": "lesson", "title": "Lesson: ask before planning when the request leaves the outcome open", "repo": repo, "scope": "repo",
                    "category": "communication", "detail": "Humans had to steer or answer questions during the run.",
                    "text": "When the request does not say how success is observed or what is out of scope, ask one clarifying question before planning instead of guessing."})
        out.append({"kind": "setting", "title": "Always run the pre-flight check and ask clarifying questions for thin requests",
                    "detail": "The wizard asks the questions before the task is queued.", "path": ["learning", "risk_check"], "value": True})
    elif cause == "flaky_checks":
        for cmd in (facts.get("flaky") or [])[:2]:
            out.append({"kind": "optional_check", "title": f"Treat `{cmd}` as optional on {label or 'this repository'}",
                        "detail": "It passed and failed on and off in one run, so it cannot gate delivery reliably. It still runs and is reported.",
                        "repo": repo, "repo_path": path, "command": cmd})
    elif cause == "protocol":
        out.append({"kind": "rule_tweak", "title": "Rule: end every turn with the protocol envelope",
                    "detail": "Agents repeatedly ended turns without the JSON envelope.", "rule": "PROTOCOL",
                    "text": "Before you stop, re-read your reply: it must end with exactly one fenced json envelope of the type your role expects, even when you are blocked."})
    elif cause == "infra":
        tr = card.get("agent_trouble") or {}
        if tr.get("timeouts") or card.get("failure_category") == "agent_timeout":
            out.append({"kind": "setting", "title": "Allow agent turns 30 minutes longer", "detail": "Turns timed out.",
                        "path": ["agent_turn_timeout_minutes"], "delta": 30})
        out.append({"kind": "setting", "title": "Retry agent crashes and hangs once more automatically", "detail": "Infrastructure failures are retried from the checkpoint.",
                    "path": ["autopilot", "retry_infra_failures"], "delta": 1})
    elif cause == "agent_capability":
        out.append({"kind": "lesson", "title": "Lesson: smaller work packages with a check each", "repo": repo, "scope": "repo", "category": "architecture",
                    "detail": "The team needed many rounds to converge.",
                    "text": "Split the work into packages small enough to verify one at a time, and run the relevant tests after each package instead of at the end."})
    return out
