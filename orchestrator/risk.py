"""Pre-flight risk check: how likely a new task is to fail or score low, why, and what would help.

    p_fail = logistic( logit(base) + Σ factor weights )

base
    The similarity-weighted failure rate of past runs (orchestrator/recommend.py weights; a run
    "failed" when it was not a success or scored under 60), shrunk towards the global failure rate
    with K = 4, and towards 0.25 before there is any history.

factors (each has an id, a weight in log-odds, a sentence of evidence and mitigations)
    requirements_thin     short request with no issue                              +1.0
    requirements_vague    vague wording ("somehow", "improve", "etc" …)             +0.3 each, max +0.9
    requirements_history  earlier runs on this repository failed on requirements     +0.5
    environment_history   environment root causes on this repository not yet fixed   +0.7
    large                 large request (long, many bullet points or several repos)  +0.4
    multi_repo            more than one repository                                   +0.3
    no_reviewer           medium or large task without an independent reviewer        +0.2
    weak_team             the chosen team's expected score here is under 60           +0.5
    team_unproven         the chosen team has never run on this repository            +0.15

level   low < 0.25 ≤ medium < 0.5 ≤ high

The prediction is stored on the task (`preflight`) and in its outcome record, so `calibration`
can later compare predicted with actual failure rates per level and compute a Brier score.
"""
from __future__ import annotations

import math

from .outcomes import failed, request_signals
from .recommend import K, weight

LEVELS = (("low", 0.25), ("medium", 0.5), ("high", 1.01))
PRIOR_FAIL = 0.25


def _logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def level(p: float) -> str:
    return next(name for name, hi in LEVELS if p < hi)


def base_rate(records: list[dict], ctx: dict) -> dict:
    n = len(records)
    g = (sum(1 for r in records if failed(r)) / n) if n else PRIOR_FAIL
    g = (g * n + PRIOR_FAIL * K) / (n + K)
    ws = [(r, weight(r, ctx)) for r in records]
    sw = sum(w for _, w in ws)
    p = (K * g + sum(w for r, w in ws if failed(r))) / (K + sw)
    similar = [r for r, w in ws if w >= 1.25]
    return {"p": p, "global": g, "evidence": round(sw, 2), "similar": len(similar),
            "similar_failed": sum(1 for r in similar if failed(r))}


CLARIFY = {
    "bugfix": ["What are the exact steps to reproduce it, and what do you expect instead of what happens?",
               "Where does it show up (screen, endpoint, job, log line)?"],
    "feature": ["How will you check it works: what should someone see or be able to do when it is done?",
                "Which screens, endpoints or files are in scope, and what is explicitly out of scope?"],
    "refactor": ["What must stay behaviourally identical, and which tests or checks prove it?",
                 "What should the structure look like afterwards?"],
    "tests": ["Which code paths or failure modes matter most?"],
    "docs": ["Who is the audience, and which pages or files should change?"],
    "review": ["What should the review focus on, and what form should the report take?"],
}


def questions(template: str, sig: dict) -> list[str]:
    out = list(CLARIFY.get(template or "feature", CLARIFY["feature"]))
    for term in (sig.get("vague_terms") or [])[:2]:
        out.insert(0, f"You wrote “{term}”: what concretely counts as done?")
    return out[:3]


def assess(ctx: dict, records: list[dict], team_eval: dict | None = None, repo_autopsies: list[dict] | None = None) -> dict:
    """ctx: repo, repo_label, template, requirements, issue, repos (count), roles (the draft team)."""
    sig = request_signals(ctx.get("requirements") or "", ctx.get("issue") or "", int(ctx.get("repos") or 1))
    ctx = {**ctx, "size": sig["size"]}
    base = base_rate(records, ctx)
    factors = []

    def add(fid, w, text, mitigations):
        factors.append({"id": fid, "weight": round(w, 2), "text": text, "mitigations": mitigations})

    if sig["chars"] < 120 and not sig["has_issue"]:
        add("requirements_thin", 1.0, f"The request is short ({sig['chars']} characters) and has no issue behind it.",
            [{"id": "clarify", "label": "Answer a few clarifying questions", "questions": questions(ctx.get("template"), sig)}])
    if sig["vague_terms"]:
        add("requirements_vague", min(0.9, 0.3 * len(sig["vague_terms"])), "Vague wording: " + ", ".join(f"“{t}”" for t in sig["vague_terms"]) + ".",
            [{"id": "clarify", "label": "Say what concretely counts as done", "questions": questions(ctx.get("template"), sig)}])
    autopsies = repo_autopsies or []
    req_hist = [a for a in autopsies if a.get("cause") == "requirements"]
    if req_hist:
        add("requirements_history", 0.5, f"{len(req_hist)} earlier run(s) on this repository went wrong on unclear requirements.",
            [{"id": "clarify", "label": "Clarify before starting", "questions": questions(ctx.get("template"), sig)}])
    env_open = [a for a in autopsies if a.get("cause") == "environment" and not a.get("fixed")]
    if env_open:
        add("environment_history", 0.7, f"{len(env_open)} earlier run(s) here were held back by the environment and no fix has been applied yet.",
            [{"id": "fix_environment", "label": "Apply the environment fix proposed in Learning", "href": "#/learning"}])
    if sig["size"] == "large":
        add("large", 0.4, "Large request" + (f" ({sig['bullets']} points)" if sig["bullets"] >= 8 else f" ({sig['chars']} characters)" if sig["chars"] > 1500 else "") + ".",
            [{"id": "split", "label": "Split it into smaller tasks that depend on each other"},
             {"id": "design", "label": "Let the supervisor design before implementing"}])
    if sig["repos"] > 1:
        add("multi_repo", 0.3, f"Changes {sig['repos']} repositories.",
            [{"id": "stack", "label": "Attach an integration stack so the services are proven together", "href": "#/repos/stacks"}])
    roles = ctx.get("roles") or {}
    if sig["size"] != "small" and not (roles.get("reviewer") or {}).get("agent"):
        add("no_reviewer", 0.2, "No independent reviewer for a task of this size.",
            [{"id": "add_reviewer", "label": "Add an independent reviewer"}])
    if team_eval:
        if team_eval.get("confidence", 0) >= 1 and team_eval.get("expected_score", 100) < 60:
            add("weak_team", 0.5, f"This team's expected score here is {team_eval['expected_score']:.0f}.",
                [{"id": "use_recommended", "label": "Use the recommended team"}])
        elif not team_eval.get("runs_on_repo") and records and any(r.get("repo") == ctx.get("repo") for r in records):
            add("team_unproven", 0.15, "This team has not worked on this repository before.",
                [{"id": "use_recommended", "label": "Use the recommended team"}])

    x = _logit(base["p"]) + sum(f["weight"] for f in factors)
    p = _sigmoid(x)
    mitigations = []
    for f in sorted(factors, key=lambda f: -f["weight"]):
        for m in f["mitigations"]:
            if m["id"] not in {x["id"] for x in mitigations}:
                mitigations.append({**m, "because": f["id"]})
    base_text = (f"{base['similar_failed']} of {base['similar']} similar runs failed or scored under 60" if base["similar"]
                 else f"{len(records)} recorded runs, none closely similar" if records else "no history yet")
    return {"level": level(p), "p_fail": round(p, 3), "base": round(base["p"], 3), "base_text": base_text,
            "factors": factors, "mitigations": mitigations, "signals": sig,
            "questions": next((m["questions"] for m in mitigations if m["id"] == "clarify"), [])}


# ----------------------------------------------------------------------------- calibration
def calibration(records: list[dict]) -> dict:
    """Predicted vs actual failure per risk level, and the Brier score of the predictions."""
    rows = [r for r in records if (r.get("prediction") or {}).get("p_fail") is not None]
    out = {"predictions": len(rows), "levels": [], "brier": None, "baseline_brier": None}
    if not rows:
        return out
    ys = [1.0 if failed(r) else 0.0 for r in rows]
    ps = [float(r["prediction"]["p_fail"]) for r in rows]
    out["brier"] = round(sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(rows), 3)
    mean_y = sum(ys) / len(ys)
    out["baseline_brier"] = round(sum((mean_y - y) ** 2 for y in ys) / len(ys), 3)  # always predicting the average
    for name, _ in LEVELS:
        sel = [(p, y) for p, y, r in zip(ps, ys, rows) if (r["prediction"].get("level") or level(p)) == name]
        if sel:
            out["levels"].append({"level": name, "n": len(sel), "predicted": round(sum(p for p, _ in sel) / len(sel), 3),
                                  "actual": round(sum(y for _, y in sel) / len(sel), 3)})
    followed = [r for r in rows if r["prediction"].get("followed") is not None]
    if followed:
        yes = [r for r in followed if r["prediction"]["followed"]]
        no = [r for r in followed if not r["prediction"]["followed"]]
        avg = lambda xs: round(sum(x.get("score") or 0 for x in xs) / len(xs), 1) if xs else None
        out["recommendation"] = {"followed": len(yes), "followed_avg": avg(yes), "ignored": len(no), "ignored_avg": avg(no)}
    return out
