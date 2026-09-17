"""Lessons that prove themselves: categories, effect on later tasks, and relevance-based selection.

Categories
    repo_conventions · testing · environment · architecture · communication. A retrospective or a
    person may set one; otherwise `categorize` picks by keywords (the category with most hits,
    repo_conventions when nothing matches).

Effect
    For an approved lesson, compare later runs in its scope (its repository, or every repository for a
    global lesson) that had the lesson in their prompts ("with") against runs in the same scope that did
    not ("without": before it was approved, or not selected). For each group: average score, verification
    first-pass rate and average revisions.

        delta = avg score with − avg score without
        verdict  unproven   fewer than N runs with it (N = `effect_min_tasks`, default 3) or none without
                 helps      delta ≥ +5
                 hurts      delta ≤ −5                      → flagged for retirement
                 no_effect  |delta| < 5 with ≥ 2N runs with → flagged for retirement

    Averages of small groups move a lot; the view shows n next to every number and never auto-retires.

Relevance selection
    Instead of every approved lesson, a task's prompts get the lessons that fit it:
        relevance = 1.0 same repository · +1.0 environment lesson of the repository (always worth knowing)
                    · +1.5 category suits the task type · +0.8 per word shared with the request (max 3)
                    · +1.0 per component or path mentioned in both (max 2) · +1.0 proven helpful · −3 flagged as hurting
    Lessons scoring under 1.5 are left out: a repository lesson needs one more reason than its repository,
    a global one needs a category that suits the task or real overlap with the request. The rest go in best
    first, up to the limit. Each chosen lesson carries its reason (`lessons_selection` on the task).

Support (auto-approval)
    When retrospectives on different tasks propose the same or nearly the same lesson (word overlap
    Jaccard ≥ 0.5), the queued lesson records each supporting task. With `auto_approve_lessons` on, a
    lesson supported by at least `auto_approve_min_tasks` tasks is approved automatically; by default
    it is only marked as high confidence.

Pure: no I/O.
"""
from __future__ import annotations

import re
from datetime import datetime

CATEGORIES = ("repo_conventions", "testing", "environment", "architecture", "communication")
CATEGORY_LABEL = {"repo_conventions": "Repository conventions", "testing": "Testing", "environment": "Environment",
                  "architecture": "Architecture", "communication": "Communication"}
_KEYWORDS = {
    "testing": r"\btest|pytest|jest|vitest|spec\b|coverage|assert|fixture|mock|regression|e2e|playwright|verif",
    "environment": r"install|dependenc|package|docker|container|\benv\b|\.env|environment|librar|apt\b|venv|virtualenv|node_modules|version of|path\b|database url|service",
    "architecture": r"module|layer|separate|refactor|interface|schema|api\b|endpoint|component|abstraction|coupl|architecture|structure|design",
    "communication": r"\bask|clarif|report|explain|state (?:the|in)|plan\b|evidence|summar|document|communicat|question|acceptance",
    "repo_conventions": r"convention|naming|style|lint|format|folder|directory|file name|commit|branch|use the existing|follow the",
}
TYPE_CATEGORIES = {
    "feature": {"architecture", "repo_conventions", "testing"},
    "bugfix": {"testing", "repo_conventions"},
    "refactor": {"architecture", "testing"},
    "tests": {"testing", "environment"},
    "docs": {"communication", "repo_conventions"},
    "review": {"communication", "architecture"},
}
_STOP = set("the a an and or of to in on for is are be it this that with not no do does should must when if as at by from but "
            "into only use using before after each every any all its their them they you your we our can will than then so "
            "task tasks agent agents code change changes file files make sure".split())


def categorize(text: str) -> str:
    low = (text or "").lower()
    hits = {c: len(re.findall(p, low)) for c, p in _KEYWORDS.items()}
    best = max(CATEGORIES, key=lambda c: (hits.get(c, 0), c == "repo_conventions"))
    return best if hits.get(best) else "repo_conventions"


def words(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9_]{2,}", (text or "").lower()) if w not in _STOP}


def paths(text: str) -> set:
    """Path-like or component-like tokens: src/api, utils.py, system-metrics.spec.js."""
    return {p.strip("./").lower() for p in re.findall(r"[\w.-]+(?:/[\w.-]+)+|[\w-]+\.(?:py|js|ts|vue|tsx|jsx|go|rs|java|kt|sql|md|json|yml|yaml)\b", text or "")}


def jaccard(a: str, b: str) -> float:
    x, y = words(a), words(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


# ----------------------------------------------------------------------------- effect
def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso)[:19])
    except (TypeError, ValueError):
        return None


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "avg_score": None, "first_pass": None, "avg_revisions": None, "success": None}
    fp = [r for r in rows if (r.get("verification") or {}).get("first_ok") is not None]
    return {"n": n, "avg_score": round(sum(r.get("score") or 0 for r in rows) / n, 1),
            "first_pass": round(sum(1 for r in fp if r["verification"]["first_ok"]) / len(fp), 2) if fp else None,
            "avg_revisions": round(sum((r.get("judge") or {}).get("revisions") or 0 for r in rows) / n, 2),
            "success": round(sum(1 for r in rows if r.get("success")) / n, 2)}


def effect(lesson: dict, records: list[dict], min_tasks: int = 3) -> dict:
    lid = lesson.get("id")
    scope_repo = lesson.get("repo") if lesson.get("scope") == "repo" else None
    # Runs whose injected lessons are unknown (rebuilt from a scorecard alone) cannot be put in either group.
    pool = [r for r in records if (not scope_repo or r.get("repo") == scope_repo) and r.get("lessons_known", True)]
    with_ = [r for r in pool if lid in (r.get("lessons_injected") or [])]
    without = [r for r in pool if lid not in (r.get("lessons_injected") or [])]
    w, wo = _stats(with_), _stats(without)
    out = {"lesson_id": lid, "with": w, "without": wo, "delta": None, "verdict": "unproven", "retire": False}
    if w["n"] and wo["n"]:
        out["delta"] = round(w["avg_score"] - wo["avg_score"], 1)
        if w["first_pass"] is not None and wo["first_pass"] is not None:
            out["first_pass_delta"] = round(w["first_pass"] - wo["first_pass"], 2)
        out["revisions_delta"] = round(w["avg_revisions"] - wo["avg_revisions"], 2)
    min_tasks = max(1, int(min_tasks or 3))
    if w["n"] >= min_tasks and wo["n"] and out["delta"] is not None:
        if out["delta"] >= 5:
            out["verdict"] = "helps"
        elif out["delta"] <= -5:
            out.update(verdict="hurts", retire=True)
        elif w["n"] >= 2 * min_tasks:
            out.update(verdict="no_effect", retire=True)
    return out


# ----------------------------------------------------------------------------- relevance
def relevance(lesson: dict, ctx: dict, eff: dict | None = None) -> tuple[float, list[str]]:
    score, why = 0.0, []
    cat = lesson.get("category") or categorize(lesson.get("text"))
    if lesson.get("scope") == "repo" and ctx.get("repo") and lesson.get("repo") == ctx["repo"]:
        score += 1.0
        why.append("this repository")
        if cat == "environment":
            score += 1.0
            why.append("environment")
    if cat in TYPE_CATEGORIES.get(ctx.get("template") or "feature", set()):
        score += 1.5
        why.append(f"{CATEGORY_LABEL[cat].lower()} suits a {ctx.get('template') or 'feature'}")
    req = ctx.get("requirements") or ""
    shared = words(lesson.get("text")) & words(req)
    if shared:
        score += 0.8 * min(3, len(shared))
        why.append("mentions " + ", ".join(sorted(shared)[:3]))
    lp = paths(lesson.get("text")) | {p.lower() for p in lesson.get("components") or []}
    tp = paths(req) | {p.lower() for p in ctx.get("components") or []}
    both = {p for p in lp if any(p in q or q in p for q in tp)}
    if both:
        score += 1.0 * min(2, len(both))
        why.append("same area: " + ", ".join(sorted(both)[:2]))
    if eff:
        if eff.get("verdict") == "helps":
            score += 1.0
            why.append("proven helpful")
        elif eff.get("verdict") == "hurts":
            score -= 3.0
            why.append("flagged as hurting")
    return round(score, 2), why


def select(lessons: list[dict], ctx: dict, limit: int = 15, effects: dict | None = None, threshold: float = 1.5) -> list[dict]:
    effects = effects or {}
    scored = []
    for x in lessons:
        if x.get("enabled", True) is False:
            continue
        s, why = relevance(x, ctx, effects.get(x.get("id")))
        if s >= threshold:
            scored.append({**x, "relevance": s, "relevance_why": why})
    scored.sort(key=lambda x: x.get("approved_at") or "", reverse=True)  # newest first among equals (stable sort below)
    scored.sort(key=lambda x: (-x["relevance"], x.get("scope") != "repo"))
    return scored[:max(0, int(limit))]


def support(queued: list[dict], text: str, task_id: str, threshold: float = 0.5) -> list[dict]:
    """Queued lessons from other tasks that say (nearly) the same thing."""
    return [q for q in queued if q.get("status") == "proposed" and q.get("task_id") != task_id and jaccard(q.get("text"), text) >= threshold]
