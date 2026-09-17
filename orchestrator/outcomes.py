"""Outcome dataset: one normalized, append-only record per finished task run.

The scorecard (orchestrator/scorecard.py) answers "how well did this go"; the outcome record keeps
everything the learning engine needs to answer "what would make the next one go better":

    what was asked    repositories, task type, request size, design used, related repositories
    who did it        the team (agent, model, effort per role) and a stable team key
    what they saw     prompt and rule version hashes, lessons injected, playbook used
    how it went       tokens, cost, duration, judge events (gate refusals, blocking findings, revisions,
                      escalations), verification first pass, end-to-end results, human interventions
    how it ended      outcome, failure category, score, success, and the post-merge signal
                      (merged, closed, human commits, reverted within 14 days)
    the forecast      the pre-flight risk prediction made when the task was created (for calibration)

Layout: DATA_DIR/learning/outcomes.jsonl. A task run (task id + start time) is re-recorded when its
signal changes (a pull request merged a week later, a revert); readers keep the last line per run.
Nothing is rewritten in place, so the history of what Relay believed at each point is preserved.

`build` is pure (no I/O) so the dataset can be rebuilt from task records and message logs.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from .util import DATA_DIR, now

VERSION = 1
LEARNING_DIR = DATA_DIR / "learning"
OUTCOMES_FILE = LEARNING_DIR / "outcomes.jsonl"

_lock = threading.RLock()

# Judge escalations (orchestrator/pipeline.py `escalate` titles): moments the team could not settle alone.
ESCALATIONS = {"Acceptance criteria not proven", "A blocking finding keeps coming back", "Revision produced no change",
               "Work-package budget reached", "Review rounds used up", "Verification still failing"}

# Signals that decide whether a new line is worth appending (anything else is bookkeeping).
_SIGNAL_KEYS = ("outcome", "failure_category", "score", "success", "post_merge", "lessons_injected", "autopsy_cause", "acceptance_unmet")


# ----------------------------------------------------------------------------- helpers
def team_key(team: dict) -> str:
    """supervisor>worker>reviewer, each agent[:model][@effort]. Blank model/effort mean the CLI default."""
    parts = []
    for r in ("supervisor", "worker", "reviewer"):
        x = (team or {}).get(r) or {}
        a = (x.get("agent") or "").strip()
        if not a:
            continue
        s = a + (f":{x['model']}" if x.get("model") else "") + (f"@{x['effort']}" if x.get("effort") else "")
        parts.append(s)
    return ">".join(parts)


def team_from_key(key: str) -> dict:
    roles = {}
    names = ["supervisor", "worker", "reviewer"]
    for i, part in enumerate((key or "").split(">")[:3]):
        if not part:
            continue
        effort = ""
        if "@" in part:
            part, effort = part.rsplit("@", 1)
        agent, _, model = part.partition(":")
        roles[names[i]] = {"agent": agent, "model": model, "effort": effort}
    return roles


def team_label(key: str, agents: dict | None = None) -> str:
    agents = agents or {}
    out = []
    for r, x in team_from_key(key).items():
        lbl = (agents.get(x["agent"]) or {}).get("label", x["agent"].capitalize())
        extra = " ".join(v for v in (x["model"], x["effort"]) if v)
        out.append(lbl + (f" ({extra})" if extra else ""))
    return " → ".join(out)


_VAGUE = re.compile(r"\b(somehow|maybe|perhaps|something|whatever|etc|and so on|tbd|improve|better|nicer|clean ?up|properly|some kind of|kind of|sort of|as needed|if possible|stuff)\b", re.I)


def request_signals(requirements: str, issue: str = "", repos: int = 1) -> dict:
    """What is known about a request before anyone works on it."""
    text = requirements or ""
    chars = len(text.strip())
    bullets = len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", text))
    vague = sorted({m.group(0).lower() for m in _VAGUE.finditer(text)})
    questions = text.count("?")
    size = size_bucket(chars, bullets, repos)
    return {"chars": chars, "bullets": bullets, "vague_terms": vague[:8], "question_marks": questions,
            "has_issue": bool(str(issue or "").strip()), "repos": max(1, int(repos or 1)), "size": size}


def size_bucket(chars: int, bullets: int = 0, repos: int = 1) -> str:
    if repos > 1 or chars > 1500 or bullets >= 8:
        return "large"
    if chars < 300 and bullets <= 2:
        return "small"
    return "medium"


def file_hash(paths) -> str:
    h = hashlib.sha1()
    for p in sorted(Path(x) for x in paths):
        try:
            h.update(p.name.encode())
            h.update(p.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


def prompt_versions(app_dir: Path, rule_names: list[str] | None = None) -> dict:
    """Hashes of what shaped the prompts: every rule file, the roles' rule sets, and the protocol code."""
    rules_dir = Path(app_dir) / "rules"
    rules = sorted(rules_dir.glob("*.md"))
    out = {"rules": file_hash(rules), "protocol": file_hash([Path(app_dir) / "orchestrator" / "protocol.py",
                                                            Path(app_dir) / "orchestrator" / "pipeline.py"])}
    if rule_names:
        out["role_rules"] = file_hash([rules_dir / f"{n}.md" for n in rule_names])
    return out


# ----------------------------------------------------------------------------- build
def build(task: dict, card: dict, messages: list[dict] | None = None) -> dict:
    """The outcome record of one finished run. Pure. Works with a card alone when the task is gone."""
    task = task or {}
    messages = messages or []
    run_start = task.get("started_at") or card.get("started_at") or ""
    msgs = [m for m in messages if not run_start or not m.get("time") or (m.get("time") or "") >= run_start[:19]]
    events = [e for e in task.get("events") or [] if not run_start or (e.get("time") or "") >= run_start[:19]]

    gates = [m for m in msgs if m.get("kind") == "gate" and m.get("ok") is False]
    reviews = [m for m in msgs if m.get("kind") == "review" and m.get("subject") != "design"]
    design_reviews = [m for m in msgs if m.get("kind") == "review" and m.get("subject") == "design"]
    blocking = sum(1 for m in reviews for f in (m.get("findings") or []) if str(f.get("severity") or "blocking").lower() == "blocking")
    design = task.get("design") or {}
    verifs = [m for m in msgs if m.get("kind") == "verification"]
    e2e = [i for m in verifs for i in (m.get("items") or []) if i.get("kind") == "e2e"]
    last_e2e = {}
    for i in e2e:
        last_e2e[i.get("command")] = i
    plan = task.get("plan") or {}
    ds = task.get("diffstat") or {}
    repos = task.get("repos") or []
    req = request_signals(task.get("requirements") or "", task.get("issue") or "", len(repos) or 1)
    pr = card.get("pr") or {}
    tm = card.get("team") or {}
    rec = {
        "v": VERSION,
        "task_id": card.get("task_id") or task.get("id"),
        "run_key": f"{card.get('task_id') or task.get('id')}@{run_start}",
        "recorded_at": now(),
        "name": card.get("name") or task.get("name"),
        "started_at": card.get("started_at"), "finished_at": card.get("finished_at"),
        "repo": card.get("repo") or "", "repo_label": card.get("repo_label") or "", "repo_path": card.get("repo_path") or task.get("repo") or "",
        "repos": [r.get("github_repo") or r.get("repo") for r in repos if r.get("role") != "primary"],
        "template": card.get("template") or task.get("template") or "feature",
        "priority": task.get("priority") or "normal",
        "tags": card.get("tags") or [],
        "source": card.get("source") or "manual",
        "follow_up": bool(task.get("follow_up_of")),
        "size": {
            "request": req,
            "bucket": req["size"],
            "files": int(ds.get("files") or task.get("changed_count") or 0),
            "lines": int(ds.get("insertions") or 0) + int(ds.get("deletions") or 0),
            "work_packages": int(card.get("work_packages") or 0),
            "acceptance": len(plan.get("acceptance") or []),
            "design_used": bool(int(design.get("version") or 0) or plan.get("system_design") or task.get("system_design")),
        },
        "design": {"mode": (task.get("workflow") or {}).get("design_mode"), "status": design.get("status"), "version": design.get("version"),
                   "complexity": (design.get("complexity") or {}).get("level") if isinstance(design.get("complexity"), dict) else design.get("complexity"),
                   "review_rounds": len(design_reviews),
                   "blocking_findings": sum(1 for m in design_reviews for f in (m.get("findings") or []) if str(f.get("severity") or "blocking").lower() == "blocking")},
        "components": _components(task),
        "team": tm, "team_key": team_key(tm), "pairing": card.get("pairing") or "",
        "team_source": (task.get("team_pick") or {}).get("source") or task.get("team_source") or "manual",
        "explored": bool((task.get("team_pick") or {}).get("explored")),
        "versions": task.get("prompt_versions") or {},
        "lessons_injected": list(task.get("lessons_used") or []),
        "playbook_used": bool(task.get("playbook_used")),
        "tokens": card.get("tokens") or {}, "cost_usd": float(card.get("cost_usd") or 0), "cost_estimated": bool(card.get("cost_estimated")),
        "duration_seconds": card.get("duration_seconds"),
        "judge": {
            "done_refusals": sum(1 for m in gates if str(m.get("content") or "").startswith("Done refused")),
            "revise_refusals": sum(1 for m in gates if str(m.get("content") or "").startswith("Revision refused")),
            "blocking_findings": blocking,
            "revisions": int(card.get("revisions") or 0),
            "review_rounds": int(card.get("review_rounds") or 0),
            "review_first_pass": card.get("review_first_pass"),
            "escalations": sum(1 for e in events if e.get("role") == "judge" and str(e.get("title") or "") in ESCALATIONS),
            "unchanged_revisions": sum(1 for e in events if str(e.get("title") or "") == "Revision produced no change"),
        },
        "verification": card.get("verification") or {},
        "blocked_checks": int(card.get("blocked_checks") or 0),
        "acceptance_unmet": sum(1 for c in task.get("acceptance") or [] if c.get("required", True) and c.get("status") not in ("met", "waived")),
        "blocked_action_required": int(card.get("blocked_action_required") or 0),
        "e2e": {"runs": len(e2e), "final": [{"name": k, "passed": bool(v.get("passed"))} for k, v in last_e2e.items()][:10],
                "final_ok": all(v.get("passed") for v in last_e2e.values()) if last_e2e else None},
        "human": card.get("human") or {},
        "agent_trouble": card.get("agent_trouble") or {},
        "outcome": card.get("outcome"), "failure_category": card.get("failure_category"),
        "score": int(card.get("score") or 0), "success": bool(card.get("success")),
        "post_merge": {"state": pr.get("state"), "merged_at": pr.get("merged_at"), "human_commits": pr.get("human_commits"),
                       "reverted": bool(pr.get("reverted")), "revert_sha": pr.get("revert_sha")} if pr else None,
        "prediction": _prediction(task),
        "autopsy_cause": (task.get("autopsy") or {}).get("cause"),
    }
    return rec


def _components(task: dict) -> list[str]:
    """Top-level areas the run touched or planned to touch: first two path segments of known files."""
    plan = task.get("plan") or {}
    files = list(plan.get("known_files") or [])
    for m in (task.get("changed_files") or []):
        files.append(m if isinstance(m, str) else (m.get("path") or ""))
    out = []
    for f in files:
        f = str(f).strip().lstrip("./")
        if not f:
            continue
        seg = "/".join(f.split("/")[:2]) if "/" in f else f
        if seg not in out:
            out.append(seg)
    return out[:20]


def _prediction(task: dict):
    pf = task.get("preflight") or {}
    risk = pf.get("risk") or {}
    if not risk:
        return None
    return {"level": risk.get("level"), "p_fail": risk.get("p_fail"), "factors": [f.get("id") for f in risk.get("factors") or []],
            "recommended_team": (pf.get("recommendation") or {}).get("team_key"), "followed": pf.get("followed")}


def failed(rec: dict, threshold: int = 60) -> bool:
    """For risk and calibration: a run counts as failed when it was not a success, scored under the threshold,
    or was delivered with required acceptance criteria still unproven."""
    return (not rec.get("success")) or int(rec.get("score") or 0) < threshold or int(rec.get("acceptance_unmet") or 0) > 0


# ----------------------------------------------------------------------------- store
def _signal(rec: dict) -> str:
    return json.dumps({k: rec.get(k) for k in _SIGNAL_KEYS}, sort_keys=True, default=str)


class OutcomeStore:
    def __init__(self, path: Path = OUTCOMES_FILE):
        self.path = Path(path)
        self._cache = None
        self._mtime = None

    def _load(self) -> dict:
        with _lock:
            try:
                st = self.path.stat()
                mtime = (st.st_mtime_ns, st.st_size)
            except OSError:
                return {}
            if self._cache is not None and self._mtime == mtime:
                return self._cache
            latest = {}
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("run_key"):
                        latest[r["run_key"]] = r
            self._cache, self._mtime = latest, mtime
            return latest

    def all(self) -> list[dict]:
        return sorted(self._load().values(), key=lambda r: r.get("finished_at") or "")

    def get(self, run_key: str):
        return self._load().get(run_key)

    def for_task(self, tid: str) -> list[dict]:
        return [r for r in self.all() if r.get("task_id") == tid]

    def append(self, rec: dict) -> bool:
        """Append when the run is new or its signal changed. Returns whether a line was written."""
        with _lock:
            prev = self._load().get(rec["run_key"])
            if prev and _signal(prev) == _signal(rec):
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._cache = None
            return True
