# Contributing to Relay

Relay is a local-first tool. There is no build step, no bundler and no test
harness to install: clone it, run it, edit it.

## Getting set up

```powershell
python -m pip install -r requirements.txt
python web_app.py            # or double-click run.bat
```

Open <http://127.0.0.1:8767>. The browser workspace is plain ES modules served
straight from `web/`, so a refresh picks up every change. Only one instance can
run at a time; a second launch refuses to start so the two cannot overwrite
each other's task file.

## Where things live

| Path | Purpose |
|---|---|
| `web_app.py` | HTTP API and the Server-Sent Events stream |
| `orchestrator/agents.py` | One adapter per CLI: command building, session resume, stream parsing |
| `orchestrator/pipeline.py` | The supervisor, worker and reviewer dialogue |
| `orchestrator/protocol.py` | Envelope parsing and every prompt |
| `orchestrator/runner.py` | Subprocess streaming, heartbeats, stop, pause, interrupt |
| `orchestrator/manager.py` | Scheduler, task lifecycle, GitHub intake, metrics |
| `orchestrator/store.py` | Task persistence and the append-only message log |
| `web/js/` | Browser workspace |
| `rules/` | Policies injected into agent prompts |
| `tools/` | Maintenance scripts |

## House rules

**Python.** Standard library plus Flask and waitress. Keep it that way unless a
dependency earns its place. Comments explain why, never what.

**Frontend.** No framework, no build. Follow `rules/DESIGN.md`, which is the
same document the agents receive for UI work. The parts that matter most:
every flex or grid child that must shrink carries `min-width: 0`, nothing is
clipped without a way to reach it, and colours come from tokens rather than
hex values.

**Adding an agent.** Subclass `AgentAdapter` in `orchestrator/agents.py` and
implement `build`, `parse_line` and, if needed, `finalize`. Register it in
`AGENTS` in `orchestrator/config.py` with its label, binary, colour and the
effort levels its CLI actually supports. Verify the flags against the real CLI
help output rather than assuming; every adapter in the tree was written that
way, and several assumptions turned out to be wrong.

**Changing prompts.** Prompts live in `orchestrator/protocol.py` and the rule
files. Remember that every agent turn re-sends the conversation so far, so
anything you add to a repeated prompt is paid for on each later turn. Check
`rule_names_for` before adding a rule file to a role.

## Before you open a pull request

1. `python -m pyflakes orchestrator/*.py web_app.py` reports nothing new.
2. `node --input-type=module --check < web/js/<file>.js` passes for each file
   you touched.
3. Run a real task end to end against a scratch repository. Unit tests cannot
   tell you that an agent adapter still parses its CLI's stream correctly.
4. Check the UI at 1500, 1100 and 700 pixels wide.
5. Add a `CHANGELOG.md` entry under Unreleased.

## Reporting a problem

Include the Relay version from the sidebar, the agent CLI versions from the
Agents page, and the relevant part of `runtime/<task-id>/raw.log`. That log
holds the exact commands and the raw agent stream, which is almost always
enough to identify the cause.

## Security

Relay runs shell commands and edits files on your machine by design. Report
anything that escapes the intended boundary, such as a path that leaves the
task worktree, privately rather than in a public issue. See `SECURITY.md`.
