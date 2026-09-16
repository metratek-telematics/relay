# Security

## What Relay is allowed to do

Relay exists to run coding agents unattended, so by design it will, on your
machine and with your credentials:

- create Git worktrees and branches, and read and write files inside them;
- run shell commands, including your test and build commands;
- run the Codex, Claude Code and Gemini CLIs with their approval prompts
  bypassed, which means those agents can edit files and execute commands
  without asking;
- commit, push task branches, and open **draft** pull requests through `gh`.

## What it will not do

- Merge a pull request, or push to a default or protected branch.
- Force-push, delete branches, or rewrite shared history.
- Deploy, restart services, or touch production infrastructure.
- Send your code or credentials anywhere except to the agent CLIs you have
  already installed and signed in yourself. There is no Relay server.

## Trust boundary

Relay binds to `127.0.0.1` and has **no authentication**. Anyone who can reach
that port can create tasks, and a task can run arbitrary commands. Therefore:

- Do not bind it to `0.0.0.0`, expose it through a tunnel, or put it behind a
  reverse proxy on a shared machine.
- Treat it as a single-user, single-machine tool. Multi-user use needs
  authentication and per-user credential isolation that Relay does not yet
  have.
- Point it only at repositories you own or are authorised to change.

## Credentials

Relay never stores or proxies agent credentials. Each CLI authenticates itself
against your own account using its own token store (`~/.codex`, `~/.claude`,
`~/.gemini`, `gh auth`). Relay only launches those binaries.

Environment variables you set per agent in Settings are written to
`config.json` in plain text. Do not put long-lived secrets there if the folder
is synced or shared. Prefer the CLI's own login.

`config.json`, `state/` and `runtime/` can contain repository content and agent
output. `runtime/<task-id>/raw.log` holds the full agent stream, which may
include file contents. These paths are gitignored, but treat them as sensitive
when sharing a folder or a bug report.

## Prompt injection

Agents read repository content, GitHub issue text and command output. A
malicious repository or issue can attempt to steer an agent. Mitigations in
place: agents work inside an isolated worktree, the orchestrator performs all
Git delivery rather than the agents, and nothing merges automatically. The
final safeguard is your own review of the draft pull request. Review the diff,
not just the summary.

## Reporting a vulnerability

Report anything that crosses the boundaries above privately through GitHub's
[private vulnerability reporting](https://github.com/metratek-telematics/relay/security/advisories/new),
rather than opening a public issue. Please include the version, the
steps to reproduce, and the relevant `runtime/<task-id>/raw.log` excerpt.
Expect an acknowledgement within a few days.
