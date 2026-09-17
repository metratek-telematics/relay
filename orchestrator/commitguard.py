"""Agents never commit: Relay commits at delivery (rules/PROTOCOL.md).

When HEAD moves during an agent turn (a worker told to "commit your work", a `git revert`, a stray
`git commit -am`), the commits are undone with `git reset --soft` back to the commit Relay recorded.
The content stays exactly as the agent left it, staged in the working tree, so nothing is lost and
Relay's delivery commit still carries it. A `git reset --hard <older>` by an agent is handled the same
way: the older tree stays in the working tree as uncommitted changes on top of the recorded commit.
"""
from __future__ import annotations

from .util import quiet


def head(wt) -> str:
    p = quiet(["git", "rev-parse", "HEAD"], cwd=wt, timeout=30)
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def current_branch(wt) -> str:
    return (quiet(["git", "branch", "--show-current"], cwd=wt, timeout=30).stdout or "").strip()


def rewind(wt, expected: str, branch: str) -> dict | None:
    """Undo commits made since `expected`. Returns what happened, or None when HEAD did not move.

    {"rewound": bool, "from": sha, "to": sha, "commits": ["abc123 message", …], "pushed": bool, "reason": str}
    """
    if not wt or not expected:
        return None
    now_head = head(wt)
    if not now_head or now_head == expected:
        return None
    info = {"rewound": False, "from": now_head, "to": expected, "commits": [], "pushed": False, "reason": ""}
    log = quiet(["git", "log", "--format=%h %s", f"{expected}..{now_head}"], cwd=wt, timeout=30)
    info["commits"] = [ln for ln in (log.stdout or "").splitlines() if ln.strip()][:20]
    on = current_branch(wt)
    if branch and on != branch:
        # Another branch is checked out; delivery refuses that on its own. Moving refs here could lose work.
        info["reason"] = f"the worktree is on '{on or 'a detached HEAD'}', not '{branch}'"
        return info
    if quiet(["git", "cat-file", "-e", f"{expected}^{{commit}}"], cwd=wt, timeout=30).returncode != 0:
        info["reason"] = f"the recorded commit {expected[:10]} no longer exists"
        return info
    if branch:
        remote = quiet(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=wt, timeout=30)
        rsha = (remote.stdout or "").strip()
        if rsha and rsha != expected and quiet(["git", "merge-base", "--is-ancestor", rsha, now_head], cwd=wt, timeout=30).returncode == 0 \
                and quiet(["git", "merge-base", "--is-ancestor", rsha, expected], cwd=wt, timeout=30).returncode != 0:
            info["pushed"] = True  # the agent also pushed: delivery must replace that remote history
    r = quiet(["git", "reset", "--soft", expected], cwd=wt, timeout=60)
    if r.returncode != 0:
        info["reason"] = (r.stderr or r.stdout or "git reset failed").strip()[:300]
        return info
    info["rewound"] = True
    return info


def supervisor_note(info: dict, role: str) -> str:
    commits = "; ".join(info.get("commits") or []) or f"HEAD moved to {info['from'][:10]}"
    text = (f"ORCHESTRATOR · the {role} committed during its turn ({commits}). Agents never commit: Relay undid the commit(s) "
            "with a soft reset, so every change is still in the working tree and will be committed by Relay at delivery. "
            "Do not ask anyone to commit, amend, revert with commits or push; to undo work, restore files instead "
            "(for example `git checkout <commit> -- <paths>`).")
    if info.get("pushed"):
        text += " The commits had also been pushed; Relay replaces that remote history when it delivers."
    return text
