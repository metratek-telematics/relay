# Changelog

All notable changes to Relay are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Prepared environments.** Relay installs a task's dependencies itself before any agent starts, using the
  repository's lockfile (`npm ci`, pnpm or yarn), the registry credentials mounted into Relay and a shared package
  cache. The result shows on the task Overview; a failure becomes a blocked check instead of an agent workaround. Tasks
  can override the command with `setup_command`.
- **A browser for agents.** The image ships headless Chromium and `relay-screenshot`, and workers are told how to run
  the app and look at it in both themes, printing console errors and failed requests with each screenshot.
- **Browser VS Code.** An optional code-server service (`--profile ide`) with the same Node toolchain. With its URL set,
  tasks and repositories get Open in VS Code, and Try it adds a Preview step: the dev-server command with the right
  base path and a link through code-server's port proxy.
- **Design gate.** Design rules are enforced, not just described. On every line agents add, Relay fails verification
  (and so blocks delivery) for a literal colour outside the token files, a font outside the design system, gradient
  text, or a forbidden term; `!important`, deep selector overrides and `backdrop-filter` are reported as warnings.
  Forbidden terms are set in Settings → Verification, stored only in Relay's settings, and matched case-insensitively
  with words joined by any separator, so a test regex naming the term is caught too. Workers get the command to run
  it themselves; repositories can add token files and fonts in `.relay/design-checks.json`.
- **Clone from GitHub in the New task wizard.** Pick any repository the signed-in
  `gh` account can reach, or type a git URL, and Relay clones it into `RELAY_REPOS`
  (or the managed repositories folder) and selects it. An existing clone is fetched
  and reused instead of cloned again.
- **Try it tab.** Every task with a branch gets copy-paste commands in five steps: see what changed, run it from
  the worktree (detected from package.json scripts, Django or a static index.html), get it onto your computer
  (`gh pr checkout` or `git switch`) or push it if it only exists locally, accept it (merge the PR, or merge the
  branch locally), and clean up the worktree and branch. Each line and each step copies with one click, and the
  delivered card and the task header both open it.
- **Readable branch names.** Branches are named from the task type and the meaningful words of its name, such as
  `feat/berth-status-page` or `fix/123-login-timeout`, with filler like "please make" dropped, accents folded and a
  `-2` suffix only on a collision. The New task wizard shows the suggestion and lets you edit it. Settings → Git can
  switch back to `<branch prefix>/…`.
- **Repositories page.** A new rail entry (`#/repos`) lists every git repository in `RELAY_REPOS` with its branch,
  ahead/behind from the last fetch, uncommitted changes, last commit and its Relay tasks and worktrees, with Fetch,
  fast-forward-only Pull, New task here, Open on GitHub, Copy path and Clone repository. A Worktrees tab shows every
  worktree Relay created with its task, disk size, last commit and uncommitted / pushed / merged / missing badges, and
  removes worktrees, deletes merged branches, or cleans up delivered-and-merged worktrees in bulk after a preview. It
  refuses to touch a worktree whose task is still running. A Branch graph tab draws the default branch with task
  branches forking off and merged ones joining back.
- **History tab.** Lists the commits on a task's branch since it started, newest first, with author, relative and
  absolute time, and per-file +/− counts, under a summary of commits, files touched, lines and time span. Uncommitted
  work in the worktree shows as its own entry at the top. Expanding a commit shows its full message and files, and
  clicking a file opens its diff. After the worktree is removed the history is read from the branch.
- **Where the time went.** The Overview tab charts wall time for planning, each work package, each review round and
  delivery, coloured by the agent that did the work. Hovering a bar shows its tool calls, tokens and cost. Tokens and
  cost are now recorded per agent turn, so tasks run before this version show time only.
- **Live pull request status.** The task header shows a pill with the pull request's state (draft, open, merged or
  closed) and its checks, and the Try it tab opens with a pull request card: review decision, checks, size and
  mergeability, with a Refresh button. Once the pull request is merged, the Accept step says so instead of offering
  merge commands. Status comes from `gh pr view` and is cached for a minute.
- **Follow up.** Delivered tasks have a Follow up button, in the header and on the Delivered card, that opens the New
  task wizard on the same repository, team and branch, with the previous summary quoted. A follow-up may reuse a
  branch whose task is done, failed or stopped, and links back to the task it builds on.

- **Ubuntu support.** `run.sh` sets up a virtual environment and starts Relay, and
  `deploy/relay.service` runs it as a systemd user service with your own logins.
- **Docker mode.** A `Dockerfile` and `docker-compose.yml` that install the agent
  CLIs in the image and mount the host's logins, settings, Git identity and
  repositories. Repositories and data mount at identical paths so task worktrees
  stay valid from host and container alike. The container publishes its port on
  the host's loopback only and reports on startup which host logins it can see.
- `RELAY_DATA_DIR` keeps all state outside the app folder, `RELAY_HOST` sets the
  listen address, `RELAY_BROWSE_ROOT` sets where the folder browser starts, and
  `RELAY_CFG_<setting>` forces any setting without writing it to `config.json`.

- **Notification settings.** Settings → Notifications shows whether the browser allows desktop notifications with
  an Allow button (Relay no longer asks by itself a few seconds after loading), per-event switches for delivered,
  failed or stopped, needs input or approval, and pull request opened, an optional chime generated in the browser,
  and a Send test notification button. Every server notification now carries a `kind`, and stopping a task notifies
  too. Clicking a desktop notification focuses Relay and opens the task.
- **Attention in the browser tab.** The tab title shows how many tasks need you, such as `(2) Relay`, and the icon gets
  a red dot. It counts questions, approvals, paused or interrupted tasks and failures you have not opened yet.
- **Keyboard shortcuts.** `?` lists every shortcut. New: `J`/`K` open the next or previous task in the list,
  `G` then `D`/`T`/`A`/`H`/`S`/`R` go to the dashboard, tasks, agents, GitHub, settings or repositories, `T` opens
  Try it and `.` focuses the guidance box. Shortcuts never fire while typing or while a dialog is open; tooltips and
  palette items show their keys, and the palette has a Keyboard shortcuts entry.
- **Dashboard insights.** Tasks finished per day over 14 days stacked by outcome, spend per day by agent, success
  rate and median time-to-deliver trends against the previous week, and the busiest repositories. Charts have hover
  and keyboard tooltips and a table view. A dashboard with no tasks shows a setup checklist instead of empty cards.
- **Saved prompts.** Keep reusable requests in Settings → Saved prompts (name, text, optional task type) and pick one
  in step 2 of the New task wizard; the first `<placeholder>` is selected so you can type over it. Two examples ship:
  redesigning a page to `rules/DESIGN.md` and fixing a bug with a regression test.

### Changed

- **The plan is a context packet.** The supervisor's plan envelope separates `requirements` (the user's request,
  nothing added), `acceptance` (derived from it) and `optional` improvements that never block done, and records
  `known_files`, `findings` with their file, `constraints` and `unknowns`. The worker and reviewer receive it, and the
  worker verifies what its package depends on instead of rediscovering the repository. Rule-file checklists may no
  longer be copied into requirements or acceptance.
- **Work is split by concern.** Supervisors split by independently verifiable concern when that creates a clear
  dependency boundary (typically data and behaviour, then UI, then tests), keep tiny changes together, and keep
  instructions short. Verification is no longer part of a work package: workers run focused checks and the
  orchestrator runs the full suite.
- **Blocked checks instead of workarounds.** Workers may not build improvised environments (dependency trees outside
  the repository, symlinked installs, registry mirrors, stub packages) to get a check running. They report
  `blocked_checks` and `blockers`; the task page shows them, the supervisor and reviewer see them, and the user is
  asked only when an entry has `action_required: true`.
- **Independent review by default.** New installs use the Codex → Claude + independent Codex review preset. The
  reviewer compares the result with the requirements and acceptance criteria and requests corrections only for
  concrete defects; optional items and alternative designs never block.
- The dashboard's duration card shows the median time to deliver, with the mean underneath, so one very long task no
  longer skews it.
- **Git noise hidden by default.** The orchestrator's git and GitHub commands no longer appear in the team
  conversation unless you turn on the new Git toggle; the timeline still records them.

- **Design-first rules.** `DESIGN.md` is now the house design system for every stack, with guidance on translating
  its tokens and components into Vue, plain CSS or any other frontend. It adds a design process (compose, build,
  look at it, improve) and explicit rules for redesign requests: change the composition and the components involved,
  keep behaviour and data contracts. `CORE.md` no longer shrinks a requested redesign to the smallest diff.
- **Proportionate verification.** `FRONTEND.md` is rewritten around structure and quality floors instead of
  exhaustive viewport, state and accessibility checklists. Supervisors size work packages by cohesion, may not order
  evidence-gathering packages (harnesses, fixture recording, screenshot matrices) unless the task asks, and revise
  only for concrete defects. Planners keep acceptance criteria to the outcomes that matter.

### Fixed

- **New task wizard on phones.** The dialog ran off the right edge at 420px; it now fits, the step list collapses
  to numbers, and the clone fields wrap onto two rows. The folder browser's GIT badge no longer stretches across the
  path row, and "Use this folder" uses the normal font. The wizard's Cancel button and in-dialog links now close it.
- **Gemini showed READY without a login.** `gemini --version` works signed out, and Docker mounts an empty
  `~/.gemini`. Gemini now reports "not signed in" unless it has a Google login, an API key or a Vertex AI setup.
- **Adding a watched GitHub repository failed with a bare HTTP 400.** The form now checks owner/repository before
  sending, accepts pasted GitHub URLs (including links to issues or branches), and shows the server's reason next
  to the field at fault, for example a local clone path that does not exist or a repository already watched.
- **Inspector tabs stuck on "Loading…".** When a request failed, for example because the task was deleted, the
  Changes, Try it, Checks, Review, Repository and Logs tabs never left their loading message. They now explain what
  failed and offer Try again.
- On screens narrower than 980px the task list no longer opens over the page on every load, the inspector toggle now
  reveals the inspector (it could not be reached before), and the notifications panel fits the screen.
- Pressing `N` while a dialog was open opened a second New task wizard.
- **Delivered tasks showed 0 files changed.** Change counts, the Changes tab and the reviewer's diff compared
  against the last commit, so once Relay committed the result they came back empty. They now compare against the
  commit the task started from; older delivered tasks correct themselves when opened.
- **False verification failures on JavaScript repositories.** `python -m pytest` was added for any repository with a
  `tests/` folder and its "no tests collected" exit code 5 failed the run. It is now added only when the repository
  has Python tests, and exit 5 is reported as skipped.
- **Rule files pulled into unrelated tasks.** Contextual rule keywords matched inside other words, so `ui` in "build"
  sent the frontend and design rules with nearly every task. Keywords now match at word starts.

- **Stop and Delete left agent processes running on Linux.** Only the direct
  child was signalled, so the shells, test runners and MCP servers an agent
  started kept running and editing files. Each agent now runs in its own process
  group and the whole group is stopped. Verified on Ubuntu: five nested
  processes before Stop, none after.
- **Codex "cheaper subagent" never worked** and printed eight "malformed agent
  role" errors per turn. Codex's `agents.<role>` table defines whole custom roles,
  not the model its built-in helpers use, and there is no such setting. The
  override is removed and the option is shown as unsupported for Codex.
- Non-fatal Codex configuration warnings no longer render as crash-style error
  cards; they appear once in the turn's notice.
- The Gradle check uses `./gradlew` on Linux instead of `gradlew.bat`.
- **You can see what agents are editing.** Edit and write calls show the file
  relative to the project with a `+added −removed` count, and small diffs open
  inline. Edits made through the shell (`sed -i`, `perl -pi`, `Set-Content`,
  redirects) are recognised and labelled with their file. The status line names
  the tool that is running instead of reporting "quiet" while a command works,
  and the conversation header counts the files edited so far.

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
- **Cheaper subagents.** Claude Code's helper agents run on a small model through
  `CLAUDE_CODE_SUBAGENT_MODEL`. (The Codex override shipped here did not work;
  see Unreleased.)
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
