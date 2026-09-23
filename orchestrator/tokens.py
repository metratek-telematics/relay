"""Token efficiency: measure where prompt tokens go, and the helpers that send less.

Measuring
  Prompt        a str that also carries its sections ({name: chars}) and where context blocks are inserted.
                protocol.py builds every prompt from named parts, so each turn records how much the rules,
                task, context packet, diff, verification, lessons, design and tools cost (chars / 4 ≈ tokens).
  report()      aggregates per role, agent, section and day across tasks for the Token efficiency panel.

Sending less
  failure_excerpt()  the lines of a failing check that explain it (pytest/jest/go/cargo summaries, errors),
                     deduplicated, instead of the tail of the whole log.
  targeted_diff()    diffstat plus whole hunks for small files within the budget; larger files are listed
                     with the command that shows them. On a re-review only files that changed since the
                     last round are sent.
  compact_handoff()  the state a fresh session needs when a long session is restarted to shed context.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone

CHARS_PER_TOKEN = 4

# Sections in the order they appear in a kickoff prompt: stable first (prefix-cache friendly), volatile last.
SECTION_ORDER = ["rules", "protocol", "tools", "task", "lessons", "knowledge", "context", "design", "environment", "packet",
                 "judge", "report", "verification", "diff", "guidance", "instruction", "other"]


class Prompt(str):
    """A prompt string that remembers the size of its named sections."""
    sections: dict
    insert_at: int
    full: str | None

    def __new__(cls, text: str, sections: dict | None = None, insert_at: int | None = None, full: str | None = None):
        obj = super().__new__(cls, text)
        obj.sections = dict(sections or {})
        obj.insert_at = len(text) if insert_at is None else insert_at
        obj.full = full
        return obj


def build(parts, insert_before: str | None = None, full: str | None = None) -> Prompt:
    """Join (section, text) parts with blank lines; empty parts are dropped.

    insert_before names the first section that context blocks (lessons, system map) are placed in front of.
    """
    chunks, sections, insert_at = [], OrderedDict(), None
    pos = 0
    for name, text in parts:
        text = (text or "").strip("\n")
        if not text.strip():
            continue
        if insert_before and name == insert_before and insert_at is None:
            insert_at = pos
        chunks.append(text)
        sections[name] = sections.get(name, 0) + len(text) + 2
        pos += len(text) + 2
    body = "\n\n".join(chunks) + "\n"
    return Prompt(body, sections, insert_at, full)


def insert_block(prompt: str, block: str, section: str) -> str:
    """Insert a context block where the prompt wants it (before its volatile tail), keeping the accounting."""
    if not block or not block.strip():
        return prompt
    block = block.strip("\n")
    if isinstance(prompt, Prompt):
        at = min(prompt.insert_at, len(prompt))
        text = prompt[:at] + block + "\n\n" + prompt[at:]
        sections = dict(prompt.sections)
        sections[section] = sections.get(section, 0) + len(block) + 2
        full = insert_block(prompt.full, block, section) if prompt.full else None
        return Prompt(text, sections, at + len(block) + 2, full)
    at = prompt.find("HOW THIS SESSION WORKS")
    if at < 0:
        return prompt.rstrip() + "\n\n" + block + "\n"
    return prompt[:at] + block + "\n\n" + prompt[at:]


def prepend(prompt: str, text: str, section: str = "other") -> str:
    if not text:
        return prompt
    sections = dict(getattr(prompt, "sections", None) or {"other": len(prompt)})
    sections[section] = sections.get(section, 0) + len(text) + 2
    full = getattr(prompt, "full", None)
    return Prompt(text + "\n\n" + prompt, sections, None, (text + "\n\n" + full) if full else None)


def sections_of(prompt: str) -> dict:
    s = getattr(prompt, "sections", None)
    if s:
        return dict(s)
    return {"other": len(prompt or "")}


def est(chars: int) -> int:
    return int(round((chars or 0) / CHARS_PER_TOKEN))


def digest(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]


# ============================================================================ usage normalization
# CLIs whose reported input tokens exclude cache reads (Anthropic-style usage); for the others input includes them.
INPUT_EXCLUDES_CACHED = {"claude", "qwen", "amp", "cursor", "opencode", "kilo", "crush"}


def prompt_tokens(agent: str, usage: dict) -> int:
    inp, cached = int(usage.get("input") or 0), int(usage.get("cached") or 0)
    write = int(usage.get("cache_write") or 0)
    return inp + cached + write if agent in INPUT_EXCLUDES_CACHED else max(inp, cached)


def cache_ratio(agent: str, usage: dict) -> float:
    total = prompt_tokens(agent, usage)
    return round(min(1.0, int(usage.get("cached") or 0) / total), 3) if total else 0.0


# ============================================================================ verification
_FAIL_LINE = re.compile(r"(?i)(^FAILED |^ERROR |^E\s{2,}|error[:\[]|exception|traceback|assert|\bfail(?:ed|ure)?\b|✕|●|panic:|"
                        r"cannot find|not found|undefined|expected|received|^\s*at .*:\d+|^--- FAIL|:\d+:\d+: error)")
_NOISE = re.compile(r"^\s*$|^[=\-_.·]{8,}\s*$|^\s*\d+%\||DeprecationWarning|^npm (?:notice|WARN)")
_SUMMARY_START = re.compile(r"(?i)^=+ (?:short test summary info|FAILURES|ERRORS) =+$|^Summary of all failing tests|^failures:$")


def failure_excerpt(output: str, limit: int) -> str:
    """The part of a failing check's output that explains the failure, within `limit` characters.

    Order of preference: a test runner's own failure summary, then error lines with a little context,
    then the tail. Repeated lines (the same stack frame, the same warning) are kept once.
    """
    text = (output or "").replace("\r", "")
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    picked: list[str] = []
    for i, line in enumerate(lines):
        if _SUMMARY_START.match(line.strip()):
            picked = lines[i:]
            break
    if not picked:
        keep = set()
        for i, line in enumerate(lines):
            if _FAIL_LINE.search(line) and not _NOISE.search(line):
                keep.update(range(max(0, i - 1), min(len(lines), i + 3)))
        picked = [lines[i] for i in sorted(keep)]
    seen, out = set(), []
    for line in picked:
        key = re.sub(r"\d+", "#", line.strip())
        if _NOISE.search(line) or key in seen:
            continue
        seen.add(key)
        out.append(line)
    tail = [x for x in lines[-6:] if x.strip()]  # the runner's final verdict line ("3 failed, 40 passed")
    body = "\n".join(out)
    tail_text = "\n".join(x for x in tail if x not in out)
    room = max(200, limit - len(tail_text) - 40)
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0] + f"\n… ({len(out)} relevant lines, trimmed)"
    result = (body + ("\n…\n" + tail_text if tail_text else "")).strip()
    return result[:limit] if result else text[-limit:]


# ============================================================================ diffs
_FILE_HEAD = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)


def split_diff(diff: str) -> list[tuple[str, str]]:
    starts = [(m.start(), m.group(2)) for m in _FILE_HEAD.finditer(diff or "")]
    out = []
    for i, (pos, path) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(diff)
        out.append((path, diff[pos:end]))
    if not starts and (diff or "").strip():
        out.append(("(diff)", diff))
    return out


def targeted_diff(diff: str, budget: int, base: str = "", previous: dict | None = None) -> tuple[str, dict]:
    """Stat + whole hunks for as many files as fit, smallest first; the rest listed with the command to see them.

    previous: {path: digest} from the last round; files whose diff did not change since are listed, not re-sent.
    Returns (text, {path: digest}).
    """
    untracked = []
    for m in re.finditer(r"\n*UNTRACKED FILES:\n(.*?)(?=\n### |\Z)", diff or "", re.S):
        untracked += [x.strip() for x in m.group(1).splitlines() if x.strip()]
    diff = re.sub(r"\n*UNTRACKED FILES:\n.*?(?=\n### |\Z)", "", diff or "", flags=re.S)
    files = split_diff(diff)
    hashes = {p: digest(body) for p, body in files}
    if untracked:
        hashes["(untracked)"] = digest("\n".join(untracked))
    if not files:
        return ("UNTRACKED FILES (new, not in git diff; read them directly):\n" + "\n".join(untracked)) if untracked else "(no changes)", hashes
    rows, send, unchanged = [], [], []
    for path, body in files:
        adds = sum(1 for x in body.splitlines() if x.startswith("+") and not x.startswith("+++"))
        dels = sum(1 for x in body.splitlines() if x.startswith("-") and not x.startswith("---"))
        rows.append((path, adds, dels, body))
    stat = "\n".join(f"  {p} | +{a} -{d}" for p, a, d, _ in rows)
    used = len(stat)
    for path, a, d, body in sorted(rows, key=lambda r: len(r[3])):
        if previous and previous.get(path) == hashes[path]:
            unchanged.append(path)
            continue
        if used + len(body) <= budget:
            send.append((path, body))
            used += len(body)
    sent_paths = {p for p, _ in send}
    rest = [p for p, _, _, _ in rows if p not in sent_paths and p not in unchanged]
    cmd = f"git diff {base} -- <path>" if base else "git diff -- <path>"
    parts = [f"DIFFSTAT ({len(rows)} files)", stat]
    if untracked:
        parts += ["", "UNTRACKED FILES (new, not in git diff; read them directly): " + ", ".join(untracked[:60])]
    if unchanged:
        parts += ["", "Unchanged since your last review (not repeated): " + ", ".join(unchanged)]
    if rest:
        parts += ["", f"Not included to save tokens (run `{cmd}` for any you need): " + ", ".join(rest)]
    order = {p: i for i, (p, _, _, _) in enumerate(rows)}
    if send:
        parts += ["", "\n".join(body.rstrip("\n") for _, body in sorted(send, key=lambda x: order[x[0]]))]
    return "\n".join(parts), hashes


# ============================================================================ compaction
def compact_handoff(role: str, state: dict, contract: str, changed: list, open_findings: str = "", context_tokens: int = 0) -> str:
    """What a fresh session needs to carry on, built by Relay from its own records (no model call)."""
    plan = state.get("plan") or {}
    report = state.get("report") or {}
    lines = [f"ORCHESTRATOR · CONTEXT HANDOFF · your previous session for this role grew to about {context_tokens:,} tokens, so Relay "
             "started a fresh one to keep every later turn cheaper. The worktree holds all the work. This is the state; do not redo "
             "finished work and do not re-read the whole repository."]
    if plan.get("summary") or plan.get("plan"):
        lines += ["", "PLAN", str(plan.get("summary") or ""), str(plan.get("plan") or "")[:3000]]
    if contract:
        lines += ["", "ACCEPTANCE CONTRACT (current status)", contract]
    if open_findings:
        lines += ["", "OPEN BLOCKING FINDINGS", open_findings]
    lines += ["", f"PROGRESS · work package #{state.get('turn') or 0} · phase {state.get('phase') or '?'}"]
    if state.get("instruction_summary"):
        lines.append(f"Current package: {state['instruction_summary']}")
    if report.get("summary"):
        lines.append(f"Last worker report ({report.get('status', '?')}): {report['summary']}")
    if changed:
        lines.append("Changed files: " + ", ".join(f"{c.get('status', '')} {c.get('path', '')}".strip() for c in changed[:40])
                     + (f" (+{len(changed) - 40} more)" if len(changed) > 40 else ""))
    blocked = state.get("blocked_checks") or []
    if blocked:
        lines.append("Checks that could not run: " + "; ".join(f"{b.get('check')}: {b.get('reason')}" for b in blocked[:8]))
    return "\n".join(x for x in lines if x is not None)


# ============================================================================ report
def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def report(tasks: list, days: int = 30, now_ts: float | None = None) -> dict:
    """Where tokens went across tasks: totals by role, agent and prompt section, cache hits, trend per day, top tasks."""
    now_ts = now_ts or time.time()
    since = now_ts - days * 86400
    zero = lambda: {"turns": 0, "input": 0, "cached": 0, "output": 0, "prompt": 0, "cost_usd": 0.0}  # noqa: E731
    by_role, by_agent, by_day, sections = {}, {}, {}, {}
    total = zero()
    rows = []
    measured_turns = 0
    for t in tasks:
        log = ((t.get("metrics") or {}).get("log") or [])
        trow = zero()
        tsec = {}
        for e in log:
            if float(e.get("end") or 0) < since:
                continue
            agent, role = e.get("agent") or "?", e.get("role") or "?"
            usage = {"input": e.get("input"), "cached": e.get("cached"), "output": e.get("output"), "cache_write": e.get("cache_write")}
            ptok = prompt_tokens(agent, usage)
            for bucket in (total, trow, by_role.setdefault(role, zero()), by_agent.setdefault(agent, zero()),
                           by_day.setdefault(_day(float(e.get("end") or now_ts)), zero())):
                bucket["turns"] += 1
                bucket["input"] += int(e.get("input") or 0)
                bucket["cached"] += int(e.get("cached") or 0)
                bucket["output"] += int(e.get("output") or 0)
                bucket["prompt"] += ptok
                bucket["cost_usd"] = round(bucket["cost_usd"] + float(e.get("cost_usd") or 0), 5)
            if e.get("sections"):
                measured_turns += 1
                for k, v in e["sections"].items():
                    sections[k] = sections.get(k, 0) + int(v or 0)
                    tsec[k] = tsec.get(k, 0) + int(v or 0)
        if trow["turns"]:
            rows.append({"id": t.get("id"), "number": t.get("number"), "name": t.get("name"), "status": t.get("status"), **trow,
                         "cache_ratio": round(trow["cached"] / trow["prompt"], 3) if trow["prompt"] else 0.0,
                         "sections": tsec, "updated_at": t.get("updated_at")})

    def finish(b):
        return {**b, "cache_ratio": round(b["cached"] / b["prompt"], 3) if b["prompt"] else 0.0,
                "output_share": round(b["output"] / (b["prompt"] + b["output"]), 3) if (b["prompt"] + b["output"]) else 0.0}

    ordered = {k: sections[k] for k in SECTION_ORDER if k in sections}
    ordered.update({k: v for k, v in sections.items() if k not in ordered})
    return {
        "days": days, "total": finish(total), "roles": {k: finish(v) for k, v in by_role.items()},
        "agents": {k: finish(v) for k, v in by_agent.items()},
        "sections": {k: {"chars": v, "tokens": est(v)} for k, v in ordered.items()}, "measured_turns": measured_turns,
        "trend": [{"day": d, **finish(v)} for d, v in sorted(by_day.items())],
        "tasks": sorted(rows, key=lambda r: r["cost_usd"] or r["prompt"], reverse=True)[:25],
    }
