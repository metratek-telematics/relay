# Relay

**Your coding agents, working as a team.**

Relay puts Codex, Claude Code and Gemini CLI on the same job. One agent supervises, another implements, an
optional third reviews independently. They pass structured messages back and forth over persistent sessions,
use all of their own tools, and you can step in at any moment from the browser.

![version](https://img.shields.io/badge/version-14.1.0-d97757)
![license](https://img.shields.io/badge/license-MIT-3a8f62)
![python](https://img.shields.io/badge/python-3.10%2B-4f6fd0)
![platform](https://img.shields.io/badge/platform-Windows-8a5fc7)

```text
                     ┌──────────────── you (browser) ────────────────┐
                     │ guidance · answers · approvals · edits · stop │
                     └───────────────┬───────────────────────────────┘
                                     ▼
  task ─► isolated git worktree ─► SUPERVISOR plans ──► work package ──► WORKER implements
                                        ▲                                      │
                                        │   verification (pytest / npm test…)  │
                                        └────── report + diff + checks ◄───────┘
                                        │
                              decision: revise ─► back to worker
                              decision: done   ─► (REVIEWER independent PASS/FAIL)
                                               ─► commit ─► push ─► draft PR
```

## Why Relay

- **Real agent-to-agent dialogue.** Supervisor, worker and reviewer exchange JSON envelopes over persistent CLI
  sessions, so every agent remembers the whole task and messages between them stay short.
- **Any agent in any role.** Presets such as *Codex supervises Claude* or a three-vendor panel, or map each role
  yourself.
- **Your model, your effort, your budget.** Pick the model and reasoning effort per role, run helper subagents on a
  cheap model, and cap what gets re-sent each turn.
- **Parallel by design.** Every task gets its own worktree, processes and sessions. Start as many as you like.
- **You stay in control.** Answer agent questions, queue guidance, interrupt a running turn, require approval before
  delivery, pause and resume from a checkpoint.
- **Nothing merges on its own.** Relay commits, pushes a task branch and opens a **draft** pull request. You merge.

## Requirements

- Windows, Python 3.10+ and Git.
- At least two of [Codex CLI](https://github.com/openai/codex), [Claude Code](https://claude.com/claude-code) and
  [Gemini CLI](https://github.com/google-gemini/gemini-cli), each installed and signed in.
- Optionally the [GitHub CLI](https://cli.github.com/) for issue intake and draft pull requests.

Relay never proxies your credentials. It launches the CLIs you have already authenticated.

## Quick start

1. Double-click `run.bat`. It installs Flask and waitress on first run.
2. Open <http://127.0.0.1:8767>.
3. Go to **Agents** and press **Test all** to confirm each CLI answers.
4. Press **New task**, choose a repository, describe the change and pick a team.

The full walkthrough, presets and troubleshooting are in [docs/RUNNING.md](docs/RUNNING.md).

## Keeping usage down

Every agent turn re-sends the conversation so far, so the loop count is the biggest cost lever. Relay gives you the
rest under **Settings → Usage & budget**: lean prompts, a cheap subagent model, output caps, and one-click Thrifty
and Thorough presets. Details in [docs/RUNNING.md](docs/RUNNING.md#models-effort-and-usage).

## Documentation

| Document | What it covers |
|---|---|
| [docs/RUNNING.md](docs/RUNNING.md) | Install, first task, presets, models and usage, troubleshooting |
| [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) | A task from start to finish, and how to steer the team |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, sessions, the protocol, persistence |
| [rules/](rules/) | The policies agents receive, including [DESIGN.md](rules/DESIGN.md) for UI work |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Project layout, house rules, adding an agent |
| [SECURITY.md](SECURITY.md) | What Relay may do, the trust boundary, reporting issues |

## Safety

Relay runs agents with their approval prompts bypassed, inside an isolated Git worktree. It binds to `127.0.0.1`
with no authentication, so keep it on your own machine and never expose the port. Read
[SECURITY.md](SECURITY.md) before pointing it at anything important.

## License

[MIT](LICENSE) © 2026 Metratek Telematics.

Relay orchestrates third-party tools you install yourself. Your use of Codex, Claude Code, Gemini CLI and the GitHub
CLI remains governed by each vendor's own terms.
