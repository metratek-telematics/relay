"""Configuration defaults, migration, and workflow presets."""
from __future__ import annotations

import copy
import threading

from .util import CONFIG_PATH, read_json, write_json

BUILD = "14.1.0"
APP_NAME = "Relay"

AGENTS = {
    "codex":  {"label": "Codex",  "vendor": "OpenAI",    "binary": "codex",  "color": "#5d78d6",
               "efforts": ["low", "medium", "high", "xhigh"]},
    "claude": {"label": "Claude", "vendor": "Anthropic", "binary": "claude", "color": "#d97757",
               "efforts": ["low", "medium", "high", "xhigh", "max"]},
    "gemini": {"label": "Gemini", "vendor": "Google",    "binary": "gemini", "color": "#3a9d8a",
               "efforts": []},
}

# Editable model catalog shown in the model pickers (Settings → Agents). Free text is always allowed too.
DEFAULT_MODELS = {
    "codex": ["gpt-6-astra", "gpt-5.3-codex", "gpt-5.2-codex", "gpt-5.1-codex-max", "gpt-5.1-codex-mini"],
    "claude": ["fable[1m]", "opus", "sonnet", "haiku", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-pro-preview"],
}

ROLES = ["supervisor", "worker", "reviewer"]

PRESETS = [
    {
        "id": "codex-supervises-claude",
        "name": "Codex supervises Claude",
        "description": "Codex plans, delegates and verifies. Claude implements each work package. Codex is the final gate.",
        "roles": {"supervisor": {"agent": "codex"}, "worker": {"agent": "claude"}, "reviewer": {"agent": ""}},
    },
    {
        "id": "claude-supervises-codex",
        "name": "Claude supervises Codex",
        "description": "Claude leads the plan and reviews; Codex implements in the worktree.",
        "roles": {"supervisor": {"agent": "claude"}, "worker": {"agent": "codex"}, "reviewer": {"agent": ""}},
    },
    {
        "id": "codex-claude-independent-review",
        "name": "Codex → Claude + independent Codex review",
        "description": "Codex supervises Claude, then a separate Codex session reviews the diff before delivery.",
        "roles": {"supervisor": {"agent": "codex"}, "worker": {"agent": "claude"}, "reviewer": {"agent": "codex"}},
    },
    {
        "id": "three-vendor-panel",
        "name": "Three-agent panel (Codex · Claude · Gemini)",
        "description": "Codex supervises, Claude implements, Gemini performs the independent review.",
        "roles": {"supervisor": {"agent": "codex"}, "worker": {"agent": "claude"}, "reviewer": {"agent": "gemini"}},
    },
    {
        "id": "claude-gemini",
        "name": "Claude supervises Gemini",
        "description": "Claude plans and reviews; Gemini implements.",
        "roles": {"supervisor": {"agent": "claude"}, "worker": {"agent": "gemini"}, "reviewer": {"agent": ""}},
    },
    {
        "id": "claude-pair",
        "name": "Claude pair (lead + engineer)",
        "description": "Two Claude sessions: one leads and verifies, the other implements. Useful when Codex is unavailable.",
        "roles": {"supervisor": {"agent": "claude"}, "worker": {"agent": "claude"}, "reviewer": {"agent": ""}},
    },
    {
        "id": "codex-pair",
        "name": "Codex pair (lead + engineer)",
        "description": "Two Codex sessions: one leads and verifies, the other implements.",
        "roles": {"supervisor": {"agent": "codex"}, "worker": {"agent": "codex"}, "reviewer": {"agent": ""}},
    },
]

TASK_TEMPLATES = [
    {"id": "feature", "name": "Feature", "hint": "Describe the user-visible behavior, the entry points, and how success is observed."},
    {"id": "bugfix", "name": "Bug fix", "hint": "Describe the reproduction steps, expected vs actual behavior, and any error output."},
    {"id": "refactor", "name": "Refactor", "hint": "Describe the target structure, what must stay behaviorally identical, and the tests that prove it."},
    {"id": "tests", "name": "Add tests", "hint": "Describe the code paths that need coverage and the failure modes worth locking down."},
    {"id": "docs", "name": "Documentation", "hint": "Describe the audience, the pages/files to update, and what must be accurate."},
    {"id": "review", "name": "Audit / review only", "hint": "Ask the team to inspect and report without changing behavior."},
]

DEFAULTS = {
    "build": BUILD,
    # workflow
    # An independent reviewer by default: a gate that checks the result against the request, not a second designer.
    "workflow_preset": "codex-claude-independent-review",
    "roles": {
        "supervisor": {"agent": "codex", "model": "", "effort": ""},
        "worker": {"agent": "claude", "model": "", "effort": ""},
        "reviewer": {"agent": "codex", "model": "", "effort": ""},
    },
    "models": copy.deepcopy(DEFAULT_MODELS),
    "max_turns": 12,
    "max_review_rounds": 3,
    "verify_mode": "each_report",          # each_report | before_review | off
    "approval_before_delivery": False,
    "allow_agent_questions": True,
    "agent_turn_timeout_minutes": 60,
    "stall_warning_minutes": 8,
    "envelope_retries": 2,
    "max_parallel": 1,
    "queue_running": False,          # remembered across restarts so the queue resumes itself
    "auto_resume_interrupted": True,  # re-queue tasks that a restart interrupted mid-run
    # codex
    "codex_sandbox": "workspace-write",      # workspace-write | danger-full-access
    "codex_extra_args": [],
    "codex_reasoning_effort": "",            # "", low, medium, high, xhigh
    # claude
    "claude_permission": "bypass",           # bypass | acceptEdits
    "claude_max_turns_per_call": 200,
    "claude_extra_args": [],
    "claude_setting_sources": "",           # "" = all; e.g. "project,local" isolates agents from personal hooks/MCP
    "prefer_claude_subscription_auth": True,
    # gemini
    "gemini_approval": "yolo",               # yolo | auto_edit
    "gemini_extra_args": [],
    # env per agent
    "agent_env": {"codex": {}, "claude": {}, "gemini": {}},
    # Estimated equivalent API pricing (USD per 1M tokens) used when a CLI reports tokens but no cost.
    # Subscription usage is not billed per token; this is an estimate for comparison and budgeting only.
    "pricing": {
        "codex": {"input": 1.25, "cached": 0.125, "output": 10.0},
        "claude": {"input": 5.0, "cached": 0.5, "output": 25.0},
        "gemini": {"input": 1.25, "cached": 0.31, "output": 10.0},
    },
    "show_estimated_cost": True,
    # Subagents: when an agent spawns helpers (Claude Task tool, Codex multi-agent),
    # run them on a cheaper model to keep usage down.
    "subagent_models": {"codex": "", "claude": "haiku", "gemini": ""},  # only Claude supports this
    "subagent_cheap_enabled": True,
    # Per-agent defaults used whenever a role leaves model/effort blank.
    "agent_defaults": {"codex": {"model": "", "effort": ""},
                       "claude": {"model": "", "effort": ""},
                       "gemini": {"model": "", "effort": ""}},
    # Token budget: caps on what the orchestrator echoes back into prompts each turn.
    "lean_prompts": True,               # send only the rules a role actually needs
    "budget_diff_chars": 24000,         # diff sent to the reviewer
    "budget_report_chars": 6000,        # worker report echoed to the supervisor
    "budget_verify_chars": 1200,        # per-command verification output (failures only)
    "budget_verify_pass_quiet": True,   # on PASS send just the verdict, not the output
    "budget_file_list": 40,             # changed files listed back to the supervisor
    "budget_tool_output_chars": 8000,   # tool result stored per message
    # verification
    "verification_commands": [],
    "auto_detect_verification": True,
    "verification_timeout_minutes": 20,
    # git
    "branch_naming": "type",               # type: feat/…, fix/… from the task type · prefix: <branch_prefix>/…
    "branch_prefix": "agent",
    "snapshot_working_tree": True,
    "copy_untracked_files": True,
    "copy_ignored_root_files": True,   # .env, .env.local, dev auth cookies: what the app needs to run
    "max_untracked_copy_mb": 100,
    "keep_worktrees": True,
    "commit_message_prefix": "agent:",
    # github
    "github_intake_enabled": True,
    "github_poll_seconds": 60,
    "github_default_label": "agent",
    "github_auto_push_on_pass": True,
    "github_auto_create_pr": True,
    "github_pr_draft": True,
    "github_pr_base": "",
    "github_pr_body_template": "{summary}\n\n---\n{details}\n\n{issue_close}\n",
    # ui
    "ui_theme": "system",
    "ui_density": "comfortable",
    "ui_notifications": True,
    # Which server notifications may raise a desktop alert or a sound. In-app toasts
    # always show; these only decide what is allowed to interrupt you elsewhere.
    "ui_notify_events": {"delivered": True, "failed": True, "needs_input": True, "pr_opened": True},
    "ui_sound": False,
    # Reusable requests offered in the New task wizard. A list, so saving replaces it
    # whole and a deleted prompt stays deleted.
    "saved_prompts": [
        {"id": "p_design_page", "name": "Redesign a page following rules/DESIGN.md", "template": "feature",
         "text": "Redesign the <page or view> so it follows rules/DESIGN.md.\n\n"
                 "- Keep every existing behaviour and data flow; this is a visual and layout change only.\n"
                 "- Use the colour tokens and spacing scale, never raw hex values.\n"
                 "- It must work at 1500, 1100, 700 and 420 pixels wide, in light and dark themes, with nothing clipped.\n"
                 "- Attach before and after screenshots to the final report."},
        {"id": "p_bug_regression", "name": "Fix a bug with a regression test", "template": "bugfix",
         "text": "Fix this bug: <what happens>.\n\nSteps to reproduce:\n1. <step>\n\nExpected: <expected>\nActual: <actual>\n\n"
                 "First write a test that fails because of the bug, then make it pass with the smallest change "
                 "that addresses the root cause. Run the full test suite before reporting."},
    ],
    "recent_repos": [],
    "model_recent": {"codex": [], "claude": [], "gemini": []},
}

_lock = threading.RLock()


def _migrate(cfg: dict) -> dict:
    """Bring older config files (v13) forward."""
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(cfg, dict):
        return out
    for k, v in cfg.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            merged = copy.deepcopy(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    # v13 model fields
    if cfg.get("codex_planner_model") and not out["roles"]["supervisor"].get("model"):
        out["roles"]["supervisor"]["model"] = cfg["codex_planner_model"]
    if cfg.get("claude_worker_model") and not out["roles"]["worker"].get("model"):
        out["roles"]["worker"]["model"] = cfg["claude_worker_model"]
    if "max_review_rounds" in cfg and cfg["max_review_rounds"] > 6:
        out["max_review_rounds"] = 3
    for r in ROLES:
        out["roles"].setdefault(r, {"agent": "", "model": "", "effort": ""})
        out["roles"][r].setdefault("agent", "")
        out["roles"][r].setdefault("model", "")
        out["roles"][r].setdefault("effort", "")
    out.setdefault("models", {})
    if isinstance(out.get("subagent_models"), dict):
        out["subagent_models"]["codex"] = ""
        out["subagent_models"]["gemini"] = ""
    out.setdefault("agent_defaults", {})
    for a in AGENTS:
        out["agent_env"].setdefault(a, {})
        d = out["agent_defaults"].get(a)
        if not isinstance(d, dict):
            d = {}
        d.setdefault("model", "")
        d.setdefault("effort", "")
        out["agent_defaults"][a] = d
        out["model_recent"].setdefault(a, [])
        if not isinstance(out["models"].get(a), list):
            out["models"][a] = list(DEFAULT_MODELS[a])
    out["saved_prompts"] = _clean_prompts(out.get("saved_prompts"))
    out["build"] = BUILD
    return out


def _clean_prompts(rows) -> list:
    """Keep saved prompts well formed whatever the browser or a hand edit sent."""
    import uuid
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        name, text = str(r.get("name") or "").strip()[:120], str(r.get("text") or "")[:20000]
        if not name and not text.strip():
            continue
        out.append({"id": str(r.get("id") or f"p_{uuid.uuid4().hex[:8]}")[:40], "name": name or "Untitled prompt",
                    "text": text, "template": str(r.get("template") or "")[:40]})
    return out[:100]


def _env_overrides() -> dict:
    """Settings forced by the environment, e.g. RELAY_CFG_codex_sandbox=danger-full-access.

    Values are parsed as JSON when possible (numbers, booleans, lists), otherwise
    taken as plain strings. They apply on every load but are never written back to
    config.json, so a container can pin a setting without editing the file.
    """
    import json as _json
    import os as _os
    out = {}
    for name, value in _os.environ.items():
        if not name.startswith("RELAY_CFG_"):
            continue
        # Setting keys are lower-case; Windows upper-cases environment variable names.
        key = name[len("RELAY_CFG_"):].lower()
        if key not in DEFAULTS:
            continue
        try:
            out[key] = _json.loads(value)
        except Exception:
            out[key] = value
    return out


def _load_file() -> dict:
    raw = read_json(CONFIG_PATH, {})
    cfg = _migrate(raw)
    if cfg != raw:
        write_json(CONFIG_PATH, cfg)
    return cfg


def load() -> dict:
    with _lock:
        cfg = _load_file()
        cfg.update(_env_overrides())
        return cfg


def save(cfg: dict) -> dict:
    with _lock:
        cfg = _migrate(cfg)
        write_json(CONFIG_PATH, cfg)
        return cfg


def update(partial: dict) -> dict:
    with _lock:
        cfg = _load_file()  # file values only, so environment overrides are never persisted
        for k, v in (partial or {}).items():
            if k not in DEFAULTS:
                continue
            if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
                merged = copy.deepcopy(cfg.get(k, {}))
                for kk, vv in v.items():
                    if isinstance(merged.get(kk), dict) and isinstance(vv, dict):
                        merged[kk] = {**merged[kk], **vv}
                    else:
                        merged[kk] = vv
                cfg[k] = merged
            else:
                cfg[k] = v
        saved = save(cfg)
        saved.update(_env_overrides())
        return saved


def remember_repo(path: str) -> None:
    if not path:
        return
    cfg = load()
    rec = [p for p in cfg.get("recent_repos", []) if p.lower() != path.lower()]
    rec.insert(0, path)
    update({"recent_repos": rec[:12]})


def remember_model(agent: str, model: str) -> None:
    if not model or agent not in AGENTS:
        return
    cfg = load()
    rec = cfg.get("model_recent", {}).get(agent, [])
    rec = [m for m in rec if m != model]
    rec.insert(0, model)
    mr = cfg.get("model_recent", {})
    mr[agent] = rec[:8]
    update({"model_recent": mr})


def preset(pid: str):
    for p in PRESETS:
        if p["id"] == pid:
            return p
    return None


def public_view(cfg: dict) -> dict:
    """Config subset that the browser needs."""
    keys = [k for k in DEFAULTS.keys()]
    return {k: cfg.get(k) for k in keys}
