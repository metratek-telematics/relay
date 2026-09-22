"""Task triage: how much process a task gets.

Relay's full team (supervisor plans and judges, worker builds, reviewer gates, design step, design research,
exploration with a focus group) pays for itself on large or risky work and costs 3 to 10 times the time on a small
one. Triage decides, at intake and again when the run starts, which of those phases a task actually needs:

    mode         solo (one agent plans, builds and proves; Relay verifies) or team
    design       the reviewed system design before code
    research     design research before a UI design
    exploration  mockup directions and a persona panel before building
    review       an independent check before delivery

Every decision carries its reason, shown on the task page, so "why did this take 40 minutes" has an answer.

Inputs: a keyword/shape heuristic over the request (instant), and for borderline requests a cheap one-shot
rating from the supervisor's agent on its cheapest model (about 10 to 30 seconds, no tools, no repository access).
Team mode per task: auto (default) | solo | team.
"""
from __future__ import annotations

import json
import re

MODES = ("auto", "solo", "team")
LEVELS = ("simple", "moderate", "complex")

# ----------------------------------------------------------------------------- vocabulary
_COMPLEX = [
    (r"\barchitect", 2), (r"\bmigrat", 2), (r"\bnew (micro)?service\b", 2), (r"\bfrom scratch\b", 2), (r"\brewrite\b", 2),
    (r"\bmillions?\b", 2), (r"\bacross (all|every|the whole)\b", 1), (r"\bend[- ]to[- ]end\b", 1), (r"\bmulti[- ]?(repo|tenant|region)", 2),
    (r"\bintegrat(e|ion)\b", 1), (r"\bsync(hroni[sz]e)?\b", 1), (r"\bworkflow engine\b", 2), (r"\bscalab", 1),
    (r"\bbackend and frontend\b|\bfrontend and backend\b", 2), (r"\bdata ?model\b", 1), (r"\bschema\b", 1),
    (r"\bperforman|\bbottleneck|\bstutter|\blag(s|gy)?\b|\bslow\b", 1), (r"\bsecurity\b|\bvulnerab", 1),
    (r"\boverhaul\b|\brevamp\b", 1), (r"\bwhole (app|application|system|site)\b", 2),
    (r"\bscan\b|\binvestigat|\baudit\b|\bdebug\b|\breliab|\bholes\b", 1),
]
_SIMPLE = [
    (r"\btypo\b", 2), (r"\brename\b", 1), (r"\blabel\b", 1), (r"\btooltip\b", 1), (r"\bcolou?r\b", 1), (r"\bwording\b|\btext\b", 1),
    (r"\bhide\b|\bshow\b|\bdisable\b|\benable\b", 1), (r"\badd (an?|the) (column|line|button|field|filter|option|icon|link|toggle)\b", 2),
    (r"\baverage line\b|\breference line\b", 1), (r"\bsmall\b|\bminor\b|\btweak\b|\bslightly\b|\bquick\b", 1),
    (r"\bone[- ]line\b|\bsingle\b", 1), (r"\bbump\b|\bupgrade (the )?version\b", 1),
]
_RISK = [
    (r"\bauth(entication|orization)?\b|\blog ?in\b|\bpassword\b|\bsecret\b|\btoken\b|\bpermission|\brbac\b|\broles?\b", "auth"),
    (r"\bpayment|\bbilling\b|\binvoice|\bcredit card\b", "payments"),
    (r"\bmigration\b|\balter table\b|\bdrop (table|column)\b|\bschema change\b", "data migration"),
    (r"\bdelete (all|user|customer|data)|\bpurge\b|\bwipe\b", "data deletion"),
    (r"\bproduction\b|\bprod\b|\bdeploy", "production"),
    (r"\bencrypt|\bpii\b|\bgdpr\b|\bpersonal data\b", "personal data"),
]
_CONTRACTS = r"\bapi\b|\bendpoint|\bschema\b|\bdatabase\b|\bmigration\b|\bwebhook|\bnew service\b|\bintegrat|\bprotocol\b|\bgraphql\b|\bpostgrest\b"
_REDESIGN = (r"\bre-?design|\bnew design\b|\bfresh (design|look|idea|new)|\bnew (look|idea|concept|style|visual)|\bcomplete new idea\b|"
             r"\boverhaul\b|\brestyle\b|\brevamp\b|\bmoderni[sz]e\b|\blook and feel\b|\bnew layout\b|\bnew (ui|screen|page|dashboard)\b|"
             r"\bmock-?ups?\b|\bdesign (options|directions|alternatives)\b|\bshow me (options|designs|mockups)\b")
_NOT_VISUAL = (r"\bvisually\b[^.]{0,40}\b(same|identical|unchanged)\b|\b(look|looks|looking)\b[^.]{0,30}\b(same|identical|unchanged)\b|"
               r"\bidentical(ly)?\b[^.]{0,30}\b(look|visual)|\bno visual change|\bkeep the (look|design|ui)\b|\bdo(n'?t| not) change the (look|design|ui)\b")
_SMALL_UI = r"\bminimal(ly)?\b|\bslight(ly)?\b|\bsmall\b|\btweak\b|\bminor\b"
_SCREEN = r"\b(page|screen|dashboard|home|layout|view|panel|app)\b"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _hits(rows, text):
    """(matched words, weight) for each pattern that matches."""
    out = []
    for pat, w in rows:
        m = re.search(pat, text)
        if m:
            out.append((m.group(0).strip(), w))
    return out


# ----------------------------------------------------------------------------- the heuristic
def heuristic(task: dict, cfg: dict | None = None) -> dict:
    """Instant assessment from the request text, template, repositories and attachments."""
    text = _norm(" ".join(str(task.get(k) or "") for k in ("name", "requirements", "issue_text")))
    words = len(text.split())
    template = (task.get("template") or "feature").lower()
    signals = []
    score = 0.0
    cx = _hits(_COMPLEX, text)
    sm = _hits(_SIMPLE, text)
    for pat, w in cx:
        score += w
        signals.append(f"+{w} “{pat}”")
    for pat, w in sm:
        score -= w
        signals.append(f"-{w} “{pat}”")
    if words > 150:
        score += 2
        signals.append(f"+2 long request ({words} words)")
    elif words > 70:
        score += 1
        signals.append(f"+1 detailed request ({words} words)")
    elif words < 25:
        score -= 1
        signals.append(f"-1 short request ({words} words)")
    if template in ("docs", "tests", "bugfix"):
        score -= 1
        signals.append(f"-1 {template} template")
    repos = [r for r in task.get("repos") or [] if r]
    multi = len(repos) > 0
    if multi:
        score += 3
        signals.append(f"+3 {len(repos) + 1} repositories")
    if task.get("attachments"):
        signals.append(f"{len(task['attachments'])} attachment(s)")
    risk = sorted({name for pat, name in _RISK if re.search(pat, text)})
    not_visual = bool(re.search(_NOT_VISUAL, text))
    redesign = bool(re.search(_REDESIGN, text)) and not not_visual
    small_ui = bool(re.search(_SMALL_UI, text)) and not re.search(r"\bcomplete\b|\bentirely\b|\bwhole\b|\bnew idea\b", text)
    ui_redesign = redesign and not small_ui and template not in ("bugfix", "tests", "docs", "review")
    contracts = bool(re.search(_CONTRACTS, text))
    level = "simple" if score < 0 else ("moderate" if score < 4 else "complex")
    if ui_redesign and level == "simple":
        level = "moderate"   # a new look is never a one-line change
    if multi:
        level = "complex"
    # How sure the heuristic is: far from a boundary, or a structural fact (several repositories).
    # A short request without a telling word is not "confidently simple": the owner writes short requests for big work too.
    confident = multi or score >= 5 or score <= -2 or (score <= -1 and bool(sm))
    return {"level": level, "score": round(score, 1), "confident": confident, "signals": signals[:14], "risk": risk,
            "ui_redesign": ui_redesign, "not_visual": not_visual, "contracts": contracts, "multi_repo": multi,
            "screen": bool(re.search(_SCREEN, text)), "words": words, "template": template}


# ----------------------------------------------------------------------------- the cheap rating
RATING_PROMPT = """You triage coding tasks for an autonomous engineering system. Rate the task below. Do not use tools.

TASK (template: {template}; repository: {repo})
{text}

Reply with only one JSON object:
{{"level":"simple|moderate|complex","reason":"one short line","needs_team":true|false,"risk":["auth"|"payments"|"data migration"|"data deletion"|"production"|"personal data"],"ui_redesign":true|false}}

simple: a local change a competent engineer finishes in under 30 minutes (one component, a few files).
moderate: a feature or fix in one repository touching several files or a few components; still one engineer's afternoon.
complex: several services or repositories, new contracts between components, data migrations, or open-ended work
  that needs a plan agreed before anyone codes.
needs_team: true only when a separate planner and reviewer clearly pay for themselves (complex or risky work).
ui_redesign: true only when the owner asks for a new look or a redesign of a whole screen, not a tweak to one element.
"""


def parse_rating(text: str) -> dict | None:
    m = None
    for m in re.finditer(r"\{[^{}]*\}", text or "", re.S):
        pass
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return None
    level = str(raw.get("level") or "").lower().strip()
    if level not in LEVELS:
        return None
    risk = [str(r).lower().strip() for r in (raw.get("risk") or []) if str(r).strip()][:6] if isinstance(raw.get("risk"), list) else []
    return {"level": level, "reason": str(raw.get("reason") or "")[:240], "needs_team": bool(raw.get("needs_team")),
            "risk": risk, "ui_redesign": bool(raw.get("ui_redesign"))}


def cheap_rating(task: dict, cfg: dict, workdir, timeout: float = 90.0) -> dict | None:
    """One tool-less turn on the supervisor agent's cheapest model. None when it could not run."""
    from . import retro
    try:
        agent, model, effort = retro.choose_agent(task, {**cfg, "retro_agent": cfg.get("triage_agent") or "",
                                                        "retro_model": cfg.get("triage_model") or "", "retro_effort": ""})
        if not agent:
            return None
        text = RATING_PROMPT.format(template=task.get("template") or "feature", repo=str(task.get("github_repo") or task.get("repo") or ""),
                                    text=(task.get("requirements") or "")[:4000])
        res = retro.run_agent(agent, model, effort, text, cfg, workdir, timeout, provider=retro.retro_provider(task, cfg),
                              task_id=task.get("id") or "")
        if not res.get("ok", True) and not res.get("text"):
            return None
        out = parse_rating(res.get("text") or res.get("last_message") or "")
        if out:
            out.update(agent=agent, model=model or "", seconds=res.get("seconds"))
        return out
    except Exception:
        return None


# ----------------------------------------------------------------------------- decisions
def team_mode(task: dict, cfg: dict) -> str:
    m = str((task.get("workflow") or {}).get("team_mode") or cfg.get("team_mode") or "auto").lower()
    return m if m in MODES else "auto"


def decide(task: dict, cfg: dict, h: dict, rating: dict | None = None) -> dict:
    """Combine the heuristic and the optional rating into the mode and the phases the task runs."""
    mode_setting = team_mode(task, cfg)
    level = h["level"]
    why_level = f"heuristic score {h['score']}"
    risk = list(h["risk"])
    ui_redesign = h["ui_redesign"]
    if rating:
        # The rating settles borderline cases; structural facts (several repositories) still win.
        level = "complex" if h["multi_repo"] else rating["level"]
        why_level = f"{rating.get('agent', 'agent')} rating: {rating.get('reason') or rating['level']}"
        risk = sorted(set(risk) | set(rating.get("risk") or []))
        ui_redesign = (ui_redesign or rating.get("ui_redesign")) and not h["not_visual"]
    if mode_setting == "solo":
        mode, why = "solo", "Team mode is set to Solo for this task"
    elif mode_setting == "team":
        mode, why = "team", "Team mode is set to Team for this task"
    elif h["multi_repo"]:
        mode, why = "team", "the task changes several repositories"
    elif level == "complex":
        mode, why = "team", f"rated complex ({why_level})"
    elif risk:
        mode, why = "team", f"risky area: {', '.join(risk)}"
    elif rating and rating.get("needs_team") and level != "simple":
        mode, why = "team", f"the rating asks for a team ({why_level})"
    elif ui_redesign and level != "simple":
        mode, why = "team", "a redesign: explore directions before building"
    else:
        mode, why = "solo", f"{level} single-repository task ({why_level})"
    return {"mode": mode, "mode_setting": mode_setting, "why": why, "level": level, "risk": risk,
            "ui_redesign": bool(ui_redesign), "contracts": h["contracts"], "not_visual": h["not_visual"],
            "heuristic": {k: h[k] for k in ("level", "score", "confident", "signals", "words")},
            "rating": rating or None}


def design_wanted(tri: dict, design_mode: str, complexity: dict | None, multi_repo: bool) -> tuple[bool, str]:
    """The reviewed design step: complex, multi-repository, or a moderate change to contracts between components."""
    level = (complexity or {}).get("level") or tri.get("level") or ""
    if design_mode == "never":
        return False, "design first is off for this task"
    if design_mode == "always":
        return True, "design first is always on for this task"
    if multi_repo:
        return True, "the task changes several repositories"
    if level == "complex":
        return True, "rated complex" + (f": {complexity.get('reason')}" if (complexity or {}).get("reason") else "")
    if level == "moderate" and tri.get("contracts"):
        return True, "a moderate change to contracts between components (API, schema, integration)"
    return False, (f"rated {level}" if level else "no design trigger") + ": a reviewed system design would cost more than it saves"


def research_wanted(tri: dict) -> tuple[bool, str]:
    if tri.get("not_visual"):
        return False, "the request keeps the look unchanged"
    if tri.get("ui_redesign"):
        return True, "the request asks for a new look or a redesign"
    return False, "not a redesign (a small UI change or no UI change)"


def exploration_wanted(tri: dict, show_mockups: bool, setting: str) -> tuple[bool, str]:
    if show_mockups:
        return True, "the task asks to see mockups before building"
    if setting == "off":
        return False, "design exploration is off in settings"
    if tri.get("mode") == "solo":
        return False, "solo mode: one agent builds directly"
    if tri.get("not_visual"):
        return False, "the request keeps the look unchanged"
    if not tri.get("ui_redesign"):
        return False, "not a redesign of a screen (exploration is for a new look, not a tweak)"
    if tri.get("level") == "simple":
        return False, "rated simple: one direction is enough"
    return True, "the request asks for a new look or a redesign"


SENSITIVE_PATHS = re.compile(r"(^|/)(migrations?|alembic|db/|sql/)|\.sql$|(^|/)(auth|security|permissions?|rbac)[^/]*(/|\.)|"
                             r"(^|/)\.github/workflows/|(^|/)Dockerfile|docker-compose|(^|/)\.env|secrets?", re.I)


def check_wanted(tri: dict, diffstat: dict | None, changed: list | None, unproven: int = 0, blocked: int = 0) -> tuple[bool, list[str]]:
    """A light independent check for a solo run: only when something about the result warrants a second look."""
    reasons = []
    if tri.get("risk"):
        reasons.append("risky area: " + ", ".join(tri["risk"]))
    ds = diffstat or {}
    lines = int(ds.get("insertions") or 0) + int(ds.get("deletions") or 0)
    files = int(ds.get("files") or len(changed or []))
    if files > 15 or lines > 900:
        reasons.append(f"a large change ({files} files, {lines} lines)")
    paths = [c.get("path") if isinstance(c, dict) else str(c) for c in changed or []]
    hot = [p for p in paths if p and SENSITIVE_PATHS.search(p)]
    if hot:
        reasons.append("sensitive files: " + ", ".join(hot[:4]))
    if int(ds.get("deletions") or 0) > 300 and int(ds.get("deletions") or 0) > 2 * int(ds.get("insertions") or 0):
        reasons.append("mostly deletions")
    if unproven:
        reasons.append(f"{unproven} acceptance criteria not proven")
    return bool(reasons), reasons


def review_wanted(tri: dict, diffstat: dict | None, changed: list | None) -> tuple[bool, str]:
    """Team mode in auto: the independent reviewer runs for complex or risky work, or a large change."""
    if tri.get("mode_setting") == "team":
        return True, "Team mode is set to Team: the full team reviews"
    if tri.get("level") == "complex":
        return True, "rated complex"
    want, reasons = check_wanted(tri, diffstat, changed)
    if want:
        return True, "; ".join(reasons)
    return False, f"rated {tri.get('level') or 'moderate'} with no risk signals: the supervisor's judgement and Relay's checks are the gate"
