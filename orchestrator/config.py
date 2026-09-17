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
for _a in AGENTS.values():
    _a["builtin"] = True

# The agent pack: more coding CLIs Relay can install from the Agents page and drive like the
# built-in three. They start uninstalled and unconfigured; nothing runs until someone signs in.
#   install: how the Agents page installs it (see orchestrator/installer.py)
#   auth:    what counts as signed in: any of these env vars, or any of these files under HOME
#   login:   command to run once in a terminal (browser VS Code shares the same home folder)
PACK_AGENTS = {
    "opencode": {"label": "OpenCode", "vendor": "SST", "binary": "opencode", "color": "#211e1e",
                 "efforts": ["minimal", "low", "medium", "high", "max"],
                 "install": {"kind": "npm", "package": "opencode-ai"},
                 "auth": {"env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"],
                          "files": [".local/share/opencode/auth.json"]},
                 "login": "opencode auth login", "models_hint": "provider/model, e.g. anthropic/claude-sonnet-5",
                 "docs": "https://opencode.ai/docs/cli/"},
    "kilo": {"label": "Kilo Code", "vendor": "Kilo", "binary": "kilo", "color": "#f8f675",
             "efforts": ["minimal", "low", "medium", "high", "max"],
             "install": {"kind": "npm", "package": "@kilocode/cli"},
             "auth": {"env": ["KILO_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"],
                      "files": [".local/share/kilo/auth.json"]},
             "login": "kilo auth login", "models_hint": "provider/model, e.g. kilo/anthropic/claude-sonnet-5",
             "docs": "https://kilo.ai/docs/cli"},
    "copilot": {"label": "GitHub Copilot", "vendor": "GitHub", "binary": "copilot", "color": "#000000",
                "efforts": ["low", "medium", "high"],
                "install": {"kind": "npm", "package": "@github/copilot"},
                "auth": {"env": ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"],
                         "files": [".copilot/config.json"]},
                "login": "copilot login   (answer y to store the token in plain text)", "models_hint": "e.g. claude-sonnet-5, gpt-5",
                "docs": "https://docs.github.com/copilot/how-tos/use-copilot-agents/use-copilot-cli"},
    "cline": {"label": "Cline", "vendor": "Cline", "binary": "cline", "color": "#000000",
              "efforts": ["low", "medium", "high"], "resume": False,
              "install": {"kind": "npm", "package": "cline"},
              "auth": {"files": [".cline/data/settings/providers.json"]},
              "login": "cline auth --provider anthropic --apikey <key> --modelid claude-sonnet-5",
              "models_hint": "provider:model, e.g. anthropic:claude-sonnet-5",
              "docs": "https://docs.cline.bot/cline-cli/overview"},
    "qwen": {"label": "Qwen Code", "vendor": "Alibaba", "binary": "qwen", "color": "#615ced",
             "efforts": [],
             "install": {"kind": "npm", "package": "@qwen-code/qwen-code"},
             "auth": {"env": ["OPENAI_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"],
                      "files": [".qwen/settings.json"]},
             "login": "qwen   (then choose an API provider)", "models_hint": "e.g. qwen3-coder-plus",
             "docs": "https://qwenlm.github.io/qwen-code-docs/"},
    "amp": {"label": "Amp", "vendor": "Amp", "binary": "amp", "color": "#f34e3f",
            "efforts": ["low", "medium", "high", "ultra"],
            "install": {"kind": "npm", "package": "@ampcode/cli"},
            "auth": {"env": ["AMP_API_KEY"], "files": [".local/share/amp/secrets.json"]},
            "login": "amp login", "models_hint": "Amp picks the model; effort sets its mode",
            "docs": "https://ampcode.com/manual"},
    "cursor": {"label": "Cursor", "vendor": "Anysphere", "binary": "cursor-agent", "color": "#000000",
               "efforts": ["low", "medium", "high"],
               # The script installs under $HOME/.local; point it at Relay's agents folder and link the binary.
               "install": {"kind": "script", "command": "curl -fsS https://cursor.com/install | HOME=\"$PREFIX\" bash && ln -sf \"$PREFIX/.local/bin/cursor-agent\" \"$BIN_DIR/cursor-agent\""},
               "auth": {"env": ["CURSOR_API_KEY", "CURSOR_AUTH_TOKEN"], "files": [".config/cursor/auth.json"]},
               "login": "cursor-agent login", "models_hint": "e.g. gpt-5, sonnet-4.5",
               "docs": "https://cursor.com/docs/cli/overview"},
    "crush": {"label": "Crush", "vendor": "Charm", "binary": "crush", "color": "#6b50ff",
              "efforts": ["low", "medium", "high"],
              "install": {"kind": "npm", "package": "@charmland/crush"},
              "auth": {"env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "HYPER_API_KEY"],
                       "files": [".local/share/crush/crush.json", ".config/crush/crush.json"]},
              "login": "crush login", "models_hint": "provider/model, e.g. anthropic/claude-sonnet-5",
              "docs": "https://github.com/charmbracelet/crush"},
    "goose": {"label": "Goose", "vendor": "Block", "binary": "goose", "color": "#000000",
              "efforts": [],
              "install": {"kind": "script", "command": "curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | CONFIGURE=false GOOSE_BIN_DIR=\"$BIN_DIR\" bash"},
              "auth": {"env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY"],
                       "files": [".config/goose/config.yaml"]},
              "login": "goose configure", "models_hint": "provider:model, e.g. anthropic:claude-sonnet-5, or a model for GOOSE_PROVIDER",
              "docs": "https://block.github.io/goose/docs/guides/goose-cli-commands"},
    "aider": {"label": "Aider", "vendor": "Aider", "binary": "aider", "color": "#14b014",
              "efforts": ["low", "medium", "high"], "edit_only": True,
              "install": {"kind": "pip", "package": "aider-chat"},
              "auth": {"env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"],
                       "files": [".aider/oauth-keys.env"]},
              "login": "set an API key under Configure", "models_hint": "e.g. sonnet, gpt-5, openrouter/…",
              "docs": "https://aider.chat/docs/usage.html"},
    "continue": {"label": "Continue", "vendor": "Continue", "binary": "cn", "color": "#000000",
                 "efforts": [],
                 "install": {"kind": "npm", "package": "@continuedev/cli"},
                 "auth": {"env": ["ANTHROPIC_API_KEY"],  # cn only auto-configures from this key; others need config.yaml
                          "files": [".continue/config.yaml", ".continue/auth.json"]},
                 "login": "cn   (then type /login)", "models_hint": "blank = model from ~/.continue/config.yaml; or a Continue Hub slug owner/model",
                 "docs": "https://docs.continue.dev/cli/overview"},
}
AGENTS.update(PACK_AGENTS)
for _id, _a in AGENTS.items():
    _a["logo"] = f"agents/{_id}.svg"  # web/agents, see SOURCES.md there

# Editable model catalog shown in the model pickers (Settings → Agents). Free text is always allowed too.
DEFAULT_MODELS = {
    "codex": ["gpt-6-astra", "gpt-5.3-codex", "gpt-5.2-codex", "gpt-5.1-codex-max", "gpt-5.1-codex-mini"],
    "claude": ["fable[1m]", "opus", "sonnet", "haiku", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-pro-preview"],
    "opencode": ["anthropic/claude-sonnet-5", "anthropic/claude-opus-5", "openai/gpt-5.3-codex"],
    "kilo": ["kilo/anthropic/claude-sonnet-5", "kilo/openai/gpt-5.3-codex"],
    "copilot": ["claude-sonnet-5", "gpt-5.3-codex", "gpt-5"],
    "cline": ["anthropic:claude-sonnet-5", "openai:gpt-5.3-codex"],
    "qwen": ["qwen3-coder-plus", "qwen3-coder-flash"],
    "amp": [],
    "cursor": ["auto", "sonnet-4.5", "gpt-5"],
    "crush": ["anthropic/claude-sonnet-5", "openai/gpt-5.3-codex"],
    "goose": ["claude-sonnet-5", "gpt-5.3-codex"],
    "aider": ["sonnet", "opus", "gpt-5"],
    "continue": [],
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
    # judge: how the supervisor's verdicts are held to the acceptance contract
    "judge_gate_nudges": 2,                  # done attempts refused for missing evidence before the human is asked
    "judge_revise_nudges": 1,                # revisions refused for missing ids / non-blocking-only findings before one is accepted
    "judge_budget_extension": 3,             # work packages granted when the budget runs out and the team continues
    "judge_escalation_timeout_minutes": 120,  # unanswered judge questions take the safe automatic choice after this (0 = wait)
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
    # Design gate: enforced checks on what agents add (token colours, fonts, forbidden terms); see orchestrator/designcheck.py.
    "design_gate": True,
    # Relay installs a task's dependencies itself before agents start (npm ci and friends).
    "env_prepare": True,
    "env_prepare_timeout_minutes": 20,
    # Browser VS Code (code-server) base URL, e.g. https://relay.example.com/code; enables Open in VS Code and previews.
    "ide_url": "",
    "design_forbidden_terms": [],
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
    "issues_comment_on_pickup": False,   # Issues board: comment "Relay picked this up" on the issue
    "issues_pickup_label": "",           # Issues board: label added to an issue when a task is created from it
    "queue_stop_chain_on_failure": False,  # one-after-the-other chains keep going after a failed task
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
            out["models"][a] = list(DEFAULT_MODELS.get(a, []))
        out["pricing"].setdefault(a, {"input": 0.0, "cached": 0.0, "output": 0.0})
        out["subagent_models"].setdefault(a, "")
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
