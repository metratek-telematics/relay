# Architecture

## Components
```
browser (web/)  ──SSE/HTTP──►  web_app.py (Flask + waitress)
                                   │
                                   ▼
                              Manager (orchestrator/manager.py)
                     scheduler · control actions · event fan-out · GitHub watcher
                                   │ launches one Runner per task
                                   ▼
                     Pipeline (orchestrator/pipeline.py)  ── uses ──►  gitops / github / protocol
                                   │ run_role(role, prompt)
                                   ▼
                     Runner (orchestrator/runner.py)  ── spawns ──►  Agent CLI subprocess
                                   │ parse_line()                      (codex | claude | gemini)
                                   ▼
                     AgentAdapter (orchestrator/agents.py) → normalized events → messages.jsonl + SSE
```

## Roles and sessions
A task has a `workflow` with three roles: `supervisor`, `worker`, optional `reviewer`. Each role maps to an agent
(`codex`, `claude`, `gemini`) and an optional model. Each role owns **one persistent CLI session** for the whole task:

| Agent | Fresh turn | Follow-up turn | Stream format |
|---|---|---|---|
| Claude | `claude -p --output-format stream-json --verbose --session-id <uuid>` | `--resume <uuid>` | stream-json (assistant / user / result) |
| Codex | `codex exec --json -o last.txt -` | `codex exec resume <thread_id> --json -` | JSONL (`thread.started`, `item.*`, `turn.completed`) |
| Gemini | `gemini -o stream-json -y` | `--resume latest` (per worktree) | stream-json (`init`, `message`, `tool_use`, `tool_result`, `result`) |

Because sessions persist, inter-agent messages are just the *new* information ("REPORT FROM WORKER …", "MESSAGE FROM
SUPERVISOR …"); rules and the full task briefing are sent once per session.

## Protocol
Every agent reply ends with one fenced JSON envelope (`rules/PROTOCOL.md`):
`plan`, `instruction`, `decision(done|revise)`, `question(to user|worker)`, `report(complete|partial|blocked)`, `review(PASS|FAIL)`.
`protocol.parse_envelope()` finds the last valid envelope (fenced or bare). Missing envelopes trigger a short nudge turn.

## Pipeline phases
1. **prepare** – create/attach the git worktree and branch, snapshot uncommitted user changes, load issue text, detect verification commands.
2. **kickoff** – supervisor inspects the repository and returns `plan` (plan, acceptance criteria, first work package).
3. **dialogue** – loop: worker turn → verification → supervisor evaluation → `instruction` / `revise` / `done` / `question`.
4. **review** – optional independent reviewer; FAIL routes findings back to the supervisor (bounded rounds).
5. **deliver** – optional human approval gate → commit → push → draft PR → `REPORT.md`.

The pipeline writes a `checkpoint` (phase, turn, awaiting role, plan, last instruction) into the task after each step.
`Resume` re-enters the loop with the same sessions, prefixing the next prompt with an orchestrator note.

## Human control points
- **Guidance** (`POST /api/tasks/<id>/guidance`) – queued for the next turn of a chosen role, or `mode=interrupt` to kill the current agent process and restart that turn with the note.
- **Questions** – an agent envelope `question` sets `task.pending`; the run blocks until `answer`.
- **Approval** – with `approval_before_delivery`, delivery waits for `approve` / `reject(note)`; a rejection becomes a revision request to the supervisor.
- **Pause / Stop / Retry / Retry-fresh** – runner flags checked at safe boundaries; stop kills the process tree.

## Persistence
- `state/tasks.json` – task metadata (status, workflow, checkpoint, sessions, metrics, timeline).
- `runtime/<task>/messages.jsonl` – append-only conversation; patches (`_patch`) update streaming/tool messages in place.
- `runtime/<task>/events.jsonl`, `raw.log`, `PLAN.md`, `ACCEPTANCE.md`, `IMPLEMENTATION.md`, `VERIFICATION.md`, `REVIEW.md`, `REPORT.md`, `PR_BODY.md`.
- `worktrees/<repo>-<id>` – isolated checkouts on branch `<prefix>/<slug>-<id>`.

## Browser
ES modules without a build step. Five places, each a view in `web/js/views/`:

| Place | Route | Module | Data |
| --- | --- | --- | --- |
| Mission Control (home; `#/home/24h` etc. is the digest) | `#/`, `#/home/<range>` | `mission.js` | `/api/mission` (`orchestrator/mission.py`: digest + live activity + 14-day trend) |
| Work board | `#/work` | `work.js` | task list from the event stream, `/api/issues` |
| Task page | `#/task/<id>/<view>` | `task.js`, `theatre.js` (Live), `changes.js` (Changes), `inspector.js` (details rail, Design, Checks, Logs) | messages over SSE, `/api/tasks/<id>/changes`, `/repo-diff` |
| Review cockpit, shareable status | `#/review/<id>`, `#/status/<id>` | `review.js`, `status.js` | `/api/tasks/<id>/merge` merges the change set in order |
| Knowledge | `#/knowledge/<tab>` | `knowledge.js` hosting `repos.js`, `systemmap.js`, `connectors.js`, `lessons.js`, `learning.js`, `github.js` | |
| Agents, Settings | `#/agents`, `#/settings/<section>` | `agents.js`, `settings.js` | |

`app.js` holds the shell (navigation, redirects from older addresses such as `#/inbox`, `#/digest`, `#/repos/*`, the event
stream, keyboard), `live.js` what each running task is doing now (phases, current tool call), `commands.js` the command
palette ("create task: …" with repository detection). Styling: `tokens.css` (the only literal colours; light and dark
themes, type, spacing, motion), `styles.css` (shared components), `app.css` (shell and Mission Control screens).
`ui.js` contains the safe markdown renderer, icons, toasts, modals, menus and the command palette.
