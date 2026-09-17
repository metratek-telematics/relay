"""Team recommendation: which supervisor / worker / reviewer to use for a new task, from what worked before.

The model is deliberately simple enough to explain on one line.

Similarity
    Every past run gets a weight for the new task:  0.25  (any task)
                                                  + 1.0   same repository
                                                  + 0.5   same task type
                                                  + 0.35  same request size (small / medium / large)
    so a refactor on the same repository weighs 2.1 and an unrelated task 0.25. A run whose autopsy blamed
    something outside the team (environment, infrastructure, unclear requirements) counts a quarter as
    much for the team: it says little about how well that team works.

Shrinkage (Bayesian average)
    expected score   = (K·μ + Σ wᵢ·scoreᵢ) / (K + Σ wᵢ)
    success chance   = (K·p + Σ wᵢ·successᵢ) / (K + Σ wᵢ)
    μ and p are the global average score and success rate over every recorded run (70 and 0.75 before
    there are any), K = 4. A team with no history on this kind of task sits at the global average, and
    one lucky run cannot outrank a team with a steady record. `confidence` = Σ wᵢ.

Cost
    The weighted median cost of the team's similar runs; with no runs of its own, the median of runs
    that used the same worker agent, else the global median (marked estimated).

Modes
    best                  highest expected score, less an uncertainty margin of 12/√(K+Σw)
    balanced              expected score − 8·log₂(cost / median cost of all runs)
    cheapest_good_enough  the cheapest team whose expected score is within 7 points of the best
                          (and at least 65) with a success chance of at least 0.6; else best

Exploration
    With probability ε (setting, default 10%) the auto-pick takes the least-tried of the next three
    candidates instead, so an alternative that has only ever been tried once gets a chance to prove
    itself. Never for tasks marked critical (priority urgent, or a `critical` tag).

Everything here is pure: the caller supplies outcome records (orchestrator/outcomes.py), the
candidate teams and an optional availability check.
"""
from __future__ import annotations

import math
import random
from statistics import median

from .outcomes import team_from_key, team_key, team_label

K = 4.0
EXTERNAL_CAUSES = {"environment", "infra", "requirements"}
PRIOR_SCORE = 70.0
PRIOR_SUCCESS = 0.75
MODES = ("best", "balanced", "cheapest_good_enough")
MODE_LABEL = {"best": "Best", "balanced": "Balanced", "cheapest_good_enough": "Cheapest good enough"}


def weight(rec: dict, ctx: dict) -> float:
    w = 0.25
    if ctx.get("repo") and rec.get("repo") == ctx["repo"]:
        w += 1.0
    if ctx.get("template") and rec.get("template") == ctx["template"]:
        w += 0.5
    if ctx.get("size") and ((rec.get("size") or {}).get("bucket")) == ctx["size"]:
        w += 0.35
    return w


def global_prior(records: list[dict]) -> tuple[float, float]:
    if not records:
        return PRIOR_SCORE, PRIOR_SUCCESS
    n = len(records)
    mu = sum(float(r.get("score") or 0) for r in records) / n
    p = sum(1 for r in records if r.get("success")) / n
    # A handful of runs should not move the prior all the way.
    blend = n / (n + K)
    return mu * blend + PRIOR_SCORE * (1 - blend), p * blend + PRIOR_SUCCESS * (1 - blend)


def weighted_median(pairs: list[tuple[float, float]]):
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def evaluate(team: str, records: list[dict], ctx: dict, prior=None) -> dict:
    """Posterior score, success chance, cost and the evidence behind them for one team key."""
    mu, p = prior or global_prior(records)
    mine = [r for r in records if r.get("team_key") == team]
    ws = [(r, weight(r, ctx) * (0.25 if r.get("autopsy_cause") in EXTERNAL_CAUSES else 1.0)) for r in mine]
    sw = sum(w for _, w in ws)
    score = (K * mu + sum(w * float(r.get("score") or 0) for r, w in ws)) / (K + sw)
    success = (K * p + sum(w * (1.0 if r.get("success") else 0.0) for r, w in ws)) / (K + sw)
    cost = weighted_median([(float(r.get("cost_usd") or 0), w) for r, w in ws if r.get("cost_usd")])
    cost_estimated = False
    if cost is None:
        worker = (team_from_key(team).get("worker") or {}).get("agent")
        same_worker = [float(r.get("cost_usd") or 0) for r in records if (r.get("team") or {}).get("worker", {}).get("agent") == worker and r.get("cost_usd")]
        pool = same_worker or [float(r.get("cost_usd") or 0) for r in records if r.get("cost_usd")]
        cost = median(pool) if pool else None
        cost_estimated = True
    on_repo = [r for r in mine if ctx.get("repo") and r.get("repo") == ctx["repo"]]
    same_type = [r for r in on_repo if r.get("template") == ctx.get("template")]
    external = [r for r in on_repo if r.get("autopsy_cause") in EXTERNAL_CAUSES]
    return {
        "team_key": team, "roles": team_from_key(team),
        "expected_score": round(score, 1), "success_chance": round(success, 3),
        "confidence": round(sw, 2), "runs": len(mine), "runs_on_repo": len(on_repo), "runs_same_type": len(same_type), "external_on_repo": len(external),
        "avg_score_on_repo": round(sum(r.get("score") or 0 for r in on_repo) / len(on_repo), 1) if on_repo else None,
        "avg_score": round(sum(r.get("score") or 0 for r in mine) / len(mine), 1) if mine else None,
        "median_cost": round(cost, 2) if cost is not None else None, "cost_estimated": cost_estimated,
        "lcb": round(score - 12.0 / math.sqrt(K + sw), 1),
    }


def explain(ev: dict, ctx: dict, agents: dict | None = None) -> str:
    """"Claude → Codex: 6 tasks on navitrak-vue, 83 avg, $3.10 median"."""
    label = team_label(ev["team_key"], agents)
    cost = f"${ev['median_cost']:.2f} median{' (estimated)' if ev['cost_estimated'] else ''}" if ev.get("median_cost") is not None else "no cost data"
    repo = ctx.get("repo_label") or ctx.get("repo") or "this repository"
    if ev["runs_on_repo"]:
        what = f"{ev['runs_on_repo']} task{'s' if ev['runs_on_repo'] != 1 else ''} on {repo}, {ev['avg_score_on_repo']:.0f} avg"
        if ev.get("external_on_repo"):
            what += f" ({ev['external_on_repo']} failed on the environment, infrastructure or requirements, counted ¼)"
    elif ev["runs"]:
        what = f"no tasks on {repo} yet; {ev['runs']} elsewhere, {ev['avg_score']:.0f} avg"
    else:
        what = "no history yet (global average assumed)"
    return f"{label}: {what}, {cost} · expected {ev['expected_score']:.0f}, {round(ev['success_chance'] * 100)}% success"


def _utility(ev: dict, mode: str, cost_ref: float) -> float:
    if mode == "best":
        return ev["lcb"]
    if mode == "balanced":
        c = ev["median_cost"] if ev["median_cost"] is not None else cost_ref
        return ev["expected_score"] - 8.0 * math.log2(max(c, 0.05) / max(cost_ref, 0.05))
    return ev["expected_score"]


def rank(records: list[dict], candidates: list[str], ctx: dict, mode: str = "balanced", available=None, agents=None,
         pinned: str | None = None) -> dict:
    """Evaluate candidate team keys and pick one for `mode`. `available(team_key)` filters unusable teams."""
    mode = mode if mode in MODES else "balanced"
    prior = global_prior(records)
    keys = []
    for k in list(candidates) + ([pinned] if pinned else []):
        if k and k not in keys:
            keys.append(k)
    evs = []
    for k in keys:
        ev = evaluate(k, records, ctx, prior)
        ev["available"] = True if available is None else bool(available(k))
        ev["pinned"] = bool(pinned and k == pinned)
        ev["why"] = explain(ev, ctx, agents)
        evs.append(ev)
    usable = [e for e in evs if e["available"]] or evs
    costs = [float(r["cost_usd"]) for r in records if r.get("cost_usd")]
    cost_ref = median(costs) if costs else 3.0
    picks = {}
    for m in MODES:
        picks[m] = _pick(usable, m, cost_ref)
    chosen = next((e for e in usable if e["pinned"]), None) or picks[mode]
    for e in evs:
        e["utility"] = round(_utility(e, mode, cost_ref), 1)
    ordered = sorted(evs, key=lambda e: (not e["available"], -e["utility"], e["median_cost"] or 0))
    evidence = sum(weight(r, ctx) for r in records)
    return {"mode": mode, "mode_label": MODE_LABEL[mode], "pick": chosen, "picks": {m: (p or {}).get("team_key") for m, p in picks.items()},
            "candidates": ordered, "prior": {"score": round(prior[0], 1), "success": round(prior[1], 3), "runs": len(records)},
            "evidence": round(evidence, 2), "cost_reference": round(cost_ref, 2),
            "pinned": pinned or None,
            "summary": (f"Pinned for this repository · {chosen['why']}" if chosen and chosen.get("pinned") else
                        f"{MODE_LABEL[mode]} · {chosen['why']}" if chosen else "No candidate teams")}


def _pick(evs: list[dict], mode: str, cost_ref: float):
    if not evs:
        return None
    best = max(evs, key=lambda e: (e["lcb"], -(e["median_cost"] or 0)))
    if mode == "best":
        return best
    if mode == "balanced":
        return max(evs, key=lambda e: (_utility(e, "balanced", cost_ref), e["expected_score"]))
    floor = max(65.0, best["expected_score"] - 7.0)
    good = [e for e in evs if e["expected_score"] >= floor and e["success_chance"] >= 0.6]
    if not good:
        return best
    return min(good, key=lambda e: (e["median_cost"] if e["median_cost"] is not None else cost_ref, -e["expected_score"]))


def is_critical(task: dict) -> bool:
    tags = {str(t).strip().lower() for t in task.get("tags") or []}
    return bool(task.get("critical")) or task.get("priority") == "urgent" or "critical" in tags


def auto_pick(ranking: dict, task: dict, epsilon: float, rng: random.Random | None = None) -> dict:
    """The team autopilot uses: the ranking's pick, or with probability ε a less-tried alternative."""
    rng = rng or random.Random()
    pick = ranking.get("pick")
    if not pick:
        return {"team_key": None, "explored": False, "reason": "no candidates"}
    if pick.get("pinned"):
        return {"team_key": pick["team_key"], "explored": False, "reason": "pinned for this repository"}
    if is_critical(task):
        return {"team_key": pick["team_key"], "explored": False, "reason": "critical task: no exploration"}
    eps = max(0.0, min(1.0, float(epsilon or 0)))
    alts = [e for e in ranking.get("candidates") or [] if e["available"] and e["team_key"] != pick["team_key"]][:3]
    if alts and rng.random() < eps:
        alt = min(alts, key=lambda e: (e["confidence"], -e["utility"]))
        return {"team_key": alt["team_key"], "explored": True,
                "reason": f"exploring an alternative ({round(eps * 100)}% of picks) to keep learning: {alt['why']}"}
    return {"team_key": pick["team_key"], "explored": False, "reason": ranking.get("summary", "")}


def candidate_keys(records: list[dict], presets: list[dict], default_roles: dict, current_roles: dict | None = None) -> list[str]:
    """Teams worth comparing: every team with history, every preset, the configured default and the draft's team."""
    keys = []

    def add(k):
        if k and k not in keys:
            keys.append(k)
    for r in records:
        add(r.get("team_key"))
    for roles in ([current_roles] if current_roles else []) + [default_roles]:
        add(team_key({r: v for r, v in (roles or {}).items() if isinstance(v, dict)}))
    for p in presets or []:
        add(team_key({r: {"agent": (v or {}).get("agent", "")} for r, v in (p.get("roles") or {}).items()}))
    return keys
