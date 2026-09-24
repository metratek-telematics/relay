"""The guards that run before delivery, not after (#65).

Everything here exists because of a failure that already happened, and each guard refuses with a named
reason rather than delivering something that will have to be rescued:

    stale base          the base branch moved during the run. `drift` measures it and `merges_cleanly`
                        asks git whether the branch still applies. `rebase` puts the work on top of the
                        base branch so verification can run again; a rebase that conflicts is aborted
                        and the run asks rather than pushing something unmergeable.
    empty proof         a "tests pass" claim with no captured command output, or a UI change with no
                        browser evidence. `evidence_gate` refuses those at the delivery gate, using the
                        judge's own rules (orchestrator/judge.py): this is a second reading of the same
                        contract, not a second set of rules.
    idling              a task waiting for an answer nobody is awake to give. `unblocked_items` says
                        what the question does not block, so the run keeps working on the rest.
    giving up too early `retry_plan` lets a failed run try once more with what it learned.

The functions take what they need and return what they found. Only `rebase` writes anything, and only
inside the task's own worktree.
"""
from __future__ import annotations

import re

from . import judge
from .util import quiet, truncate

# How many work packages a run may take on the unblocked part of the work while a question waits.
MAX_DEFERRALS = 2

# Evidence that a command really ran: a captured exit code, an output block, or a result line.
_OUTPUT = re.compile(r"```|\bexit(?:ed with)?\s*(?:code\s*)?[:=]?\s*\d|\b\d+\s+(?:passed|failed|tests?|ok)\b|"
                     r"\bOK\b|\bFAILED\b|\bpassed\b|\bfailing\b", re.I)
_IMAGE = re.compile(r"\.(png|jpe?g|gif|webp|mp4|webm)\b", re.I)
_UI_WORDS = re.compile(r"\b(screenshot|browser|rendered?|render|visual|page|screen|ui|button|layout|css|styl)", re.I)


# ============================================================================ the branch still applies
def base_ref(cfg: dict, task: dict, remote: str = "origin") -> str:
    """The ref a delivery must still merge into: `origin/<base branch>`."""
    base = str((task or {}).get("github_base") or (cfg or {}).get("github_pr_base") or "").strip()
    if not base:
        return ""
    return base if "/" in base else f"{remote}/{base}"


def drift(wt, ref: str, *, fetch: bool = True) -> dict:
    """How far the base branch moved away from the branch: {ref, behind, ahead, ok, error}.

    `behind` is the number of commits on the base branch the task branch does not have. `ok` is False
    only when the answer is known and the branch is behind; an unreachable remote leaves `behind` None.
    """
    out = {"ref": ref, "behind": None, "ahead": None, "ok": None, "error": ""}
    if not wt or not ref:
        out["error"] = "no base branch to compare against"
        return out
    if fetch:
        quiet(["git", "fetch", "--prune", "--quiet"], cwd=wt, timeout=180)
    if quiet(["git", "rev-parse", "--verify", "--quiet", ref], cwd=wt, timeout=15).returncode != 0:
        out["error"] = f"{ref} is not a ref in this checkout"
        return out
    p = quiet(["git", "rev-list", "--left-right", "--count", f"{ref}...HEAD"], cwd=wt, timeout=60)
    parts = (p.stdout or "").split()
    if p.returncode != 0 or len(parts) != 2:
        out["error"] = truncate((p.stdout or "") + (p.stderr or ""), 200) or "git rev-list failed"
        return out
    out["behind"], out["ahead"] = int(parts[0]), int(parts[1])
    out["ok"] = out["behind"] == 0
    return out


def merges_cleanly(wt, ref: str) -> dict:
    """Does the branch still merge into `ref`? {clean, conflicts, checked, error}.

    Asks git, not the agent: `git merge-tree` merges in memory and names the conflicted paths without
    touching the worktree. Where git is too old for that, a branch whose base is an ancestor is clean
    and anything else is left unchecked rather than guessed at.
    """
    out = {"clean": None, "conflicts": [], "checked": False, "error": ""}
    if not wt or not ref:
        out["error"] = "no base branch to merge into"
        return out
    p = quiet(["git", "merge-tree", "--write-tree", "--name-only", ref, "HEAD"], cwd=wt, timeout=180)
    text = (p.stdout or "") + (p.stderr or "")
    if p.returncode in (0, 1) and "unknown option" not in text and "usage:" not in text.lower():
        out["checked"] = True
        out["clean"] = p.returncode == 0
        if p.returncode == 1:
            # First line is the tree oid; the rest are the conflicted paths.
            out["conflicts"] = [l.strip() for l in (p.stdout or "").splitlines()[1:] if l.strip()][:40]
        return out
    if quiet(["git", "merge-base", "--is-ancestor", ref, "HEAD"], cwd=wt, timeout=60).returncode == 0:
        out.update(clean=True, checked=True)
        return out
    # Older git (before 2.38): the three-argument form prints conflict markers instead of setting an exit code.
    merge_base = (quiet(["git", "merge-base", ref, "HEAD"], cwd=wt, timeout=60).stdout or "").strip()
    if not merge_base:
        out["error"] = "the branch and the base branch share no history"
        return out
    p = quiet(["git", "merge-tree", merge_base, ref, "HEAD"], cwd=wt, timeout=180)
    if p.returncode != 0:
        out["error"] = "this git cannot test the merge without touching the worktree"
        return out
    out["checked"] = True
    out["clean"] = "<<<<<<<" not in (p.stdout or "")
    if not out["clean"]:
        # Files appear as "  our 100644 <sha> <path>"; only the ones whose hunks carry markers conflict.
        path = ""
        for line in (p.stdout or "").splitlines():
            m = re.match(r"\s+our\s+\d+\s+[0-9a-f]{7,40}\s+(.+)$", line)
            if m:
                path = m.group(1).strip()
            elif line.startswith("+<<<<<<<") and path and path not in out["conflicts"]:
                out["conflicts"].append(path)
        out["conflicts"] = out["conflicts"][:40]
    return out


def rebase(runner, wt, ref: str) -> dict:
    """Rebase the task branch onto `ref`. A conflict is aborted, never left half-applied.

    Returns {ok, conflicts, error}. `ok` True means the branch now sits on top of the base branch and
    verification must run again before anything is delivered.
    """
    out = {"ok": False, "conflicts": [], "error": ""}
    p = quiet(["git", "rebase", ref], cwd=wt, timeout=600)
    if p.returncode == 0:
        out["ok"] = True
        if runner:
            runner.timeline("git", f"Rebased onto {ref}", "The base branch moved during the run; the work now sits on current code.")
        return out
    conflicts = [l.strip() for l in (quiet(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt, timeout=60).stdout or "").splitlines() if l.strip()]
    quiet(["git", "rebase", "--abort"], cwd=wt, timeout=120)
    out["conflicts"] = conflicts[:40]
    out["error"] = truncate(((p.stdout or "") + (p.stderr or "")).strip(), 400)
    if runner:
        runner.timeline("git", f"Rebase onto {ref} conflicted",
                        ", ".join(conflicts[:6]) or out["error"] or "git could not replay the work on the base branch")
    return out


def base_guard(runner, wt, cfg: dict, task: dict, *, fetch: bool = True, rebaser=rebase) -> dict:
    """The whole stale-base check, in the order the run needs it.

    Returns a record for the task: {ref, base_behind, conflicted, rebased, needs_human, conflicts,
    reverify, note}. `needs_human` means the branch cannot be made mergeable here and delivering it
    would hand over something nobody can merge.
    """
    ref = base_ref(cfg, task)
    rec = {"ref": ref, "base_behind": None, "conflicted": None, "rebased": False, "needs_human": False,
           "conflicts": [], "reverify": False, "note": ""}
    if not ref:
        rec["note"] = "no base branch is configured, so there was nothing to check the merge against"
        return rec
    d = drift(wt, ref, fetch=fetch)
    rec["base_behind"] = d["behind"]
    if d.get("error"):
        rec["note"] = d["error"]
        return rec
    m = merges_cleanly(wt, ref)
    if m["clean"] is None:
        rec["note"] = m.get("error") or "the merge could not be tested"
        return rec
    rec["conflicted"] = not m["clean"]
    if m["clean"]:
        rec["note"] = (f"{d['behind']} commit(s) landed on {ref} during the run; the branch still merges cleanly."
                       if d["behind"] else f"{ref} has not moved since the branch was cut.")
        return rec
    r = rebaser(runner, wt, ref)
    rec["conflicts"] = r.get("conflicts") or []
    if r.get("ok"):
        rec.update(rebased=True, conflicted=False, reverify=True,
                   note=f"The branch no longer merged into {ref}, so it was rebased onto it and verification runs again.")
        return rec
    rec.update(needs_human=True,
               note=(f"The branch no longer merges into {ref} and the rebase conflicted"
                     + (f" in {', '.join(rec['conflicts'][:5])}" if rec["conflicts"] else "")
                     + ". Relay did not deliver something nobody can merge."))
    return rec


# ============================================================================ proof, not claims
def proof_index(verification: dict | None, messages: list[dict] | None = None, artifacts: list[str] | None = None) -> dict:
    """What Relay itself captured during the run: commands that ran with output, and image evidence."""
    commands, screenshots = [], []
    for i in (verification or {}).get("items") or []:
        cmd = str(i.get("command") or "").strip()
        if not cmd:
            continue
        has_output = bool(str(i.get("output") or i.get("tail") or "").strip()) or i.get("rc") is not None
        commands.append({"command": cmd, "ok": bool(i.get("ok")), "captured": has_output})
    for m in messages or []:
        for p in (m.get("images") or []) + ([m.get("path")] if m.get("kind") == "screenshot" else []):
            if p and _IMAGE.search(str(p)):
                screenshots.append(str(p))
    for a in artifacts or []:
        if _IMAGE.search(str(a)):
            screenshots.append(str(a))
    return {"commands": commands, "screenshots": screenshots}


def _claims_a_command(text: str) -> str:
    m = re.search(r"`([^`]{3,120})`", text or "")
    return m.group(1).strip() if m else ""


def evidence_gate(criteria, verification: dict | None, proof: dict | None = None) -> dict:
    """Refuse a delivery whose proof proves nothing. Pure.

    It starts from the judge's own `done_gate` — same contract, same concrete-evidence rule — and adds
    the two shapes of empty proof that got past it:

        a test or command criterion whose evidence names no command output Relay captured;
        a screenshot criterion with no image anywhere in the run.

    Returns {ok, refusals: [{id, criterion, reason}], gate} where `gate` is the judge's own result.
    """
    proof = proof or {"commands": [], "screenshots": []}
    gate = judge.done_gate(criteria, verification, check_verification=True)
    from_judge = {m["id"]: m["reason"] for m in gate["missing"]}
    captured = [c for c in proof.get("commands") or [] if c.get("captured")]
    refusals = []
    for c in gate["criteria"]:
        if not c.get("required"):
            continue
        reason = from_judge.get(c["id"], "")
        # The judge says the evidence is not concrete; this says exactly what is missing from the run.
        vague = not reason or reason.startswith("evidence missing")
        evidence = str(c.get("evidence") or "")
        if c.get("status") != "waived" and vague:
            if c.get("kind") in ("test", "command") and not captured and not _OUTPUT.search(evidence):
                named = _claims_a_command(evidence) or str(c.get("how_to_verify") or "")
                reason = f"a passing claim for `{truncate(named, 60)}` with no command output captured in this run"
            elif (c.get("kind") == "screenshot" or (_UI_WORDS.search(c.get("criterion") or "") and _UI_WORDS.search(evidence))) \
                    and not proof.get("screenshots") and not _IMAGE.search(evidence):
                reason = "a user-interface change with no browser evidence: no screenshot was captured in this run"
        if reason:
            refusals.append({"id": c["id"], "criterion": c["criterion"], "reason": reason})
    for m in gate["missing"]:   # anything the judge raised that is not a criterion of its own (a failed check)
        if not any(r["id"] == m["id"] for r in refusals):
            refusals.append({"id": m["id"], "criterion": m["criterion"], "reason": m["reason"]})
    return {"ok": not refusals, "refusals": refusals, "gate": gate}


# ============================================================================ not idling on a question
def unblocked_items(criteria, question: str) -> list[dict]:
    """Required criteria the question does not block: what the run can still work on while it waits.

    A criterion is blocked when the question names it by id, or when the question and the criterion
    talk about the same things (the judge's own token overlap).
    """
    q = str(question or "")
    ids = {x.upper() for x in re.findall(r"\b([Aa]\d{1,3})\b", q)}
    out = []
    for c in criteria or []:
        if not c.get("required") or c.get("status") in ("met", "waived"):
            continue
        if c["id"].upper() in ids:
            continue
        if judge.similar({"problem": q}, {"problem": c.get("criterion")}, threshold=0.34):
            continue
        out.append(c)
    return out


def keep_working(criteria, question: str, deferrals: int, *, max_deferrals: int = MAX_DEFERRALS) -> dict:
    """Should the run carry on while the question waits? {go, items, reason}."""
    items = unblocked_items(criteria, question)
    if deferrals >= max_deferrals:
        return {"go": False, "items": items, "reason": f"the question has already waited through {deferrals} work package(s)"}
    if not items:
        return {"go": False, "items": [], "reason": "everything still open depends on the answer"}
    return {"go": True, "items": items,
            "reason": f"{len(items)} acceptance criteria do not depend on the answer"}


# ============================================================================ one more try
def retry_plan(task: dict, settings: dict | None = None) -> dict:
    """A failed run gets exactly one more try, carrying what it learned. {retry, attempt, note}.

    Relay already retries infrastructure failures (orchestrator/autopilot.py). This is the wider rule
    the owner asked for: any failed run tries once more, from its checkpoint, with the cause of the
    failure written into the next run's instructions. Twice is not a retry, it is a loop.
    """
    task = task or {}
    settings = settings or {}
    allowed = int(settings.get("retry_once", 1) or 0)
    done = int(task.get("honest_retries") or 0)
    if task.get("status") != "failed":
        return {"retry": False, "attempt": done, "note": ""}
    if done >= allowed:
        return {"retry": False, "attempt": done, "note": "it already had its one retry"}
    cause = truncate(str(task.get("error") or task.get("detail") or "").strip(), 400)
    lesson = (task.get("autopsy") or {}).get("cause") or ""
    note = ("The previous run failed: " + (cause or "no error was recorded") +
            (f"\nWhat the retrospective made of it: {truncate(lesson, 300)}" if lesson else "") +
            "\nStart from where it stopped and do not repeat that step the same way.")
    return {"retry": True, "attempt": done + 1, "note": note}
