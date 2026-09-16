# Changelog

All notable changes to Relay are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [14.1.0] — 2026-09-16

### Changed

- Renamed the project from DevOrchestrator to **Relay**, after the way agents
  hand structured messages to each other. Brand mark, favicon, window title,
  agent prompts and docs all follow. Preferences saved under the old
  `devorch.*` browser keys migrate automatically on first load.
- The `RELAY_PORT` environment variable replaces `DEVORCH_PORT`.

### Added

- **Per-role model and reasoning effort.** Each of supervisor, worker and
  reviewer picks its own model and effort, in the task wizard or in settings.
  Codex accepts low through xhigh, Claude adds max, Gemini has no effort
  control and is shown as unsupported.
- **Per-agent defaults.** Model, effort and subagent model now sit together on
  each agent card. A role that leaves a field blank falls back to the agent
  default, then to the CLI's own configured default.
- **Cheaper subagents.** Helper agents spawned mid-turn run on a small model:
  Claude through `CLAUDE_CODE_SUBAGENT_MODEL`, Codex through
  `agents.<role>.model` overrides.
- **Usage & budget settings.** Lean prompts send each role only the rules it
  needs. Caps on the reviewer diff, worker reports, failed-check output,
  changed-file lists and stored tool output. Passing checks send only their
  verdict. Thrifty and Thorough presets apply a whole profile at once.
- **Estimated spend.** Agents that report tokens but not cost are priced from
  an editable table and marked with `~`, since subscription usage is not
  billed per token.
- **Start any task immediately**, bypassing the queue limit, from the task
  header or the hover controls on each sidebar row. Tasks run genuinely in
  parallel, each with its own worktree, processes and sessions.
- **Resizable, collapsible task list.** Drag the edge, double-click to reset,
  press `B` to toggle. On narrow screens it becomes an overlay drawer.
- `rules/DESIGN.md`, the house UI and layout rules, loaded only for tasks that
  touch the interface.
- `tools/recover_tasks.py`, which rebuilds task records from `runtime/` history
  if the state file is ever lost.

### Fixed

- **Task loss from concurrent instances.** A second server would overwrite the
  first one's task list. Relay now refuses to start twice, merges unknown task
  ids on write instead of clobbering them, and keeps rolling backups in
  `state/backups/`.
- **The queue no longer forgets itself.** Its running state persists, and
  interrupted runs re-queue on startup and continue from their checkpoint.
- **Resume no longer requires agent sessions.** A task with a live worktree
  continues in it rather than failing on an already-checked-out branch.
- Codex `exec resume` rejects `-s`; the sandbox is now set through a config
  override on both the fresh and resumed paths.
- Waitress rejects a hop-by-hop `Connection` header, which broke the event
  stream. Removed.
- Filter chips wrap instead of hiding behind an invisible scrollbar, inspector
  tabs scroll with a visible affordance and keep the active tab in view, and
  the header and agent roster wrap rather than pushing content off screen.
- Shell wrapper prefixes are stripped from displayed commands, so the
  conversation shows `git status` rather than a full PowerShell invocation.

## [14.0.0] — 2026-09-16

### Added

- Rewrite into the `orchestrator/` package and an ES-module browser workspace.
- Real agent-to-agent dialogue over persistent CLI sessions, carried by
  structured JSON envelopes defined in `rules/PROTOCOL.md`.
- Any agent in any role, with presets and a fully custom mapping.
- Live structured conversation: tool calls, results, reasoning, verification,
  hand-offs and decisions, with per-role token and cost accounting.
- Human control points: questions, queued guidance, immediate interrupt,
  approval before delivery, pause, stop and checkpointed resume.
- Dashboard, inspector tabs, repository editor, command palette, notifications,
  GitHub issue intake and draft pull request delivery.
