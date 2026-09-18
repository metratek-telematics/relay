"""The design step: system design before implementation, reviewed, approved, then built contract first.

Simple single-repository tasks skip it and behave as before. When a task needs it (multi-repository, a
feature/refactor the supervisor did not call simple, a complex assessment, or "Design first: always"):

    kickoff (plan with `complexity`)
      → design      the supervisor writes a DESIGN envelope; Relay validates it and renders SYSTEM_DESIGN.md
      → review      an independent agent challenges it against a checklist (severities); blocking findings go
                    back to the supervisor, at most `design_max_revisions` times, then to the human
      → approval    the human approves or requests changes (Needs you inbox), or Relay auto-approves in
                    unattended / quiet mode after the review passed
      → dialogue    work packages in dependency order (data → backend → frontend, providers before consumers),
                    each carrying the contract slices it implements
      → review      the final reviewer checks design conformance and cross-repository consistency
      → deliver     every pull request carries the shared change set with the merge order

The design lives in task meta `design` (structured) and SYSTEM_DESIGN.md (rendered). Deviations during
implementation are recorded as amendments (`design.amendments`), never silent.

Pure functions come first (tested without a pipeline); `DesignFlow` is the Pipeline mixin.
"""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

from .util import new_id, now, quiet, safe_slug, truncate, write_text

MODES = ("auto", "always", "never")
APPROVAL_MODES = ("auto", "on", "off")
DOC_MODES = ("commit", "comment", "off")
COMPLEXITY = ("simple", "moderate", "complex")
LAYERS = ("data", "backend", "frontend", "other")
LAYER_RANK = {"data": 0, "backend": 1, "frontend": 2, "other": 3}
KIND_LAYER = {"database": "data", "api": "backend", "worker": "backend", "library": "backend", "infra": "data", "frontend": "frontend"}
AUTO_TEMPLATES = ("feature", "refactor")

SECTIONS = [  # (anchor, title) in render order; the Design tab builds its navigation from these headings
    ("goal", "Goal and scope"), ("components", "Affected components"), ("contracts", "API contracts"),
    ("data-model", "Data model and migrations"), ("sequence", "Sequence of calls"), ("ui", "UI plan"),
    ("risks", "Failure modes, security, performance"), ("rollout", "Rollout and merge order"),
    ("tests", "Test strategy"), ("acceptance", "Acceptance criteria"), ("packages", "Work packages"),
    ("amendments", "Amendments"), ("review", "Design review"),
]

REVIEW_CHECKLIST = [
    "Missing component: does the request reach a service, database, job or UI the design does not list?",
    "Contract mismatch: do consumer and provider agree on method, path, field names, types, status codes and error bodies?",
    "Migration safety: is every schema change reversible (down), safe on existing rows (defaults, backfill), and ordered before the code that needs it?",
    "Backward compatibility: can each repository be merged and deployed alone in the stated merge order without breaking the others (old clients, old servers)?",
    "Failure handling: timeouts, retries, partial failure between services, idempotency, concurrent requests.",
    "Security: authentication, authorization, input validation, secrets.",
    "Testability: does every contract have a provider-side test and a consumer-side test or mock against the same schema, and every acceptance criterion a way to prove it?",
    "Work packages: in dependency order, each in one repository, each naming the contracts it implements.",
]


def _s(v, n=2000) -> str:
    return truncate(str(v).strip(), n) if v is not None else ""


def _list(v, n=600, cap=40) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = [line.strip(" -*\t") for line in v.splitlines()]
    if not isinstance(v, list):
        v = [v]
    out = []
    for x in v:
        if isinstance(x, dict):
            x = "; ".join(f"{k}: {val}" for k, val in x.items() if val)
        x = _s(x, n)
        if x:
            out.append(x)
    return out[:cap]


def _text(v, n=1500) -> str:
    """A schema or example given as text or JSON."""
    if v is None or v == "":
        return ""
    if isinstance(v, (dict, list)):
        import json
        return truncate(json.dumps(v, ensure_ascii=False), n)
    return _s(v, n)


# ----------------------------------------------------------------------------- complexity and triggers
def normalize_complexity(raw) -> dict:
    """The supervisor's assessment in the plan envelope: {"level": simple|moderate|complex, "reason": "…"}."""
    if isinstance(raw, str):
        raw = {"level": raw}
    if not isinstance(raw, dict):
        return {}
    level = str(raw.get("level") or raw.get("complexity") or "").strip().lower()
    if level not in COMPLEXITY:
        return {}
    return {"level": level, "reason": _s(raw.get("reason"), 400)}


def design_mode(task: dict, cfg: dict) -> str:
    m = str((task.get("workflow") or {}).get("design_mode") or cfg.get("design_mode") or "auto").lower()
    return m if m in MODES else "auto"


def needs_design(task: dict, cfg: dict, complexity: dict | None = None, multi_repo: bool = False) -> tuple[bool, str]:
    """Whether the task runs the design phase, and why. Simple single-repository tasks never do unless forced."""
    mode = design_mode(task, cfg)
    level = (complexity or {}).get("level") or ""
    if mode == "never":
        return False, "design first is off for this task"
    if mode == "always":
        return True, "design first is always on for this task"
    if multi_repo:
        return True, "the task changes several repositories"
    if level == "complex":
        return True, "the supervisor assessed the task as complex" + (f": {complexity.get('reason')}" if complexity.get("reason") else "")
    templates = cfg.get("design_auto_templates") or list(AUTO_TEMPLATES)
    if (task.get("template") or "feature") in templates and level == "moderate":
        return True, f"a {task.get('template') or 'feature'} the supervisor assessed as moderate"
    return False, ("the supervisor assessed the task as simple" if level == "simple" else "no design trigger applies")


def upfront_design(task: dict, cfg: dict, multi_repo: bool) -> bool:
    """Known before the plan: the kickoff prompt can say a design phase follows."""
    mode = design_mode(task, cfg)
    return mode == "always" or (mode == "auto" and multi_repo)


# ----------------------------------------------------------------------------- the design envelope
def _contract(x, i) -> dict | None:
    if isinstance(x, str):
        x = {"endpoint": x}
    if not isinstance(x, dict):
        return None
    method = _s(x.get("method"), 12).upper()
    path = _s(x.get("path") or "", 300)
    endpoint = _s(x.get("endpoint") or "", 300)
    if not path and endpoint:
        m = re.match(r"^\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S.*)$", endpoint, re.I)
        if m:
            method, path = method or m.group(1).upper(), m.group(2).strip()
        else:
            path = endpoint
    c = {"id": _s(x.get("id"), 20) or f"C{i}", "provider": _s(x.get("provider"), 80), "consumer": _s(x.get("consumer"), 200),
         "method": method, "path": path, "kind": _s(x.get("kind") or ("http" if method or path.startswith("/") else ""), 20),
         "summary": _s(x.get("summary") or x.get("description"), 400),
         "request": _text(x.get("request") or x.get("request_schema")), "response": _text(x.get("response") or x.get("response_schema")),
         "errors": _list(x.get("errors"), 300, 20), "compatibility": _s(x.get("compatibility") or x.get("versioning") or x.get("backward_compatibility"), 600),
         "notes": _s(x.get("notes"), 600)}
    return c if (c["path"] or c["summary"]) else None


def _data_change(x, i) -> dict | None:
    if isinstance(x, str):
        x = {"change": x}
    if not isinstance(x, dict):
        return None
    d = {"id": _s(x.get("id"), 20) or f"D{i}", "component": _s(x.get("component") or x.get("repo"), 80),
         "change": _s(x.get("change") or x.get("summary"), 800), "up": _s(x.get("up") or x.get("migration_up") or x.get("migration"), 1200),
         "down": _s(x.get("down") or x.get("migration_down") or x.get("rollback"), 1200), "backfill": _s(x.get("backfill"), 600)}
    return d if d["change"] else None


def _package(x, i) -> dict | None:
    if isinstance(x, str):
        x = {"summary": x}
    if not isinstance(x, dict):
        return None
    deps = x.get("depends_on") or []
    impl = x.get("implements") or x.get("contracts") or []
    layer = str(x.get("layer") or "").strip().lower()
    p = {"id": _s(x.get("id"), 20) or f"W{i}", "repo": _s(x.get("repo"), 80), "layer": layer if layer in LAYERS else "",
         "summary": _s(x.get("summary") or x.get("title"), 400), "instruction": _s(x.get("instruction"), 4000),
         "implements": [_s(d, 20) for d in (impl if isinstance(impl, list) else [impl]) if _s(d)],
         "depends_on": [_s(d, 20) for d in (deps if isinstance(deps, list) else [deps]) if _s(d)],
         "tests": _s(x.get("tests"), 800)}
    return p if (p["summary"] or p["instruction"]) else None


def normalize_design(env: dict) -> dict:
    """A DESIGN envelope as the structured record kept in task meta. Loose shapes are tolerated."""
    env = env if isinstance(env, dict) else {}
    ui = env.get("ui") or env.get("ui_plan") or []
    if isinstance(ui, dict):
        ui = ui.get("screens") or [ui]
    screens = []
    for s in ui if isinstance(ui, list) else [ui]:
        if isinstance(s, str) and s.strip():
            screens.append({"screen": _s(s, 200), "description": "", "states": []})
        elif isinstance(s, dict):
            states = s.get("states") or []
            if isinstance(states, dict):
                states = [f"{k}: {v}" for k, v in states.items()]
            screens.append({"screen": _s(s.get("screen") or s.get("name"), 200), "description": _s(s.get("description"), 800),
                            "states": _list(states, 300, 12)})
    rollout = env.get("rollout") or {}
    if isinstance(rollout, str):
        rollout = {"notes": rollout}
    tests = []
    for t in env.get("test_strategy") or env.get("tests") or []:
        if isinstance(t, dict):
            tests.append({"repo": _s(t.get("repo") or t.get("component"), 80), "strategy": _s(t.get("strategy") or t.get("tests"), 1200)})
        elif _s(t):
            tests.append({"repo": "", "strategy": _s(t, 1200)})
    e2e = []
    for i, e in enumerate(env.get("e2e") or env.get("e2e_scenarios") or [], 1):
        if isinstance(e, dict):
            e2e.append({"id": _s(e.get("id"), 20) or f"E{i}", "scenario": _s(e.get("scenario") or e.get("summary"), 800),
                        "stack_check": _s(e.get("stack_check") or e.get("check"), 120)})
        elif _s(e):
            e2e.append({"id": f"E{i}", "scenario": _s(e, 800), "stack_check": ""})
    comps = []
    for c in env.get("components") or []:
        if isinstance(c, str) and c.strip():
            comps.append({"name": _s(c, 80), "repo": "", "reason": "", "change": ""})
        elif isinstance(c, dict):
            comps.append({"name": _s(c.get("name") or c.get("id") or c.get("repo"), 80), "repo": _s(c.get("repo") or c.get("name"), 80),
                          "reason": _s(c.get("reason"), 400), "change": _s(c.get("change"), 600)})
    return {
        "summary": _s(env.get("summary"), 400),
        "goal": _s(env.get("goal"), 1500),
        "scope": _list(env.get("scope")), "non_goals": _list(env.get("non_goals")),
        "components": [c for c in comps if c["name"]][:20],
        "contracts": [c for c in (_contract(x, i) for i, x in enumerate(env.get("contracts") or env.get("api_contracts") or [], 1)) if c][:40],
        "data_model": [d for d in (_data_change(x, i) for i, x in enumerate(env.get("data_model") or [], 1)) if d][:30],
        "sequence": _list(env.get("sequence"), 500, 30),
        "ui": [s for s in screens if s["screen"]][:20],
        "failure_modes": _list(env.get("failure_modes")), "security": _list(env.get("security")), "performance": _list(env.get("performance")),
        "rollout": {"merge_order": _list(rollout.get("merge_order") or rollout.get("order"), 80, 20),
                    "feature_flags": _list(rollout.get("feature_flags"), 300, 10), "notes": _s(rollout.get("notes"), 1200)},
        "tests": tests[:20], "e2e": e2e[:20],
        "acceptance": env.get("acceptance") if isinstance(env.get("acceptance"), list) else [],
        "work_packages": [p for p in (_package(x, i) for i, x in enumerate(env.get("work_packages") or [], 1)) if p][:40],
    }


def _match_repo(ref: str, repos: list[str]) -> str:
    ref = (ref or "").strip().lower()
    if not ref:
        return ""
    for r in repos:
        if ref == r.lower() or ref.split("/")[-1] == r.lower():
            return r
    for r in repos:  # "catalog" for "catalog-api"
        if ref in r.lower() or r.lower() in ref:
            return r
    return ""


def validate(design: dict, repos: list[str], frontend: bool = False) -> list[str]:
    """Problems that make a design unusable as a contract for implementation. Empty list: usable."""
    problems = []
    if not design.get("goal"):
        problems.append("`goal` is missing")
    if not design.get("components"):
        problems.append("`components` is empty: list every component the change touches, with the reason")
    if not design.get("work_packages"):
        problems.append("`work_packages` is empty")
    if not design.get("acceptance"):
        problems.append("`acceptance` is empty: derive 2 to 6 checkable criteria from the design")
    multi = len(repos) > 1
    if multi and not design.get("contracts"):
        problems.append("the task spans several repositories but `contracts` is empty")
    ids = {c["id"] for c in design.get("contracts") or []} | {d["id"] for d in design.get("data_model") or []} | {e["id"] for e in design.get("e2e") or []}
    pkg_ids = {p["id"] for p in design.get("work_packages") or []}
    for p in design.get("work_packages") or []:
        unknown = [i for i in p["implements"] if i not in ids and not re.match(r"^(ui|UI|A\d+)", i)]
        if unknown:
            problems.append(f"{p['id']} implements unknown ids {', '.join(unknown)}")
        bad = [d for d in p["depends_on"] if d not in pkg_ids]
        if bad:
            problems.append(f"{p['id']} depends on unknown packages {', '.join(bad)}")
        if multi and not _match_repo(p["repo"], repos):
            problems.append(f"{p['id']} names repository '{p['repo'] or '(none)'}', which is not one of {', '.join(repos)}")
    for c in design.get("contracts") or []:
        if c.get("kind") in ("http", "") and c.get("path") and not (c.get("request") or c.get("response")):
            problems.append(f"{c['id']} {c.get('method', '')} {c['path']} has no request or response schema")
    if frontend and not design.get("ui"):
        problems.append("a frontend is involved but `ui` is empty: describe the screens and their empty, loading and error states")
    try:
        order_packages(design.get("work_packages") or [], design)
    except ValueError as e:
        problems.append(str(e))
    return problems


# ----------------------------------------------------------------------------- work packages
def layer_of(pkg: dict, design: dict, kinds: dict | None = None) -> str:
    if pkg.get("layer") in LAYERS:
        return pkg["layer"]
    kind = (kinds or {}).get((pkg.get("repo") or "").lower())
    if kind in KIND_LAYER:
        return KIND_LAYER[kind]
    text = f"{pkg.get('repo', '')} {pkg.get('summary', '')}".lower()
    if re.search(r"\b(migration|schema|table|sql|database|db)\b", text):
        return "data"
    if re.search(r"\b(web|ui|frontend|front-end|page|screen|component|react|vue|svelte)\b", text):
        return "frontend"
    if re.search(r"\b(api|service|endpoint|backend|server|worker)\b", text):
        return "backend"
    return "other"


def order_packages(packages: list[dict], design: dict | None = None, kinds: dict | None = None) -> list[dict]:
    """Dependency order: explicit depends_on, providers before consumers of the contracts a package implements,
    then layer (data → backend → frontend), then the order the supervisor wrote. Raises ValueError on a cycle."""
    design = design or {}
    pkgs = [dict(p) for p in packages]
    by_id = {p["id"]: p for p in pkgs}
    contracts = {c["id"]: c for c in design.get("contracts") or []}
    for p in pkgs:
        p["layer"] = layer_of(p, design, kinds)
        deps = [d for d in p.get("depends_on") or [] if d in by_id and d != p["id"]]
        # Contract first: a package consuming a contract waits for the package that provides it.
        for cid in p.get("implements") or []:
            c = contracts.get(cid)
            if not c or not c.get("provider"):
                continue
            prov = (c["provider"] or "").lower()
            if (p.get("repo") or "").lower() == prov:
                continue
            for q in pkgs:
                if q is not p and cid in (q.get("implements") or []) and (q.get("repo") or "").lower() == prov and q["id"] not in deps:
                    deps.append(q["id"])
        p["depends_on"] = deps
    index = {p["id"]: i for i, p in enumerate(pkgs)}
    done, out = set(), []
    while len(out) < len(pkgs):
        ready = [p for p in pkgs if p["id"] not in done and all(d in done for d in p["depends_on"])]
        if not ready:
            stuck = [p["id"] for p in pkgs if p["id"] not in done]
            raise ValueError(f"work packages depend on each other in a cycle: {', '.join(stuck)}")
        ready.sort(key=lambda p: (LAYER_RANK.get(p["layer"], 3), index[p["id"]]))
        nxt = ready[0]
        done.add(nxt["id"])
        out.append(nxt)
    return out


def merge_order(design: dict, repos: list[str]) -> list[str]:
    """Repositories in the order their pull requests should merge: the design's stated order, else package order."""
    order = []
    for r in (design.get("rollout") or {}).get("merge_order") or []:
        m = _match_repo(r, repos)
        if m and m not in order:
            order.append(m)
    for p in design.get("work_packages") or []:
        m = _match_repo(p.get("repo"), repos) or (repos[0] if len(repos) == 1 else "")
        if m and m not in order:
            order.append(m)
    return order + [r for r in repos if r not in order]


# ----------------------------------------------------------------------------- slices for prompts
def contract_line(c: dict) -> str:
    head = f"{c['id']} {c.get('method', '')} {c.get('path', '')}".replace("  ", " ").strip()
    parts = [head + (f" · {c['consumer']} → {c['provider']}" if c.get("provider") else "")]
    if c.get("summary"):
        parts.append(f"  {c['summary']}")
    for k, label in (("request", "request"), ("response", "response")):
        if c.get(k):
            parts.append(f"  {label}: {c[k]}")
    if c.get("errors"):
        parts.append("  errors: " + "; ".join(c["errors"]))
    if c.get("compatibility"):
        parts.append(f"  compatibility: {c['compatibility']}")
    if c.get("notes"):
        parts.append(f"  notes: {c['notes']}")
    return "\n".join(parts)


def contract_slice(design: dict, pkg: dict, stack: bool = False) -> str:
    """What a worker needs for one package: the contracts and data changes it implements, and how to test them."""
    ids = set(pkg.get("implements") or [])
    repo = (pkg.get("repo") or "").lower()
    lines = []
    contracts = [c for c in design.get("contracts") or [] if c["id"] in ids
                 or (repo and (repo == (c.get("provider") or "").lower() or repo in (c.get("consumer") or "").lower()))]
    if contracts:
        lines.append("CONTRACTS THIS PACKAGE IMPLEMENTS (from the approved design; implement them exactly, field names and status codes included)")
        lines += [contract_line(c) for c in contracts]
        provides = [c["id"] for c in contracts if repo and repo == (c.get("provider") or "").lower()]
        consumes = [c["id"] for c in contracts if c["id"] not in provides]
        if provides:
            lines.append(f"Contract tests (provider side): add tests that call {', '.join(provides)} and assert the response schema, status codes and error bodies above.")
        if consumes:
            lines.append(f"Contract tests (consumer side): test the client code for {', '.join(consumes)} against a mock or fixture that uses exactly the schema above, including the error responses.")
    data = [d for d in design.get("data_model") or [] if d["id"] in ids or (repo and repo == (d.get("component") or "").lower())]
    if data:
        lines.append("DATA MODEL CHANGES")
        for d in data:
            lines.append(f"{d['id']} {d.get('component', '')}: {d['change']}" + (f"\n  up: {d['up']}" if d.get("up") else "")
                         + (f"\n  down: {d['down']}" if d.get("down") else "") + (f"\n  backfill: {d['backfill']}" if d.get("backfill") else ""))
    if pkg.get("layer") == "frontend" and design.get("ui"):
        lines.append("UI PLAN")
        for s in design["ui"]:
            lines.append(f"- {s['screen']}: {s.get('description', '')}" + (f" · states: {'; '.join(s['states'])}" if s.get("states") else ""))
    if pkg.get("tests"):
        lines.append(f"TESTS FOR THIS PACKAGE: {pkg['tests']}")
    scenarios = [e for e in design.get("e2e") or [] if e["id"] in ids]
    if scenarios:
        lines.append("END-TO-END SCENARIOS")
        lines += [f"- {e['id']}: {e['scenario']}" + (f" (stack check `{e['stack_check']}`)" if e.get("stack_check") else "") for e in scenarios]
    if stack and (scenarios or pkg.get("layer") == "frontend" or design.get("e2e")):
        lines.append("END-TO-END EVIDENCE: `relay-stack` is a command on PATH in your shell (not a tool). Run `relay-stack up` and then "
                     "`relay-stack check` yourself before you report, and paste the output in your report as evidence. Do not skip it.")
    lines.append("If this package cannot follow the design, do not diverge silently: report it with "
                 '"amendment":{"ref":"C1","change":"…","reason":"…"} in your envelope, and the supervisor decides.')
    return "\n".join(lines)


def design_block(design: dict, packages: list[dict] | None = None, full: bool = False) -> str:
    """The approved design for prompts. `full` adds every contract in detail (supervisor and reviewer)."""
    if not design:
        return ""
    lines = [f"APPROVED SYSTEM DESIGN v{design.get('version', 1)} (the contract for implementation; change it only through an amendment)"]
    if design.get("goal"):
        lines.append(f"Goal: {design['goal']}")
    if design.get("non_goals"):
        lines.append("Non-goals: " + "; ".join(design["non_goals"]))
    for c in design.get("components") or []:
        lines.append(f"- component {c['name']}: {c.get('reason', '')}")
    if full:
        lines += [contract_line(c) for c in design.get("contracts") or []]
        for d in design.get("data_model") or []:
            lines.append(f"{d['id']} data {d.get('component', '')}: {d['change']}")
    else:
        lines += [f"- {c['id']} {c.get('method', '')} {c.get('path', '')} ({c.get('consumer', '')} → {c.get('provider', '')})" for c in design.get("contracts") or []]
    for e in design.get("e2e") or []:
        lines.append(f"- {e['id']} end to end: {e['scenario']}")
    pk = packages if packages is not None else design.get("work_packages") or []
    if pk:
        lines.append("WORK PACKAGES (in this order)")
        lines += [f"- {p['id']} [{p.get('repo') or 'primary'} · {p.get('layer') or ''}] {p['summary']}"
                  + (f" · implements {', '.join(p['implements'])}" if p.get("implements") else "")
                  + (f" · after {', '.join(p['depends_on'])}" if p.get("depends_on") else "") for p in pk]
    for a in design.get("amendments") or []:
        lines.append(f"- amendment {a['id']} ({a.get('by', '')}) {a.get('ref', '')}: {a['change']}")
    return "\n".join(lines)


def legacy_system_design(design: dict) -> dict:
    """The multi-repository `system_design` shape older views and prompts read."""
    return {"summary": design.get("goal") or design.get("summary") or "",
            "components": [c["name"] for c in design.get("components") or []],
            "api_contracts": [{"provider": c.get("provider", ""), "consumer": c.get("consumer", ""), "endpoint": f"{c.get('method', '')} {c.get('path', '')}".strip(),
                               "request": c.get("request", ""), "response": c.get("response", ""), "notes": c.get("id", "")} for c in design.get("contracts") or []],
            "data_model": [{"component": d.get("component", ""), "change": d["change"]} for d in design.get("data_model") or []],
            "sequence": list(design.get("sequence") or [])}


# ----------------------------------------------------------------------------- amendments
def amendments_from(env: dict) -> list[dict]:
    raw = (env or {}).get("amendments") or (env or {}).get("amendment") or []
    if isinstance(raw, (dict, str)):
        raw = [raw]
    out = []
    for a in raw:
        if isinstance(a, str) and a.strip():
            a = {"change": a}
        if isinstance(a, dict) and _s(a.get("change")):
            out.append({"ref": _s(a.get("ref") or a.get("contract") or a.get("section"), 40), "change": _s(a.get("change"), 1200),
                        "reason": _s(a.get("reason"), 600)})
    return out


def add_amendments(design: dict, items: list[dict], by: str, package: str = "") -> tuple[dict, list[dict]]:
    """Record amendments on the design. Duplicates (same ref and change) are ignored. Returns (design, added)."""
    design = dict(design or {})
    rows = list(design.get("amendments") or [])
    added = []
    for a in items:
        if any(r.get("ref") == a["ref"] and r.get("change") == a["change"] for r in rows):
            continue
        row = {"id": f"AM{len(rows) + 1}", "time": now(), "by": by, "package": package, **a}
        rows.append(row)
        added.append(row)
    design["amendments"] = rows
    return design, added


# ----------------------------------------------------------------------------- rendering
def _h(anchor: str) -> str:
    return f"## {dict(SECTIONS)[anchor]}"


def design_md(design: dict, title: str = "") -> str:
    if not design:
        return ""
    d = design
    out = [f"# System design{': ' + title if title else ''}", ""]
    meta = [f"version {d.get('version', 1)}", f"status {d.get('status', 'draft')}"]
    if (d.get("complexity") or {}).get("level"):
        meta.append(f"complexity {d['complexity']['level']}")
    if d.get("approval"):
        meta.append(f"approved by {d['approval'].get('by')} {d['approval'].get('time', '')[:16]}")
    out += ["_" + " · ".join(meta) + "_", ""]
    out += [_h("goal"), "", d.get("goal") or "(missing)", ""]
    if d.get("scope"):
        out += ["**In scope**", *[f"- {x}" for x in d["scope"]], ""]
    if d.get("non_goals"):
        out += ["**Non-goals**", *[f"- {x}" for x in d["non_goals"]], ""]
    out += [_h("components"), ""]
    out += [f"- **{c['name']}**" + (f" ({c['repo']})" if c.get("repo") and c["repo"] != c["name"] else "") + (f": {c['reason']}" if c.get("reason") else "")
            + (f"\n  - change: {c['change']}" if c.get("change") else "") for c in d.get("components") or []] or ["(none)"]
    out += ["", _h("contracts"), ""]
    for c in d.get("contracts") or []:
        out.append(f"### {c['id']} · `{(c.get('method', '') + ' ' + c.get('path', '')).strip()}`")
        out.append("")
        if c.get("provider") or c.get("consumer"):
            out.append(f"{c.get('consumer') or '?'} → **{c.get('provider') or '?'}**" + (f" · {c['summary']}" if c.get("summary") else ""))
            out.append("")
        if c.get("request"):
            out += ["Request:", "```", c["request"], "```"]
        if c.get("response"):
            out += ["Response:", "```", c["response"], "```"]
        if c.get("errors"):
            out += ["Errors:", *[f"- {e}" for e in c["errors"]]]
        if c.get("compatibility"):
            out.append(f"Compatibility: {c['compatibility']}")
        if c.get("notes"):
            out.append(f"Notes: {c['notes']}")
        out.append("")
    if not d.get("contracts"):
        out += ["(no contracts between components)", ""]
    out += [_h("data-model"), ""]
    for x in d.get("data_model") or []:
        out.append(f"- **{x['id']}** {x.get('component', '')}: {x['change']}")
        for k in ("up", "down", "backfill"):
            if x.get(k):
                out.append(f"  - {k}: `{x[k]}`" if "\n" not in x[k] else f"  - {k}:\n\n```\n{x[k]}\n```")
    if not d.get("data_model"):
        out.append("(no data model changes)")
    out += ["", _h("sequence"), ""]
    out += [f"{i}. {s}" for i, s in enumerate(d.get("sequence") or [], 1)] or ["(not given)"]
    out += ["", _h("ui"), ""]
    for s in d.get("ui") or []:
        out.append(f"- **{s['screen']}**" + (f": {s['description']}" if s.get("description") else ""))
        out += [f"  - {st}" for st in s.get("states") or []]
    if not d.get("ui"):
        out.append("(no UI changes)")
    out += ["", _h("risks"), ""]
    for key, label in (("failure_modes", "Failure modes"), ("security", "Security"), ("performance", "Performance")):
        if d.get(key):
            out += [f"**{label}**", *[f"- {x}" for x in d[key]], ""]
    ro = d.get("rollout") or {}
    out += [_h("rollout"), ""]
    if d.get("merge_order"):
        out += ["Merge order:", *[f"{i}. {r}" for i, r in enumerate(d["merge_order"], 1)], ""]
    if ro.get("feature_flags"):
        out += ["Feature flags:", *[f"- {x}" for x in ro["feature_flags"]], ""]
    if ro.get("notes"):
        out += [ro["notes"], ""]
    out += [_h("tests"), ""]
    out += [f"- **{t['repo'] or 'all'}**: {t['strategy']}" for t in d.get("tests") or []]
    if d.get("e2e"):
        out += ["", "End-to-end scenarios:"] + [f"- **{e['id']}** {e['scenario']}" + (f" · stack check `{e['stack_check']}`" if e.get("stack_check") else "") for e in d["e2e"]]
    out += ["", _h("acceptance"), ""]
    for a in d.get("acceptance") or []:
        if isinstance(a, dict):
            out.append(f"- **{a.get('id', '')}** {a.get('criterion', '')}" + (f" · verify: {a['how_to_verify']}" if a.get("how_to_verify") else ""))
        else:
            out.append(f"- {a}")
    out += ["", _h("packages"), ""]
    for p in d.get("work_packages") or []:
        out.append(f"{p['id']}. **[{p.get('repo') or 'primary'}]** {p['summary']}" + (f" · implements {', '.join(p['implements'])}" if p.get("implements") else "")
                   + (f" · after {', '.join(p['depends_on'])}" if p.get("depends_on") else "") + (f" · {p['layer']}" if p.get("layer") else ""))
    out += ["", _h("amendments"), ""]
    out += [f"- **{a['id']}** by {a.get('by', '')}{' in ' + a['package'] if a.get('package') else ''} · {a.get('ref') or 'design'}: {a['change']}"
            + (f" (why: {a['reason']})" if a.get("reason") else "") for a in d.get("amendments") or []] or ["(none)"]
    rv = d.get("review") or {}
    out += ["", _h("review"), ""]
    if rv:
        out.append(f"Round {rv.get('round', 1)} · **{rv.get('verdict', '')}** · {rv.get('summary', '')}")
        out += [f"- [{f.get('severity')}] {f.get('file') or f.get('section') or ''} {f.get('problem', '')}" + (f" → {f['fix']}" if f.get("fix") else "") for f in rv.get("findings") or []]
    else:
        out.append("(not reviewed yet)")
    return "\n".join(out).rstrip() + "\n"


# ----------------------------------------------------------------------------- change set (pull request bodies)
CHANGESET_START = "<!-- relay:changeset -->"
CHANGESET_END = "<!-- /relay:changeset -->"


def task_repo_rows(task: dict) -> list[dict]:
    """Every repository of a task with its branch and pull request, primary first."""
    rows = [{"name": Path(task.get("repo") or "").name, "primary": True, "github_repo": task.get("github_repo") or "",
             "branch": task.get("branch") or task.get("branch_name") or "", "pr_url": task.get("pr_url") or "", "pr_number": task.get("pr_number"),
             "changed_count": task.get("changed_count")}]
    for name, w in (task.get("repo_worktrees") or {}).items():
        rows.append({"name": name, "primary": False, "github_repo": w.get("github_repo") or "", "branch": w.get("branch") or "",
                     "pr_url": w.get("pr_url") or "", "pr_number": w.get("pr_number"), "changed_count": w.get("changed_count")})
    return rows


def changeset(task: dict) -> dict:
    """The structured change set: merge order with pull requests, design link, evidence."""
    design = task.get("design") or {}
    rows = task_repo_rows(task)
    names = [r["name"] for r in rows]
    order = merge_order(design, names) if design else names
    by = {r["name"]: r for r in rows}
    return {"merge_order": [{**by[n], "step": i} for i, n in enumerate(order, 1)], "design_doc": task.get("design_doc") or {},
            "design_version": design.get("version"), "has_design": bool(design)}


def changeset_md(task: dict, cfg: dict | None = None) -> str:
    """The shared "Change set" section every pull request of the task carries."""
    cfg = cfg or {}
    design = task.get("design") or {}
    cs = changeset(task)
    multi = len(cs["merge_order"]) > 1
    if not design and not multi:
        return ""
    lines = [CHANGESET_START, "## Change set", ""]
    doc = cs["design_doc"]
    if design:
        where = (f"[{doc['path']}]({doc['url']})" if doc.get("url") else f"`{doc['path']}` in {doc.get('repo') or 'the primary repository'}") if doc.get("path") \
            else ("posted as a comment on the primary pull request" if doc.get("mode") == "comment" else "kept in Relay (SYSTEM_DESIGN.md in the task's run folder)")
        lines += [f"**Design:** {where} · version {design.get('version', 1)}" + (f" · approved by {design['approval']['by']}" if design.get("approval") else ""), ""]
        if design.get("goal"):
            lines += [f"> {truncate(design['goal'], 400)}", ""]
        if design.get("contracts"):
            lines += ["**Contracts**", ""] + [f"- `{c['id']}` `{(c.get('method', '') + ' ' + c.get('path', '')).strip()}` {c.get('consumer') or '?'} → {c.get('provider') or '?'}"
                                              + (f" · {truncate(c['summary'], 120)}" if c.get("summary") else "") for c in design["contracts"]] + [""]
        if design.get("data_model"):
            lines += ["**Data model**", ""] + [f"- `{d['id']}` {d.get('component', '')}: {truncate(d['change'], 160)}" + (" (reversible)" if d.get("down") else " (no down migration)")
                                               for d in design["data_model"]] + [""]
        if design.get("amendments"):
            lines += ["**Amendments during implementation**", ""] + [f"- `{a['id']}` {a.get('ref') or ''}: {truncate(a['change'], 200)}" for a in design["amendments"]] + [""]
    v = task.get("verification") or {}
    items = v.get("items") or []
    if items:
        unit = [i for i in items if i.get("kind") != "e2e"]
        e2e = [i for i in items if i.get("kind") == "e2e"]
        mark = lambda i: "✓" if i.get("passed", i.get("ok")) else ("~" if i.get("ok") else "✗")
        lines += ["**Evidence**", ""]
        lines += [f"- {mark(i)} `{i['command']}`" + (" (pre-existing failure)" if i.get("pre_existing") else "") + (" (optional)" if i.get("optional") else "") for i in unit]
        lines += [f"- {mark(i)} end to end · `{i['command'].replace('e2e · ', '')}`" + (" (could not run)" if i.get("cannot_run") else "") for i in e2e]
        if task.get("connectors_active"):
            lines.append(f"- connectors available to the team: {', '.join(task['connectors_active'])} (see the task's messages for what was checked)")
        lines.append("")
    verified = [i for i in items if i.get("kind") == "e2e" and i.get("passed")]
    lines += ["**Verified end to end:** " + (", ".join(f"`{i['command'].replace('e2e · ', '')}`" for i in verified) if verified
                                              else "nothing; no integration stack check passed for this change"), ""]
    not_verified = [f"{b.get('check') or 'check'}: {b.get('reason', '')}" for b in task.get("blocked_checks") or []]
    not_verified += [f"{e['id']}: {e['scenario']}" for e in design.get("e2e") or [] if not any(e.get("stack_check") and e["stack_check"] in i["command"] for i in verified)]
    if not_verified:
        lines += ["**Not verified**", ""] + [f"- {truncate(x, 240)}" for x in not_verified] + [""]
    follow = task.get("follow_ups") or []
    if follow:
        lines += ["**Follow-ups**", ""] + [f"- [{f.get('severity', 'should_fix')}] {truncate(f.get('problem', ''), 200)}" for f in follow[:20]] + [""]
    if multi:
        lines += ["**Merge order**", ""]
        for r in cs["merge_order"]:
            pr = (f"{r['github_repo']}#{r['pr_number']}" if r.get("pr_number") and r.get("github_repo") else r.get("pr_url") or "pull request not opened")
            lines.append(f"- [ ] {r['step']}. **{r['name']}** · `{r.get('branch') or ''}` · {pr}")
        lines += ["", "Merge in this order; each step must be deployed before the next one merges."]
    lines.append(CHANGESET_END)
    return "\n".join(lines)


def replace_changeset(body: str, section: str) -> str:
    body = body or ""
    if CHANGESET_START in body and CHANGESET_END in body:
        a = body.index(CHANGESET_START)
        b = body.index(CHANGESET_END) + len(CHANGESET_END)
        return body[:a] + section + body[b:]
    return body.rstrip() + "\n\n" + section + "\n"


def doc_path(task: dict, day: date | None = None) -> str:
    day = day or date.today()
    return f"docs/designs/{day.isoformat()}-{safe_slug(task.get('name') or 'design', 50).lower()}.md"


# ----------------------------------------------------------------------------- prompts
def envelope_example(multi: bool) -> str:
    return ('{"type":"design","summary":"one line","goal":"…","scope":["…"],"non_goals":["…"],'
            '"components":[{"name":"<repo or component>","repo":"<repository name>","reason":"why it changes","change":"what changes"}],'
            '"contracts":[{"id":"C1","provider":"<repo>","consumer":"<repo>","method":"POST","path":"/things/{id}/hold","summary":"…",'
            '"request":{"qty":"int >= 1"},"response":{"hold_id":"str","expires_at":"ISO-8601 UTC"},"errors":["404 {\\"error\\":\\"not_found\\"}","409 {\\"error\\":\\"insufficient\\"}"],'
            '"compatibility":"additive; existing endpoints unchanged"}],'
            '"data_model":[{"id":"D1","component":"<repo>","change":"…","up":"SQL or migration file","down":"…","backfill":"none"}],'
            '"sequence":["web → orders POST /orders/{id}/reserve","orders → catalog POST …"],'
            '"ui":[{"screen":"…","description":"…","states":["empty: …","loading: …","error: …","success: …"]}],'
            '"failure_modes":["…"],"security":["…"],"performance":["…"],'
            '"rollout":{"merge_order":["<repo>","<repo>"],"feature_flags":[],"notes":"…"},'
            '"test_strategy":[{"repo":"<repo>","strategy":"provider tests for C1 …"}],'
            '"e2e":[{"id":"E1","scenario":"…","stack_check":"<stack check name, if a stack exists>"}],'
            '"acceptance":[{"id":"A1","criterion":"…","how_to_verify":"test: …","required":true}],'
            '"work_packages":[{"id":"W1","repo":"<repo>","layer":"data|backend|frontend","summary":"…","implements":["D1","C1"],"depends_on":[],'
            '"tests":"…","instruction":"the exact work package for the worker"}]}')


def design_request(reason: str, repos_text: str, map_text: str, stack_text: str, frontend: bool, multi: bool, guidance: str = "") -> str:
    parts = [f"ORCHESTRATOR · DESIGN FIRST. This task gets a system design before any implementation ({reason}).",
             "Inspect every repository involved (the code on both sides of each call), then reply with ONE design envelope. It becomes SYSTEM_DESIGN.md, "
             "is challenged by an independent reviewer, may need the human's approval, and then is the contract the worker implements.",
             ""]
    if repos_text:
        parts += [repos_text, ""]
    if map_text:
        parts += [map_text, ""]
    if stack_text:
        parts += ["INTEGRATION STACK (map each end-to-end scenario to one of its checks by name in `stack_check`)", stack_text, ""]
    parts += ["WHAT THE DESIGN MUST CONTAIN",
              "- goal, scope and non_goals;",
              "- components: every component the change touches (from the system map when there is one), each with the reason;",
              "- contracts: every API between components, with id (C1…), method, path, request and response schemas, error responses with status codes, "
              "and compatibility (can each side be deployed alone? versioning?);",
              "- data_model: schema changes with id (D1…), up and down migrations and backfill;",
              "- sequence: the calls in order for the main flow;",
              ("- ui: screens and their empty, loading, error and success states, following DESIGN.md and FRONTEND.md;" if frontend else "- ui: only if a user interface changes;"),
              "- failure_modes, security, performance: concrete, for this change;",
              "- rollout: merge_order across repositories (providers before consumers, migrations first) and feature flags if needed;",
              "- test_strategy per repository (provider-side contract tests, consumer-side tests with mocks matching the same schema) and e2e scenarios (E1…);",
              "- acceptance: 2 to 6 checkable criteria derived from the design (they replace the plan's criteria);",
              "- work_packages: one repository each, in dependency order (data and migrations → backend services → frontend), each with `implements` "
              "(the C/D/E ids), `depends_on`, `tests` and a short `instruction` for the worker.",
              "Keep it proportionate: a small change gets a short design. Do not implement anything yet.",
              "", "ENVELOPE", envelope_example(multi)]
    if guidance:
        parts += ["", "USER GUIDANCE", guidance]
    return "\n".join(parts)


def design_fix_request(problems: list[str]) -> str:
    return ("ORCHESTRATOR · the design envelope cannot be used yet:\n" + "\n".join(f"- {p}" for p in problems)
            + "\n\nReply with the complete corrected design envelope (all sections, not only the fixes).")


def design_revision_request(findings_text: str, note: str = "", source: str = "the design reviewer") -> str:
    return (f"ORCHESTRATOR · {source} asks for changes to the design.\n\n{findings_text}\n"
            + (f"\nHUMAN NOTE\n{note}\n" if note else "")
            + "\nCheck each point against the repositories. Fix the real ones; for a finding you reject, say why in `review_response`. "
              'Reply with the complete revised design envelope, adding "review_response":[{"finding":"F1","response":"fixed: … | rejected: …"}].')


def review_prompt(task: dict, design_text: str, repos_text: str, map_text: str, round_no: int, previous: str = "") -> str:
    checklist = "\n".join(f"{i}. {c}" for i, c in enumerate(REVIEW_CHECKLIST, 1))
    return f"""You are the DESIGN REVIEWER for a multi-agent engineering team run by Relay. The supervisor wrote a system design; nothing is implemented yet.
Your job is to find what would break when it is built and merged. You are independent of the supervisor. Do not modify any file.

TASK
{task.get('requirements', '').strip() or '(see issue)'}

{repos_text}

{map_text}

DESIGN UNDER REVIEW (round {round_no})
{design_text}
{(chr(10) + "YOUR EARLIER FINDINGS AND THE SUPERVISOR'S RESPONSES" + chr(10) + previous + chr(10)) if previous else ""}
CHECKLIST (read the code in the repositories to check each point; do not trust the design's description of existing code)
{checklist}

SEVERITY
- blocking: building this design as written would produce wrong behaviour, a broken contract between services, an unsafe or irreversible migration, a deployment order that breaks production, or a requirement that is not covered.
- should_fix: a real gap that does not break the change (a missing error case in a schema, a vague test strategy).
- nit: wording or taste.
Do not add scope the request does not contain, and do not redesign a sound choice you would have made differently.

Reply with exactly one fenced ```json envelope:
{{"type":"review","verdict":"PASS|FAIL","summary":"one line","findings":[{{"id":"F1","severity":"blocking","section":"contracts C1","problem":"…","fix":"…"}}]}}
FAIL needs at least one blocking finding.
"""


FINAL_REVIEW_BLOCK = """DESIGN CONFORMANCE AND CROSS-SERVICE CONSISTENCY (this task has an approved design)
Besides the usual review, check:
- every contract (C…) is implemented as designed on the provider AND called as designed by the consumer: method, path, field names and types, status codes, error bodies;
- the data model changes (D…) match the design, with a working down migration;
- the same concept has the same name, unit and format in every repository (ids, timestamps, money, enums);
- error handling agrees across services (what the provider returns is what the consumer handles);
- the end-to-end evidence (integration stack checks in the verification) covers the e2e scenarios, and what could not run is listed;
- any deviation from the design is recorded as an amendment; an unrecorded deviation that breaks a contract is blocking."""


def conformance_note(design: dict) -> str:
    if not design:
        return ""
    ids = ", ".join([c["id"] for c in design.get("contracts") or []] + [d["id"] for d in design.get("data_model") or []])
    return ("DESIGN CONFORMANCE · judge this package against the approved design" + (f" ({ids})" if ids else "")
            + ": names, schemas, status codes and error bodies as designed. A deviation is acceptable only as an amendment: add "
              '"amendment":{"ref":"C1","change":"…","reason":"…"} to your envelope so it is recorded and visible; never approve a silent one.')


# ============================================================================= the pipeline mixin
class DesignFlow:
    """Design phase hooks for the Pipeline. Relies on the Pipeline's run_role, escalate, state, m, r, cfg, task."""

    # ------------------------------------------------------------ records
    def design(self) -> dict:
        return dict(self.task_meta().get("design") or {})

    def save_design(self, design: dict, render=True):
        self.m.set_meta(self.tid, design=design)
        if render:
            self.artifact("design", "SYSTEM_DESIGN.md", design_md(design, self.task.get("name") or ""))

    def repo_names(self) -> list[str]:
        return [Path(self.task.get("repo") or "").name] + [r["name"] for r in getattr(self, "related", None) or []]

    def component_kinds(self) -> dict:
        from . import systemmap
        kinds = {}
        try:
            data = systemmap.load()
            for path in [self.task.get("repo")] + [r["repo"] for r in getattr(self, "related", None) or []]:
                comp = systemmap.for_repo(data, path) if path else None
                if comp and comp.get("kind"):
                    kinds[Path(path).name.lower()] = comp["kind"]
                    for r in getattr(self, "related", None) or []:
                        if r["repo"] == path:
                            kinds[r["name"].lower()] = comp["kind"]
        except Exception:
            pass
        return kinds

    def has_frontend(self) -> bool:
        from . import protocol
        kinds = self.component_kinds()
        if "frontend" in kinds.values():
            return True
        return "FRONTEND" in protocol.rule_names_for("supervisor", {"lean_prompts": True}, self.task.get("requirements") or "")

    def design_active(self) -> bool:
        d = self.task_meta().get("design") or {}
        return d.get("status") == "approved"

    # ------------------------------------------------------------ entry: after the plan
    def kickoff_design_note(self) -> str:
        if not upfront_design(self.task, self.cfg, bool(getattr(self, "related", None))):
            return ""
        return ("DESIGN FIRST · after your plan envelope Relay asks you for a full system design (contracts, data model, rollout, work packages), "
                "which is reviewed and possibly approved by the human before implementation. Keep the plan short and include "
                '"complexity":{"level":"simple|moderate|complex","reason":"…"}; its instruction is provisional.')

    def maybe_enter_design(self, env: dict):
        """Called at the end of kickoff: switch to the design phase when the task needs one."""
        cx = normalize_complexity(env.get("complexity"))
        multi = bool(getattr(self, "related", None))
        wanted, reason = needs_design(self.task, self.cfg, cx, multi)
        self.m.set_meta(self.tid, complexity=cx or None)
        self.state["complexity"] = cx
        if not wanted:
            if (self.task_meta().get("design") or {}).get("version") is not None:
                self.m.set_meta(self.tid, design=None)   # a design from an earlier run of this task no longer applies
            if cx:
                self.r.timeline("supervisor", f"Complexity: {cx['level']}", "no design phase · " + reason)
            self.save()
            return
        self.r.timeline("system", "Design first", reason)
        self.state.update({"phase": "design", "design_stage": "draft", "design_reason": reason, "design_revisions": 0})
        self.save_design({"status": "drafting", "version": 0, "complexity": cx, "trigger": reason}, render=False)
        self.save()

    # ------------------------------------------------------------ supervisor turns with design envelopes
    def _supervisor_design_turn(self, prompt: str, label: str) -> dict:
        self.r.status("planning", label)
        res, env = self.run_role("supervisor", prompt, label, 0, expect={"design", "question"})
        while env.get("type") == "question":
            follow = self.ask_human("supervisor", env)
            self.r.status("planning", label)
            res, env = self.run_role("supervisor", follow, label, 0, expect={"design", "question"})
        return env

    def _draft(self, prompt: str, label: str) -> dict:
        repos = self.repo_names()
        env = self._supervisor_design_turn(prompt, label)
        design = normalize_design(env)
        problems = validate(design, repos, self.has_frontend())
        tries = 0
        while problems and tries < 2:
            tries += 1
            self.r.timeline("judge", "Design incomplete", "; ".join(problems)[:300])
            self.r.msg(role="orchestrator", agent=None, kind="gate", ok=False, turn=0, content="Design refused: " + "; ".join(problems))
            env = self._supervisor_design_turn(design_fix_request(problems), "Supervisor is completing the design")
            design = normalize_design(env)
            problems = validate(design, repos, self.has_frontend())
        prev = self.design()
        design["work_packages"] = order_packages(design["work_packages"], design, self.component_kinds()) if not any("cycle" in p for p in problems) else design["work_packages"]
        design.update({"version": int(prev.get("version") or 0) + 1, "status": "in_review", "complexity": prev.get("complexity") or self.state.get("complexity") or {},
                       "trigger": prev.get("trigger") or self.state.get("design_reason", ""), "problems": problems,
                       "merge_order": merge_order(design, repos), "review_history": prev.get("review_history") or [],
                       "amendments": prev.get("amendments") or [], "review_response": env.get("review_response") or [], "time": now()})
        self.save_design(design)
        sup = self.role_agent("supervisor")[0]
        self.r.msg(role="supervisor", agent=sup, kind="plan", summary=f"System design v{design['version']}: {design.get('summary') or truncate(design.get('goal', ''), 120)}",
                   content=design_md(design), turn=0, subtype="design")
        self.r.timeline("supervisor", f"Design v{design['version']} written",
                        f"{len(design['contracts'])} contracts · {len(design['data_model'])} data changes · {len(design['work_packages'])} packages")
        return design

    # ------------------------------------------------------------ review
    def design_reviewer(self):
        """(role, session key): the reviewer agent, else a different agent than the supervisor, else a fresh supervisor session."""
        sup = self.role_agent("supervisor")[0]
        if self.role_agent("reviewer")[0]:
            return "reviewer", "design_reviewer"
        if self.role_agent("worker")[0] and self.role_agent("worker")[0] != sup:
            return "worker", "design_reviewer"
        return "supervisor", "design_reviewer"

    def review_design(self, design: dict, round_no: int) -> dict:
        from . import judge, systemmap
        role, key = self.design_reviewer()
        agent = self.role_agent(role)[0]
        try:
            map_text = systemmap.prompt_block([self.task.get("repo")] + [r["repo"] for r in getattr(self, "related", None) or []])
        except Exception:
            map_text = ""
        prev = design.get("review_history") or []
        previous = ""
        if prev:
            last = prev[-1]
            previous = "\n".join(f"- {f.get('id')} [{f.get('severity')}] {f.get('section', '')}: {f.get('problem', '')}" for f in last.get("findings") or [])
            if design.get("review_response"):
                previous += "\nSupervisor: " + "; ".join(f"{r.get('finding')}: {r.get('response')}" for r in design["review_response"] if isinstance(r, dict))
        prompt = review_prompt(self.task, design_md(design), self.repos_text() if hasattr(self, "repos_text") else "", map_text, round_no, previous)
        self.r.status("reviewing", f"Design review · round {round_no}")
        self.handoff("orchestrator", role, f"Design review requested · round {round_no}", design.get("summary") or design.get("goal", ""), subtype="review_request")
        try:
            res, env = self.run_role(role, prompt, f"Design review · round {round_no}", 0, expect={"review"}, session_key=key)
        except Exception as e:
            from .runner import Interrupted, Stopped
            if isinstance(e, (Stopped, Interrupted)):
                raise
            self.record_blocked([{"check": "Design review", "action_required": True, "reason": truncate(str(e), 300),
                                  "impact": "the design was not independently reviewed"}])
            return {"verdict": "SKIPPED", "summary": f"design review could not run: {truncate(str(e), 200)}", "findings": [], "round": round_no}
        findings = judge.normalize_findings(env.get("findings") if isinstance(env.get("findings"), list) else [])
        for i, f in enumerate(findings, 1):
            raw = (env.get("findings") or [])[i - 1] if i - 1 < len(env.get("findings") or []) else {}
            f["id"] = (raw.get("id") if isinstance(raw, dict) else "") or f"F{i}"
            f["section"] = (raw.get("section") if isinstance(raw, dict) else "") or f.get("file", "")
        blocking, minor = judge.split_findings(findings)
        verdict = "FAIL" if blocking else ("PASS" if findings or str(env.get("verdict", "")).upper() == "PASS" else str(env.get("verdict", "FAIL")).upper())
        review = {"round": round_no, "verdict": verdict, "summary": _s(env.get("summary"), 400), "findings": findings, "agent": agent, "time": now()}
        self.r.msg(role=role, agent=agent, kind="review", verdict=verdict, summary="Design review: " + review["summary"], findings=findings, turn=0, subject="design")
        self.r.timeline(role, f"Design review round {round_no}: {verdict}", review["summary"])
        return review

    # ------------------------------------------------------------ approval
    def design_approval_wanted(self, design: dict) -> tuple[bool, str]:
        wf = self.task.get("workflow") or {}
        mode = str(wf.get("design_approval") or self.cfg.get("design_approval") or "auto").lower()
        if mode == "auto" and str(wf.get("question_policy") or self.cfg.get("question_policy") or "blocked").lower() == "blocked":
            return False, "Relay only interrupts you when a task is blocked; the reviewed design is approved automatically"
        if not self.allow_questions:
            return False, "agent questions are disabled (unattended)"
        ap = getattr(self.m, "autopilot", None)
        if ap and ap.quiet():
            return False, "quiet hours"
        if mode == "off":
            return False, "design approval is off for this task"
        if mode == "on":
            return True, "design approval is on for this task"
        complex_ = (design.get("complexity") or {}).get("level") == "complex"
        multi = bool(getattr(self, "related", None)) or len({p.get("repo") for p in design.get("work_packages") or [] if p.get("repo")}) > 1
        return (complex_ or multi), ("complex or multi-repository design" if (complex_ or multi) else "a single-repository design that is not complex")

    def ask_design_approval(self, design: dict) -> dict:
        qid = new_id("a")
        text = design_md(design, self.task.get("name") or "")
        summary = f"**System design v{design['version']}**: {design.get('goal') or design.get('summary')}\n\n" + \
                  f"{len(design.get('contracts') or [])} contracts · {len(design.get('data_model') or [])} data changes · {len(design.get('work_packages') or [])} work packages" + \
                  (f" · merge order {' → '.join(design.get('merge_order') or [])}" if len(design.get("merge_order") or []) > 1 else "") + \
                  f"\n\nDesign review: {(design.get('review') or {}).get('verdict', 'not run')}"
        qmsg = self.r.msg(role="orchestrator", agent=None, kind="approval", subject="design", qid=qid, content=summary, answered=False, turn=0)
        self.m.ask_user(self.tid, {"id": qid, "kind": "design_approval", "from": "orchestrator", "question": "Approve the system design?",
                                   "summary": summary, "design_md": truncate(text, 30000), "design_version": design["version"],
                                   "message_id": qmsg["id"], "time": now()})
        self.r.status("needs_input", "Waiting for your approval of the system design")
        self.m.notify("warning", "Design approval needed", f"{self.task.get('name')} · design v{design['version']}", self.tid, kind="approval")
        ans = self.r.wait_for_answer(qid) or {}
        self.m.clear_pending(self.tid)
        approved = bool((ans.get("extra") or {}).get("approved", True))
        self.r.msg_update(qmsg["id"], answered=True, approved=approved, answer=ans.get("text") or "")
        if ans.get("text"):
            self.r.msg(role="user", agent=None, kind="user", content=ans["text"], to="supervisor", reply_to=qid, turn=0)
        return {"approved": approved, "note": (ans.get("text") or "").strip()}

    # ------------------------------------------------------------ the phase
    def design_phase(self):
        sup_label = self.role_agent("supervisor")[0]
        max_rev = max(0, int(self.cfg.get("design_max_revisions", 2)))
        while self.state.get("phase") == "design":
            self.r.check_stop()
            stage = self.state.get("design_stage") or "draft"
            if stage == "draft":
                from . import systemmap
                try:
                    map_text = systemmap.prompt_block([self.task.get("repo")] + [r["repo"] for r in getattr(self, "related", None) or []])
                except Exception:
                    map_text = ""
                stack = getattr(self, "stack", None)
                stack_text = ", ".join(f"`{c['name']}`" + ("" if c.get("required", True) else " (optional)") for c in (stack or {}).get("checks") or []) if stack else ""
                prompt = self.state.pop("design_prompt", "") or design_request(self.state.get("design_reason", ""), self.repos_text(), map_text,
                                                                                 stack_text, self.has_frontend(), bool(getattr(self, "related", None)),
                                                                                 self.take_guidance("supervisor"))
                self._draft(prompt, "Supervisor is designing the system")
                self.state["design_stage"] = "review"
                self.save()
                continue
            if stage == "review":
                design = self.design()
                rnd = len(design.get("review_history") or []) + 1
                review = self.review_design(design, rnd)
                design["review"] = review
                design["review_history"] = (design.get("review_history") or []) + [review]
                self.add_followups([f for f in review["findings"] if f.get("severity") != "blocking"], f"design review round {rnd}")
                blocking = [f for f in review["findings"] if f.get("severity") == "blocking"]
                if blocking:
                    n = int(self.state.get("design_revisions") or 0)
                    findings_text = "\n".join(f"- {f['id']} [blocking] {f.get('section', '')}: {f.get('problem', '')}" + (f"\n  fix: {f['fix']}" if f.get("fix") else "") for f in blocking)
                    if n < max_rev:
                        design["status"] = "changes_requested"
                        self.save_design(design)
                        self.state.update({"design_stage": "draft", "design_revisions": n + 1, "design_prompt": design_revision_request(findings_text)})
                        self.r.timeline("judge", f"Design revision {n + 1}/{max_rev}", f"{len(blocking)} blocking finding(s)")
                        self.save()
                        continue
                    self.save_design(design)
                    question = (f"The design reviewer still blocks the design after {n} revision(s):\n\n{findings_text}\n\n"
                                "Proceed with the design as it is (the findings become follow-ups), type guidance for the supervisor to revise it once more, or stop.")
                    key, text = self.escalate("Design review still blocking", question,
                                              {"accept": "Proceed with the design, list the findings as follow-ups", "guidance": "Give guidance", "stop": "Stop the task"},
                                              auto="accept",
                                              work=("guidance", "Revise the design to resolve each blocking finding above; where you disagree, "
                                                                "say why in one line and keep the rest."))
                    if key != "accept":
                        self.state.update({"design_stage": "draft", "design_revisions": max(0, max_rev - 1),
                                           "design_prompt": design_revision_request(findings_text, text, "the human")})
                        self.save()
                        continue
                    self.add_followups([{**f, "severity": "should_fix"} for f in blocking], "design review (accepted)")
                self.save_design(design)
                self.state["design_stage"] = "approval"
                self.save()
                continue
            if stage == "approval":
                design = self.design()
                want, why = self.design_approval_wanted(design)
                if want:
                    design["status"] = "awaiting_approval"
                    self.save_design(design)
                    ans = self.ask_design_approval(design)
                    if not ans["approved"]:
                        design["status"] = "changes_requested"
                        design["review_history"] = design.get("review_history") or []
                        self.save_design(design)
                        self.r.timeline("user", "Design changes requested", truncate(ans["note"], 200))
                        self.state.update({"design_stage": "draft", "design_revisions": 0,
                                           "design_prompt": design_revision_request("(no reviewer findings; the human reviewed the design)", ans["note"] or "Revise the design.", "the human")})
                        self.save()
                        continue
                    design["approval"] = {"by": "human", "time": now(), "note": ans["note"]}
                    self.r.timeline("user", "Design approved", truncate(ans["note"], 200))
                else:
                    design["approval"] = {"by": "relay", "time": now(), "note": f"auto-approved: {why}"}
                    self.r.timeline("judge", "Design auto-approved", why)
                    self.r.msg(role="orchestrator", agent=None, kind="notice", turn=0,
                               content=f"Design v{design['version']} approved automatically ({why}) after the design review ({(design.get('review') or {}).get('verdict', 'not run')}).")
                design["status"] = "approved"
                self.save_design(design)
                self.start_implementation(design, (design.get("approval") or {}).get("note") if design["approval"]["by"] == "human" else "")
                return

    def start_implementation(self, design: dict, note: str = ""):
        """Turn the approved design into the plan's work packages and dispatch the first one."""
        from . import judge
        criteria = judge.normalize_acceptance(design.get("acceptance"))
        if criteria:
            user_rows = [c for c in self.criteria() if c.get("source") == "user"]
            if user_rows:
                criteria = judge.edit_acceptance(criteria, {"add": user_rows})
            self.set_criteria(criteria)
        plan = dict(self.state.get("plan") or {})
        packages = design.get("work_packages") or []
        plan["work_packages"] = [{"id": p["id"], "repo": p.get("repo", ""), "summary": p["summary"], "depends_on": p.get("depends_on") or [],
                                  "implements": p.get("implements") or [], "layer": p.get("layer", "")} for p in packages]
        plan["system_design"] = legacy_system_design(design)
        if criteria:
            plan["acceptance"] = judge.criterion_texts(criteria)
        self.state["plan"] = plan
        self.m.set_meta(self.tid, plan=plan, system_design=plan["system_design"])
        first = packages[0] if packages else None
        text = (first.get("instruction") or first.get("summary")) if first else "Implement the approved design."
        if note:
            text += f"\n\nHUMAN NOTE ON THE DESIGN\n{note}"
        env = {"package": first["id"], "repo": first.get("repo", "")} if first else {}
        self.state.update({"phase": "dialogue", "turn": 1, "awaiting": "worker", "instruction": self.package_prefix(env) + text,
                           "instruction_kind": "instruction", "instruction_summary": (first or {}).get("summary", ""), "design_stage": "approved"})
        order = " → ".join(f"{p['id']} [{p.get('repo') or 'primary'}]" for p in packages)
        self.state["judge_note"] = (self.state.get("judge_note", "") + "\n\n" if self.state.get("judge_note") else "") + \
            (f"ORCHESTRATOR · design v{design['version']} is approved ({design['approval']['by']}). Relay dispatched {first['id'] if first else 'the first package'} to the worker. "
             f"Implement the packages in this order: {order}. Name the package in every instruction (\"package\":\"W2\"); Relay attaches its contracts."
             + (f"\nHuman note: {note}" if note else ""))
        self.r.timeline("system", "Implementation starts from the design", order)
        self.save()

    # ------------------------------------------------------------ during implementation
    def package_prefix(self, env: dict) -> str:
        head = super().package_prefix(env)
        design = self.task_meta().get("design") or {}
        if design.get("status") != "approved":
            return head
        pkg_id = str((env or {}).get("package") or "").strip()
        pkg = next((p for p in design.get("work_packages") or [] if p["id"] == pkg_id), None)
        if not pkg:
            return head
        if not head:
            head = f"WORK PACKAGE {pkg_id}\n"
            self.state["current_package"] = pkg_id
        return head + contract_slice(design, pkg, stack=bool(getattr(self, "stack", None))) + "\n\n"

    def context_text(self, role: str) -> str:
        base = super().context_text(role)
        design = self.task_meta().get("design") or {}
        if design.get("status") != "approved":
            return base
        # The approved design replaces the plan-time system design block.
        legacy = design_blockless(base)
        if role == "reviewer":
            return "\n\n".join(p for p in (legacy, design_block(design, full=True), FINAL_REVIEW_BLOCK) if p)
        return "\n\n".join(p for p in (legacy, design_block(design, full=(role == "supervisor"))) if p)

    def design_judge_text(self) -> str:
        design = self.task_meta().get("design") or {}
        return conformance_note(design) if design.get("status") == "approved" else ""

    def note_amendments(self, env: dict, role: str):
        items = amendments_from(env)
        if not items:
            return
        design = self.task_meta().get("design") or {}
        if not design:
            return
        design, added = add_amendments(design, items, role, str((env or {}).get("package") or self.state.get("current_package") or ""))
        if not added:
            return
        self.save_design(design)
        agent = self.role_agent(role)[0] if role in ("supervisor", "worker", "reviewer") else None
        for a in added:
            self.r.msg(role=role, agent=agent, kind="notice", turn=self.state.get("turn"),
                       content=f"Design amendment {a['id']} ({a.get('ref') or 'design'}): {a['change']}" + (f" · why: {a['reason']}" if a.get("reason") else ""))
            self.r.timeline(role, f"Design amendment {a['id']}", truncate(f"{a.get('ref', '')} {a['change']}", 200))
        if role == "worker":
            self.state["judge_note"] = (self.state.get("judge_note", "") + "\n\n" if self.state.get("judge_note") else "") + \
                "ORCHESTRATOR · the worker proposed design amendments: " + "; ".join(f"{a['id']} {a.get('ref', '')}: {a['change']}" for a in added) + \
                ". Accept them (they stay recorded) or revise the package back to the design; if an amendment changes a contract, make sure every other side follows it."

    # ------------------------------------------------------------ delivery
    def write_design_doc(self):
        """Commit the approved design into the primary repository (docs/designs/…), or remember to comment it on the PR."""
        design = self.task_meta().get("design") or {}
        mode = str(self.cfg.get("design_doc_mode") or "commit")
        if not design or design.get("status") != "approved" or mode == "off":
            return None
        if mode == "comment":
            info = {"mode": "comment"}
        else:
            rel = (self.task_meta().get("design_doc") or {}).get("path") or doc_path(self.task)
            write_text(Path(self.wt) / rel, design_md(design, self.task.get("name") or ""))
            info = {"mode": "commit", "path": rel, "repo": Path(self.task.get("repo") or "").name}
        self.m.set_meta(self.tid, design_doc=info)
        return info

    def design_doc_url(self, pr_url: str = ""):
        info = dict(self.task_meta().get("design_doc") or {})
        if info.get("path") and getattr(self, "repo_full", None):
            info["url"] = f"https://github.com/{self.repo_full}/blob/{self.branch}/{info['path']}"
            self.m.set_meta(self.tid, design_doc=info)
        return info

    def comment_design(self, pr: dict):
        info = self.task_meta().get("design_doc") or {}
        if info.get("mode") != "comment" or not pr.get("number") or not getattr(self, "repo_full", None):
            return
        design = self.task_meta().get("design") or {}
        f = Path(self.run_dir) / "DESIGN_COMMENT.md"
        write_text(f, design_md(design, self.task.get("name") or ""))
        try:
            self.r.run_shell_args(["gh", "pr", "comment", str(pr["number"]), "--repo", self.repo_full, "--body-file", str(f)],
                                  cwd=self.wt, role="github", title="Post the design on the pull request")
            self.m.set_meta(self.tid, design_doc={**info, "url": pr.get("url")})
        except Exception as e:
            self.r.timeline("github", "Could not post the design comment", truncate(str(e), 200))

    def changeset_text(self) -> str:
        return changeset_md(self.task_meta(), self.cfg)

    def write_changeset_preview(self):
        """PR bodies as they are (or would be) sent, per repository, also when nothing is pushed."""
        t = self.task_meta()
        section = changeset_md(t, self.cfg)
        if not section:
            return
        self.artifact("changeset", "CHANGESET.md", section)
        self.m.set_meta(self.tid, changeset=changeset(t))


def design_blockless(text: str) -> str:
    """Drop the plan-time SYSTEM DESIGN / WORK PACKAGES block from a context text (the approved design replaces it)."""
    if not text:
        return ""
    out, skip = [], False
    for block in text.split("\n\n"):
        if block.startswith("SYSTEM DESIGN (agreed at planning") or block.startswith("WORK PACKAGES (in order"):
            continue
        out.append(block)
    return "\n\n".join(out)
