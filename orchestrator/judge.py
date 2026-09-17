"""The supervisor as a judge.

Pure helpers the pipeline uses to hold the team to an acceptance contract:
- normalise the plan's acceptance criteria and the supervisor's per-criterion verdicts;
- gate `done` on evidence, with Relay's own verification overriding claimed evidence;
- classify findings by severity so only blocking ones cost another round;
- fingerprint findings so a finding that keeps coming back is escalated instead of looped on;
- fingerprint the worktree so a revision that changed nothing is noticed.

Everything here tolerates loose, hand-written envelopes and old task state (no acceptance contract).
"""
from __future__ import annotations

import hashlib
import re

from .util import quiet

SEVERITIES = ("blocking", "should_fix", "nit")
STATUSES = ("unmet", "met", "waived")
VERIFY_KINDS = ("test", "command", "screenshot", "inspection")

# A finding the worker was asked to fix this many times that still comes back goes to the human.
MAX_FIX_ATTEMPTS = 2

_SEVERITY_ALIASES = {
    "blocking": "blocking", "blocker": "blocking", "block": "blocking", "critical": "blocking", "high": "blocking",
    "major": "blocking", "error": "blocking", "must": "blocking", "must_fix": "blocking", "required": "blocking",
    "should_fix": "should_fix", "should": "should_fix", "medium": "should_fix", "minor": "should_fix", "warning": "should_fix",
    "suggestion": "should_fix", "improvement": "should_fix", "non_blocking": "should_fix", "nonblocking": "should_fix",
    "nit": "nit", "nitpick": "nit", "low": "nit", "style": "nit", "trivial": "nit", "info": "nit", "optional": "nit", "cosmetic": "nit",
}
_STATUS_ALIASES = {
    "met": "met", "pass": "met", "passed": "met", "done": "met", "ok": "met", "yes": "met", "satisfied": "met", "true": "met", "complete": "met",
    "waived": "waived", "waive": "waived", "skipped": "waived", "n/a": "waived", "na": "waived", "not_applicable": "waived",
}
_TRIVIAL_EVIDENCE = {"yes", "done", "ok", "okay", "verified", "tested", "works", "n/a", "na", "see above", "see report",
                     "confirmed", "met", "pass", "passed", "true", "checked", "looks good", "lgtm", "as above"}
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "it", "this", "that", "with", "not",
         "no", "does", "do", "should", "must", "when", "if", "as", "at", "by", "from", "but", "still", "still", "which", "into"}


def _s(v) -> str:
    return str(v if v is not None else "").strip()


def _key(v) -> str:
    return re.sub(r"[\s\-]+", "_", _s(v).lower())


# ----------------------------------------------------------------------------- acceptance contract
def verify_kind(how: str) -> str:
    low = _s(how).lower()
    for k in VERIFY_KINDS:
        if low.startswith(k):
            return k
    if re.search(r"\b(pytest|jest|vitest|unittest|npm (run )?test|go test|cargo test|spec)\b", low):
        return "test"
    if re.search(r"screenshot|visual|render", low):
        return "screenshot"
    if "`" in low or re.search(r"\b(run|npm|python|node|make|curl|build|lint)\b", low):
        return "command"
    return "inspection"


def normalize_acceptance(raw, source="supervisor") -> list[dict]:
    """Criteria as {id, criterion, how_to_verify, kind, required, status, evidence}. Strings become required criteria."""
    out, seen = [], set()
    for i, item in enumerate(raw or [], 1):
        if isinstance(item, dict):
            text = _s(item.get("criterion") or item.get("text") or item.get("description"))
            how = _s(item.get("how_to_verify") or item.get("verify") or item.get("how"))
            required = item.get("required", True)
            required = required if isinstance(required, bool) else _key(required) not in ("false", "no", "0", "optional")
            cid = _s(item.get("id"))
        else:
            text, how, required, cid = _s(item), "", True, ""
        if not text:
            continue
        if not cid or cid in seen:
            n = len(out) + 1
            while f"A{n}" in seen:
                n += 1
            cid = f"A{n}"
        seen.add(cid)
        status = _STATUS_ALIASES.get(_key(item.get("status")), "unmet") if isinstance(item, dict) else "unmet"
        out.append({"id": cid, "criterion": text, "how_to_verify": how or "inspection", "kind": verify_kind(how),
                    "required": bool(required), "status": status if status in STATUSES else "unmet",
                    "evidence": _s(item.get("evidence")) if isinstance(item, dict) else "", "source": source})
    return out


def criterion_texts(criteria) -> list[str]:
    return [c["criterion"] for c in criteria or []]


def normalize_results(raw) -> list[dict]:
    """The supervisor's per-criterion verdicts. Accepts a list of objects or an {id: status|object} map."""
    rows = []
    if isinstance(raw, dict):
        raw = [{"id": k, **(v if isinstance(v, dict) else {"status": v})} for k, v in raw.items()]
    for r in raw or []:
        if not isinstance(r, dict) or not _s(r.get("id")):
            continue
        rows.append({"id": _s(r.get("id")), "status": _STATUS_ALIASES.get(_key(r.get("status")), "unmet"),
                     "evidence": _s(r.get("evidence"))})
    return rows


def concrete_evidence(text: str) -> bool:
    """Evidence must point at something checkable: a command and its result, a file:line, a path, a count."""
    t = _s(text)
    if len(t) < 12 or t.lower().strip(" .!") in _TRIVIAL_EVIDENCE:
        return False
    return bool(re.search(r"`|/|\\|\$ |\w\.\w{1,5}\b|:\d+|\d", t))


def failed_checks(verification: dict | None) -> list[dict]:
    """Checks Relay itself ran that failed and did not already fail on the starting commit."""
    return [i for i in (verification or {}).get("items") or [] if not i.get("ok") and not i.get("pre_existing")]


def apply_results(criteria, results, by="supervisor") -> list[dict]:
    """Merge verdicts into the contract. A human's waiver or verdict is never overwritten by an agent."""
    index = {r["id"]: r for r in results or []}
    out = []
    for c in criteria or []:
        c = dict(c)
        r = index.get(c["id"])
        if r and not (c.get("set_by") == "user" and by != "user"):
            c.update(status=r["status"], evidence=r["evidence"] or c.get("evidence", ""), set_by=by)
        out.append(c)
    return out


def done_gate(criteria, verification=None, check_verification=True) -> dict:
    """Decide whether `done` may stand.

    Returns {ok, missing: [{id, criterion, reason}], criteria} where criteria carry the judged status.
    A required criterion needs status met with concrete evidence, or a waiver the human made. Checks Relay
    ran itself override claims: a failing check makes every test/command criterion unmet.
    """
    failures = failed_checks(verification) if check_verification else []
    fail_text = "; ".join(f"`{f.get('command')}` exit {f.get('rc')}" for f in failures)
    judged, missing = [], []
    for c in criteria or []:
        c = dict(c)
        reason = ""
        if failures and c.get("kind") in ("test", "command") and c.get("status") != "waived":
            c.update(status="unmet", evidence=f"Relay verification failed: {fail_text}", set_by="relay")
            reason = f"Relay's own verification failed ({fail_text})"
        elif c.get("status") == "waived":
            if c.get("required") and c.get("set_by") != "user":
                reason = "a required criterion can only be waived by the human (ask with a question envelope)"
        elif c.get("status") != "met":
            reason = "not reported as met"
        elif not concrete_evidence(c.get("evidence")):
            reason = "evidence missing or not concrete (name the command and its result, a file:line, or a screenshot path)"
        if reason and c.get("required"):
            missing.append({"id": c["id"], "criterion": c["criterion"], "reason": reason})
        judged.append(c)
    if failures and not any(c.get("kind") in ("test", "command") for c in judged):
        missing.append({"id": "verification", "criterion": "Relay's verification commands pass", "reason": f"failed: {fail_text}"})
    return {"ok": not missing, "missing": missing, "criteria": judged}


def edit_acceptance(criteria, ops: dict) -> list[dict]:
    """Apply a human edit: {update: [{id, ...}], add: [{criterion, how_to_verify, required}], remove: [id]}."""
    ops = ops or {}
    remove = {_s(x) for x in ops.get("remove") or []}
    out = [dict(c) for c in criteria or [] if c["id"] not in remove]
    by_id = {c["id"]: c for c in out}
    for u in ops.get("update") or []:
        c = by_id.get(_s((u or {}).get("id")))
        if not c:
            continue
        if _s(u.get("criterion")):
            c["criterion"] = _s(u["criterion"])
        if "how_to_verify" in u:
            c["how_to_verify"] = _s(u["how_to_verify"]) or "inspection"
            c["kind"] = verify_kind(c["how_to_verify"])
        if "required" in u:
            c["required"] = bool(u["required"])
        if "status" in u:
            st = _STATUS_ALIASES.get(_key(u["status"]), "unmet")
            c.update(status=st, set_by="user" if st != "unmet" else "")
            c["evidence"] = _s(u.get("evidence")) or ("waived by the human" if st == "waived" else "confirmed by the human" if st == "met" else "")
        elif "evidence" in u:
            c["evidence"] = _s(u["evidence"])
    ids = {c["id"] for c in out}
    n = 1
    for a in ops.get("add") or []:
        text = _s(a.get("criterion") if isinstance(a, dict) else a)
        if not text:
            continue
        while f"A{n}" in ids:
            n += 1
        row = normalize_acceptance([{**(a if isinstance(a, dict) else {}), "criterion": text, "id": f"A{n}"}], source="user")[0]
        ids.add(row["id"])
        out.append(row)
    return out


def acceptance_md(criteria) -> str:
    mark = {"met": "x", "waived": "~", "unmet": " "}
    lines = ["# Acceptance criteria", ""]
    for c in criteria or []:
        lines.append(f"- [{mark.get(c.get('status'), ' ')}] **{c['id']}** {c['criterion']}"
                     + ("" if c.get("required") else " _(optional)_") + f" · verify: {c.get('how_to_verify') or 'inspection'}")
        if c.get("evidence"):
            lines.append(f"  - evidence: {c['evidence']}")
    return "\n".join(lines) + "\n"


def acceptance_block(criteria, with_status=True) -> str:
    if not criteria:
        return "(no acceptance contract: legacy task)"
    rows = []
    for c in criteria:
        head = f"- {c['id']} [{'required' if c.get('required') else 'optional'} · {c.get('kind') or 'inspection'}] {c['criterion']}"
        if c.get("how_to_verify") and c.get("how_to_verify") != "inspection":
            head += f" · verify: {c['how_to_verify']}"
        if with_status:
            head += f" · status: {c.get('status', 'unmet')}" + (" (set by the human)" if c.get("set_by") == "user" else "")
            if c.get("evidence"):
                head += f" · evidence: {c['evidence']}"
        rows.append(head)
    return "\n".join(rows)


# ----------------------------------------------------------------------------- findings and severity
def normalize_severity(value, default="blocking") -> str:
    if not _s(value):
        return default
    return _SEVERITY_ALIASES.get(_key(value), "should_fix")


def normalize_findings(raw, default="blocking") -> list[dict]:
    out = []
    for f in raw or []:
        if isinstance(f, str):
            f = {"problem": f}
        if not isinstance(f, dict):
            continue
        problem = _s(f.get("problem") or f.get("finding") or f.get("issue") or f.get("description"))
        if not problem:
            continue
        out.append({"id": _s(f.get("id")), "severity": normalize_severity(f.get("severity"), default),
                    "file": _s(f.get("file") or f.get("location")), "problem": problem, "fix": _s(f.get("fix")),
                    "criterion": _s(f.get("criterion"))})
    return out


def split_findings(findings):
    blocking = [f for f in findings if f["severity"] == "blocking"]
    return blocking, [f for f in findings if f["severity"] != "blocking"]


def _norm_file(path: str) -> str:
    p = _s(path).lower().replace("\\", "/")
    p = re.sub(r"^\./", "", p)
    p = re.sub(r"(:\d+(-\d+)?)+$", "", p)            # file.py:12 or file.py:12-20
    p = re.sub(r"#l\d+.*$", "", p)
    return p.strip()


def _tokens(text: str) -> set[str]:
    t = re.sub(r"[`'\"()\[\]{}<>]", " ", _s(text).lower())
    ids = set(re.findall(r"\b[af]\d{1,3}\b", t))      # criterion / finding ids tell templated texts apart
    t = re.sub(r"\d+", " ", t)
    return ids | {w for w in re.findall(r"[a-z_][a-z0-9_]{2,}", t) if w not in _STOP}


def fingerprint(finding: dict) -> str:
    base = _norm_file(finding.get("file")) + "|" + " ".join(sorted(_tokens(finding.get("problem"))))
    return hashlib.sha1(base.encode()).hexdigest()[:12]


def similar(a: dict, b: dict, threshold=0.6) -> bool:
    if fingerprint(a) == fingerprint(b):
        return True
    fa, fb = _norm_file(a.get("file")), _norm_file(b.get("file"))
    if fa and fb and fa != fb:
        return False
    ta, tb = _tokens(a.get("problem")), _tokens(b.get("problem"))
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


class Ledger:
    """Blocking findings across rounds, keyed by stable ids F1, F2, …, stored as plain dicts in the checkpoint."""

    def __init__(self, rows=None):
        self.rows = [dict(r) for r in rows or []]

    def dump(self):
        return self.rows

    def get(self, fid):
        return next((r for r in self.rows if r["id"] == fid), None)

    def match(self, finding):
        return next((r for r in self.rows if similar(r, finding)), None)

    def observe(self, findings, where="") -> list[dict]:
        """Record findings; returns them with ledger ids and `attempts` (fix requests already sent)."""
        seen = []
        for f in findings:
            row = self.match(f)
            if not row:
                row = {"id": f"F{len(self.rows) + 1}", "file": f.get("file", ""), "problem": f["problem"], "fix": f.get("fix", ""),
                       "severity": f["severity"], "criterion": f.get("criterion", ""), "fingerprint": fingerprint(f),
                       "attempts": 0, "seen": 0, "status": "open", "where": where}
                self.rows.append(row)
            row["seen"] = int(row.get("seen") or 0) + 1
            if f.get("fix"):
                row["fix"] = f["fix"]
            seen.append({**f, "id": row["id"], "attempts": int(row.get("attempts") or 0), "status": row.get("status", "open")})
        return seen

    def attempt(self, ids):
        for fid in ids or []:
            row = self.get(fid)
            if row:
                row["attempts"] = int(row.get("attempts") or 0) + 1

    def set_status(self, fid, status):
        row = self.get(fid)
        if row:
            row["status"] = status
            if status == "open":
                row["attempts"] = 0

    def recurring(self, observed) -> list[dict]:
        """Observed blocking findings the worker already tried to fix MAX_FIX_ATTEMPTS times."""
        return [f for f in observed if f.get("status") == "open" and int(f.get("attempts") or 0) >= MAX_FIX_ATTEMPTS]

    def accepted(self, finding) -> bool:
        row = self.match(finding)
        return bool(row and row.get("status") == "accepted")


def revise_refs(env: dict) -> list[str]:
    raw = env.get("addresses") or env.get("refs") or env.get("criteria_ids") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    return [_s(x) for x in raw if _s(x)]


def check_revise(env: dict, criteria, ledger: Ledger) -> dict:
    """Severity discipline for a revise decision.

    Returns {ok, reason, refs, blocking, followups}. A revise is justified by an unmet criterion id, an open
    ledger finding, or a blocking finding of its own. One that only carries should_fix/nit items is not.
    """
    findings = normalize_findings(env.get("findings"))
    blocking, followups = split_findings(findings)
    refs = revise_refs(env)
    ids = {c["id"] for c in criteria or []}
    valid = [r for r in refs if r in ids or ledger.get(r)]
    unknown = [r for r in refs if r not in valid and not any(r == f.get("id") for f in blocking)]
    if blocking or valid:
        return {"ok": True, "reason": "", "refs": valid, "blocking": blocking, "followups": followups, "unknown": unknown}
    if followups:
        reason = ("every finding in this revision is should_fix or nit; those become follow-ups and do not justify another round")
    elif refs:
        reason = f"the revision references ids that do not exist ({', '.join(unknown)})"
    else:
        reason = "the revision does not name the acceptance criteria or blocking findings it addresses"
    return {"ok": False, "reason": reason, "refs": valid, "blocking": blocking, "followups": followups, "unknown": unknown}


def add_followups(existing, items, source="") -> list[dict]:
    """Merge non-blocking findings into the follow-up list, deduplicated by fingerprint."""
    out = [dict(x) for x in existing or []]
    for f in items or []:
        if isinstance(f, str):
            f = {"problem": f, "severity": "should_fix"}
        norm = normalize_findings([f], default="should_fix")
        if not norm:
            continue
        f = norm[0]
        # Exact match only: follow-ups are often templated ("A2 not proven: …") and must not swallow each other.
        if any(fingerprint(o) == fingerprint(f) for o in out):
            continue
        out.append({"severity": f["severity"] if f["severity"] != "blocking" else "should_fix", "file": f["file"],
                    "problem": f["problem"], "fix": f["fix"], "source": source or f.get("source", "")})
    return out


def followups_md(items) -> str:
    if not items:
        return ""
    order = {"blocking": 0, "should_fix": 1, "nit": 2}
    rows = sorted(items, key=lambda f: order.get(f.get("severity"), 3))
    return "\n".join(f"- [{f.get('severity', 'should_fix')}] " + (f"`{f['file']}` " if f.get("file") else "") + f["problem"]
                     + (f" (fix: {f['fix']})" if f.get("fix") else "") for f in rows)


def findings_block(findings) -> str:
    return "\n".join(f"- {f['id']} [{f['severity']}] {f.get('file') or '(general)'}: {f['problem']}"
                     + (f" · criterion {f['criterion']}" if f.get("criterion") else "")
                     + (f"\n  Fix: {f['fix']}" if f.get("fix") else "")
                     + (f"\n  Fix attempts so far: {f['attempts']}" if f.get("attempts") else "") for f in findings) or "(none)"


# ----------------------------------------------------------------------------- worktree fingerprint
def worktree_fingerprint(wt, base=None) -> str:
    """Hash of the tracked diff against the base plus untracked file contents; equal hashes mean nothing changed."""
    if not wt:
        return ""
    h = hashlib.sha1()
    diff = quiet(["git", "diff", "--binary", base or "HEAD"], cwd=wt, timeout=120)
    h.update((diff.stdout or "").encode("utf-8", "replace"))
    others = quiet(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=wt, timeout=60).stdout or ""
    names = sorted(n for n in others.split("\0") if n and not n.startswith(".orchestrator_refs/"))
    if names:
        hashes = quiet(["git", "hash-object", "--", *names], cwd=wt, timeout=120).stdout or ""
        h.update("\n".join(names).encode())
        h.update(hashes.encode())
    return h.hexdigest()


# ----------------------------------------------------------------------------- escalation answers
def classify_choice(text: str, choices: dict) -> tuple[str, str]:
    """Map a human answer onto a choice key. `choices` is {key: button label}. Free text is guidance."""
    t = _s(text)
    low = t.lower()
    for key, label in choices.items():
        if low == label.lower():
            return key, ""
    for key, label in choices.items():
        if low.startswith(label.lower()):
            return key, t[len(label):].strip(" :-—\n")
    if re.match(r"^(stop|abort|cancel)\b", low):
        return ("stop", t) if "stop" in choices else ("guidance", t)
    return "guidance", t
