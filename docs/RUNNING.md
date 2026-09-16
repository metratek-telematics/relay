# Running Relay on Windows

## Install and sign in once
```powershell
winget install Python.Python.3.12 Git.Git GitHub.cli   # if missing
npm install -g @openai/codex @anthropic-ai/claude-code @google/gemini-cli
codex          # sign in with ChatGPT, then exit
claude         # /login, then exit
gemini         # choose Google login (Workspace accounts also need $env:GOOGLE_CLOUD_PROJECT)
gh auth login  # optional: issue intake + draft PRs
```

## Start
Double-click `run.bat`, or from a terminal:
```powershell
python web_app.py
```
Open <http://127.0.0.1:8767>. Set `RELAY_PORT` to use another port. Pass `--no-browser` to skip auto-opening.

## First task
1. **Agents** page → *Test all* to confirm each CLI answers and streams correctly.
2. **New task** → choose a Git repository (it must have at least one commit) → describe the change → pick a preset → *Create & queue*.
3. Watch the **Team conversation**. Use the composer to guide, answer questions or approve delivery.
4. When delivered, open the draft PR (if the repo has a GitHub remote) or inspect the branch in the worktree.

## Presets
| Preset | Supervisor | Worker | Reviewer |
|---|---|---|---|
| Codex supervises Claude (default) | Codex | Claude | – |
| Claude supervises Codex | Claude | Codex | – |
| Codex → Claude + independent Codex review | Codex | Claude | Codex |
| Three-agent panel | Codex | Claude | Gemini |
| Claude supervises Gemini | Claude | Gemini | – |
| Claude pair / Codex pair | same vendor, two sessions | | – |

Any role can be remapped per task; models are free-text and passed straight to the CLI (`--model` / `-m`).

## Models, effort and usage

Every role picks its own model and reasoning effort, in the new-task wizard (step 3) or under Settings → Workflow:

- **Model** — a dropdown of the catalog for that agent plus "Custom…" for any exact id. Blank means the CLI's own default, shown in the option label (read from `~/.codex/config.toml`, `~/.claude/settings.json`, `~/.gemini/settings.json`).
- **Reasoning effort** — Codex `low|medium|high|xhigh`, Claude `low|medium|high|xhigh|max`. The Gemini CLI has no effort control, so the picker is disabled.
- Edit the catalog per agent under Settings → Agents → Model catalog.

### Keeping usage down
Settings → **Usage & budget**:

- **Cheaper subagents** — helper agents spawned by the main agent run on a small model. Claude via `CLAUDE_CODE_SUBAGENT_MODEL`, Codex via `agents.<role>.model` overrides. Defaults: `haiku` and `gpt-5.1-codex-mini`.
- **Lean prompts** — each role gets only the rules it needs, plus rule files matching the task text, instead of all fourteen.
- **Prompt budget** — caps on the diff sent to the reviewer, the worker report echoed to the supervisor, failed-check output, the changed-file list and stored tool output. Passing checks send only their verdict.
- **Thrifty / Thorough presets** — one click each.

The largest lever is the loop itself: every extra work package or review round re-sends the whole session. Keep max work packages at 4–6 and review rounds at 1–2 unless the change is risky.

## Where things are
- `config.json` – defaults (also editable in Settings)
- `state/tasks.json` – tasks
- `runtime/<task-id>/` – conversation, artifacts, raw log
- `worktrees/` – isolated checkouts (delete a task with *Delete* to remove its worktree)
- `managed-repos/` – clones created by GitHub intake

## Troubleshooting
- **Backend offline** – keep the `run.bat` window open; the browser reconnects automatically.
- **Agent “not ready”** – run the CLI once in a terminal to finish sign-in; Gemini Workspace accounts need `GOOGLE_CLOUD_PROJECT` (Settings → Agents → environment).
- **Task interrupted after a restart** – open it and press *Resume*; the same agent sessions continue from the checkpoint.
- **Turn budget exhausted** – raise *Max work packages* in the task menu (Edit task settings) and resume.
- **Codex cannot write files** – choose the `danger-full-access` sandbox in Settings → Agents if your repository lives outside the sandboxed worktree roots.
