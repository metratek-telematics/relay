"""Per-repository playbooks: what a supervisor should know before planning work on a repository.

A playbook has five short sections:

    run          how to install, run and test it
    conventions  what the codebase expects (layout, style, what it is and what it talks to)
    gotchas      environment and testing traps seen before
    breakdown    how successful tasks here were typically split into work packages
    fixes        failures seen before and what fixed them

Sources
    seed     built without any agent from the system map (description, how it runs, dependencies), the
             repository environment (setup, services, checks, system packages), approved lessons by
             category, successful outcome records (work packages, acceptance counts) and applied
             improvement proposals. Re-seeding refreshes every section a person has not edited.
    agent    a periodic cheap agent turn (`refresh_prompt` / `parse_refresh`) rewrites the unedited
             sections from the seed plus digests of recent successful and failed runs.
    person   any section can be edited on the Learning page; an edited section is never overwritten
             (the agent's suggestion is kept next to it for review).

Only the supervisor's kickoff prompt gets the playbook (planning), capped at PROMPT_CHARS.
Layout: DATA_DIR/learning/playbooks/<slug>-<hash>.json
"""
from __future__ import annotations

import hashlib
import json
import threading
from statistics import median

from .lesson_effect import categorize
from .outcomes import LEARNING_DIR
from .util import now, read_json, safe_slug, truncate, write_json

PLAYBOOKS_DIR = LEARNING_DIR / "playbooks"
SECTIONS = ("run", "conventions", "gotchas", "breakdown", "fixes")
SECTION_LABEL = {"run": "How to run and test", "conventions": "Conventions", "gotchas": "Gotchas",
                 "breakdown": "Typical work-package breakdown", "fixes": "Common failures and fixes"}
PROMPT_CHARS = 3500
_lock = threading.RLock()


def _file(repo: str):
    digest = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:8]
    return PLAYBOOKS_DIR / f"{safe_slug(repo.replace('/', '__').strip('_')[-60:]).strip('_')}-{digest}.json"


def load(repo: str) -> dict | None:
    return read_json(_file(repo), None) if repo else None


def all_playbooks() -> list[dict]:
    if not PLAYBOOKS_DIR.is_dir():
        return []
    return [d for d in (read_json(p, None) for p in sorted(PLAYBOOKS_DIR.glob("*.json"))) if d]


def save(pb: dict) -> dict:
    with _lock:
        pb["updated_at"] = now()
        write_json(_file(pb["repo"]), pb)
        return pb


def _bullets(rows) -> str:
    return "\n".join(f"- {r}" for r in rows if r)


# ----------------------------------------------------------------------------- seed (pure)
def seed_sections(repo_label: str, component: dict | None, env: dict | None, detected_checks: list[str], lessons: list[dict],
                  records: list[dict], proposals: list[dict]) -> dict:
    env = env or {}
    comp = component or {}
    run = []
    if env.get("system_packages"):
        run.append("System packages (installed by Relay before setup): " + " ".join(env["system_packages"]))
    if env.get("setup"):
        run.append(f"Setup: `{env['setup']}`")
    if env.get("services_up"):
        run.append(f"Services: `{env['services_up']}`" + (f" (stop: `{env['services_down']}`)" if env.get("services_down") else ""))
    checks = [c for c in env.get("checks") or []]
    if checks:
        run += [f"Check: `{c['command']}`" + ("" if c.get("required", True) else " (optional, informative)") for c in checks]
    elif detected_checks:
        run += [f"Check (detected): `{c}`" for c in detected_checks]
    if comp.get("runs"):
        run.append(f"Runs as: {comp['runs']}")
    if env.get("stack"):
        run.append(f"Integration stack: {env['stack']}")

    conv = []
    if comp.get("description"):
        conv.append(comp["description"])
    for d in comp.get("depends_on") or []:
        if d.get("status") == "approved":
            conv.append(f"Depends on {d['component']} via {d['via']}" + (f": {d['details']}" if d.get("details") else ""))
    for d in comp.get("used_by") or []:
        if d.get("status") == "approved":
            conv.append(f"Used by {d['component']} via {d['via']}")
    by_cat = {}
    for x in lessons:
        if x.get("enabled", True) is False or x.get("retired"):
            continue
        by_cat.setdefault(x.get("category") or categorize(x.get("text")), []).append(x["text"])
    conv += by_cat.get("repo_conventions", []) + by_cat.get("architecture", [])
    gotchas = by_cat.get("environment", []) + by_cat.get("testing", [])

    ok = [r for r in records if r.get("success") and int(r.get("score") or 0) >= 70]
    breakdown = []
    if ok:
        wps = [int((r.get("size") or {}).get("work_packages") or 0) for r in ok if (r.get("size") or {}).get("work_packages")]
        acc = [int((r.get("size") or {}).get("acceptance") or 0) for r in ok if (r.get("size") or {}).get("acceptance")]
        if wps:
            breakdown.append(f"Successful tasks here used {min(wps)}–{max(wps)} work packages (median {median(wps):g})"
                             + (f" and {median(acc):g} acceptance criteria" if acc else "") + f", over {len(ok)} task(s).")
        by_type = {}
        for r in ok:
            by_type.setdefault(r.get("template") or "feature", []).append(r)
        for typ, rows in sorted(by_type.items()):
            files = [int((r.get("size") or {}).get("files") or 0) for r in rows]
            breakdown.append(f"{typ}: {len(rows)} successful, typically {median(files):g} files changed" if any(files) else f"{typ}: {len(rows)} successful")
    revisions = [r for r in records if (r.get("judge") or {}).get("revisions", 0) >= 2]
    if revisions:
        breakdown.append(f"{len(revisions)} task(s) needed two or more revisions; state the acceptance evidence in each work package up front.")

    fixes = []
    for p in proposals:
        if p.get("status") == "applied":
            fixes.append(f"{p.get('cause_label') or p.get('cause') or 'Failure'}: {p.get('title')}")
    fails = {}
    for r in records:
        if r.get("autopsy_cause"):
            fails[r["autopsy_cause"]] = fails.get(r["autopsy_cause"], 0) + 1
    if fails:
        fixes.append("Root causes seen: " + ", ".join(f"{k} ×{v}" for k, v in sorted(fails.items(), key=lambda kv: -kv[1])))
    return {"run": _bullets(run), "conventions": _bullets(conv[:12]), "gotchas": _bullets(gotchas[:12]),
            "breakdown": _bullets(breakdown), "fixes": _bullets(fixes[:10])}


def merge_seed(pb: dict | None, repo: str, repo_label: str, sections: dict, source: str = "seed") -> dict:
    """Refresh unedited sections; an edited section keeps its text and stores the new one as a suggestion."""
    pb = pb or {"repo": repo, "repo_label": repo_label, "created_at": now(), "sections": {}}
    pb["repo_label"] = repo_label or pb.get("repo_label") or repo
    for s in SECTIONS:
        cur = pb["sections"].get(s) or {}
        new = (sections.get(s) or "").strip()
        if cur.get("edited"):
            if new and new != cur.get("text"):
                cur["suggestion"] = new
                cur["suggestion_source"] = source
        else:
            cur = {"text": new, "source": source, "edited": False, "updated_at": now()}
        pb["sections"][s] = cur
    pb[f"{source}_at"] = now()
    return pb


def edit(pb: dict, section: str, text: str) -> dict:
    if section not in SECTIONS:
        raise ValueError(f"Unknown playbook section {section}")
    pb["sections"][section] = {"text": truncate(str(text or "").strip(), 4000), "source": "person", "edited": bool(str(text or "").strip()),
                               "updated_at": now()}
    return pb


def prompt_block(pb: dict | None) -> str:
    if not pb:
        return ""
    parts = []
    for s in SECTIONS:
        t = ((pb.get("sections") or {}).get(s) or {}).get("text") or ""
        if t.strip():
            parts.append(f"{SECTION_LABEL[s]}:\n{t.strip()}")
    if not parts:
        return ""
    body = truncate("\n\n".join(parts), PROMPT_CHARS)
    return ("PLAYBOOK FOR THIS REPOSITORY\nDistilled by Relay from earlier tasks here and maintained by the operator. Use it when planning "
            "and splitting the work; the request and the rules take precedence.\n" + body)


# ----------------------------------------------------------------------------- agent refresh
def refresh_prompt(pb: dict, records: list[dict], retros: list[dict]) -> str:
    cur = {s: ((pb.get("sections") or {}).get(s) or {}).get("text") or "" for s in SECTIONS}
    runs = []
    for r in records[-12:]:
        runs.append(f"- {r.get('template')} · score {r.get('score')} · {'success' if r.get('success') else 'not a success'}"
                    f" · work packages {(r.get('size') or {}).get('work_packages')} · revisions {(r.get('judge') or {}).get('revisions')}"
                    f" · files {(r.get('size') or {}).get('files')}" + (f" · root cause {r['autopsy_cause']}" if r.get("autopsy_cause") else "")
                    + f" · {truncate(r.get('name') or '', 80)}")
    notes = []
    for x in retros[-8:]:
        for k in ("what_went_well", "what_went_wrong", "root_causes"):
            notes += [f"- [{k.replace('_', ' ')}] {truncate(v, 200)}" for v in (x.get(k) or [])[:3]]
    return f"""You maintain a short playbook for one code repository ({pb.get('repo_label') or pb.get('repo')}) that a supervisor agent reads before planning a task there.

Do not use any tools. Rewrite each section from the CURRENT PLAYBOOK and the evidence below: keep what is still true, merge duplicates,
drop anything generic, add only what the evidence supports. Each section is at most 8 bullet lines of one sentence ("- ..."), empty if nothing is known.
Never include secrets, tokens, credentials or personal data.

Reply with ONLY one fenced ```json block: {{"type":"playbook","sections":{{"run":"- ...","conventions":"- ...","gotchas":"- ...","breakdown":"- ...","fixes":"- ..."}}}}

CURRENT PLAYBOOK
{json.dumps(cur, indent=1)}

RECENT RUNS (oldest first)
{chr(10).join(runs) or '(none)'}

RETROSPECTIVE NOTES
{chr(10).join(notes) or '(none)'}
"""


def parse_refresh(text: str) -> dict | None:
    from . import protocol
    env = protocol.parse_envelope(text or "")
    if not env or not isinstance(env.get("sections"), dict):
        return None
    out = {}
    for s in SECTIONS:
        v = env["sections"].get(s)
        if isinstance(v, list):
            v = "\n".join(f"- {str(x).lstrip('- ')}" for x in v)
        out[s] = truncate(str(v or "").strip(), 2000)
    return out
