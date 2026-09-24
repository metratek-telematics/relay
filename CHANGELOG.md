# Changelog

All notable changes to Relay are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Tasks no longer start from stale code** (`orchestrator/gitops.py`, setting `branch_from_upstream`).
  A new task branch is created after a fetch, from the remote branch the checkout follows, whenever that checkout
  is merely behind it — a server checkout nobody pulls used to send every task off from months-old files, so the
  work did not fit current code and the pull request could not be merged. A checkout that has diverged, carries
  uncommitted changes or follows no remote branch still starts from what is checked out, and the timeline says
  which of the two happened.
- **A dialog that asks for a name keeps the name** (`web/js/ui.js`). The prompt dialog closed before it resolved,
  and closing it answers "cancelled", so OK returned nothing and whatever was typed was dropped.

### Added

- **Several accounts per agent CLI, and a page that says what each one costs and when it runs out**
  (`orchestrator/accounts.py`, `orchestrator/agent_info.py`, `orchestrator/autopilot.py`, `orchestrator/runner.py`,
  `web/js/views/accounts.js`, `web_app.py`). A CLI is signed in per configuration folder, so a second subscription
  needed a second folder: Claude, Codex, Gemini and every pack agent can now hold more than one account, each with
  a name you choose, its own sign-in folder under the data directory, its own limit reading and an enabled/paused
  flag. One account stays the default, and an installation with the single account it has always had behaves
  exactly as before — same folder, same readings, same answers. A turn prefers the account its role's session
  already runs on, then the default, then the next enabled account with capacity, and only then falls back to what
  Relay did before: switch the role to a fallback agent, or wait for the window to reset. Two turns never share a
  configuration folder while another account is free, and the task records which account took each turn, beside
  the model and the effort. Signing a second account in stays manual — these CLIs need an interactive login — so
  the Agents page prints the exact command, with the account's folder in it, and notices by itself when the
  sign-in lands there. The new Accounts, plans and limits card gives one state per account: signed in or not, the
  plan, how much of each window is used with its reset time, and the reason it cannot run when it cannot. It says
  in words what happens when a limit is hit (move to the next account, switch to a fallback, wait), keeps a plan
  subscription visually apart from pay-as-you-go spend, describes OpenRouter credit and free-model rotation in the
  same language, marks which numbers came from the CLI and which Relay measured, and says "not known" wherever it
  has not read something instead of showing a blank or a zero. Closes #55 and #56.


- **Personal settings, on top of the organisation's** (`orchestrator/personal.py`, `orchestrator/org/identity.py`,
  `orchestrator/manager.py`, `web/js/views/settings.js`). Each person can keep their own default team and workflow
  — agent, model and effort per role, team mode, turn budget, review rounds, verification, the approval gate,
  agent questions, the design step — and new tasks start from theirs. Everything shared stays shared and
  owner/admin only: provider keys and OpenRouter, spend caps, connectors, repositories, the redeploy command,
  sign-in and role mapping; the role matrix refuses those to anyone below admin, and the personal endpoint
  refuses to store them at all. The precedence — what the task itself says, then the person's own setting, then
  the organisation default — is resolved once in the backend and nowhere else, so the browser is handed values
  that are already in effect. Settings → Team and workflow says which values are yours and which are the
  organisation's, clears an override back to the organisation default in one click, and lets an admin switch to
  editing the organisation default itself. A task records who created it and which defaults it started from, so
  it does not change meaning when somebody else looks at it. An installation where nobody has personal settings
  behaves exactly as before.

### Fixed

- **Tasks no longer start from stale code** (`orchestrator/gitops.py`, setting `branch_from_upstream`).
  A new task branch is created after a fetch, from the remote branch the checkout follows, whenever that checkout
  is merely behind it — a server checkout nobody pulls used to send every task off from months-old files, so the
  work did not fit current code and the pull request could not be merged. A checkout that has diverged, carries
  uncommitted changes or follows no remote branch still starts from what is checked out, and the timeline says
  which of the two happened.

### Added

- **The model and effort a task runs on are shown, and come from the orchestrator** (`orchestrator/pipeline.py`
  `publish_role_plan`, `orchestrator/runner.py`, `web/js/state.js`). Relay resolves each role's model and effort
  once, with the same code that launches the agent, and records what every finished turn actually used; the task
  page's Team card, the team pills, the theatre and the dashboard team hover show that rather than re-deriving
  the settings in the browser. A role with no model states the CLI's own default, and a CLI with no effort
  setting says so instead of showing a blank.
- **Knowledge docs as a first-class source** (`orchestrator/knowledge.py`, `web_knowledge.py`, `web/js/views/kdocs.js`).
  Every kickoff prompt (supervisor, worker, solo, reviewer, fresh-session handoffs) carries a short Knowledge section: the
  paths under `DATA_DIR/knowledge` that apply to the task (its repositories' docs, related repositories from the approved
  system map, and the platform docs a repository doc names in `platform_docs`) with one line each, for agents to read
  before planning. Knowledge → Docs lists, searches and renders them; owners and admins can edit a doc (audited, with a
  stale-edit check), and every section they change is recorded in `human_sections` so no refresh rewrites it.
- **Knowledge docs stay current.** Repository docs record the commit they were written from (`source_commit`). A daily
  job (and Refresh now) asks GitHub, read-only, how far the default branch moved; a doc behind by N commits or by
  changes to key files (manifests, Dockerfiles, compose, CI, migrations, config) gets ONE cheap agent turn on a
  shallow clone that rewrites only the affected sections of the doc and playbook, then the repository is rescanned in
  the system map. A delivery that touched key files marks the repository's doc as possibly stale.

- **Redeploy after merge** (`orchestrator/redeploy.py`, `orchestrator/manager.py`, `web_app.py`,
  `web/js/views/{settings,newtask,task}.js`). *Settings → Git & GitHub → Redeploy after merge* adds
  `redeploy_enabled` (off by default), `redeploy_command`, `redeploy_working_dir`, `redeploy_trigger`,
  `redeploy_timeout_minutes` and `redeploy_poll_seconds`; `redeploy_default_on` is a separate setting for what
  the per-task switch in the workflow editor starts with, so changing the default for new tasks never flips the
  master switch. A watcher checks each delivered opted-in task's pull request with `gh pr view` and runs the
  command once, one deployment at a time, in its own process group with the configured timeout; the output goes
  to `runtime/<task>/redeploy.log` and the status, exit code, trigger and timestamps onto the task, with a
  timeline event and a notification. The task page shows the record beside the pull request and
  `POST /api/tasks/<tid>/redeploy` (the *Redeploy now* button) runs it regardless of merge state, or returns 400
  when no command is configured. The command is owner configuration only: never task text, agent output or a
  request body.
- **Report an issue** (`web/js/views/report.js`, `POST /api/github/report-issue`). A flag button in the left rail and a
  command-palette entry open a dialog that files a bug report or a feature request on the Relay repository without
  leaving the workspace. The fields follow the repository's own issue templates, the environment block (Relay version,
  agent CLI versions) is read from the running session and stays editable, and screenshots can be pasted, dropped or
  browsed for. Relay calls `gh issue create`, retrying without the label if the repository has never created it, and
  links straight to the new issue. GitHub accepts issue images only through its web interface, so pasted screenshots are
  held briefly under `DATA_DIR/issue-attachments/` and offered for one-click copying instead. The target repository is
  the new `github_issue_repo` setting under Settings → Git and GitHub.
- **The model and effort a task runs on are shown, and come from the orchestrator** (`orchestrator/pipeline.py`
  `publish_role_plan`, `orchestrator/runner.py`, `web/js/state.js`). Relay resolves each role's model and effort
  once, with the same code that launches the agent, and records what every finished turn actually used; the task
  page's Team card, the team pills, the theatre and the dashboard team hover show that rather than re-deriving
  the settings in the browser. A role with no model states the CLI's own default, and a CLI with no effort
  setting says so instead of showing a blank.
- **Knowledge docs as a first-class source** (`orchestrator/knowledge.py`, `web_knowledge.py`, `web/js/views/kdocs.js`).
  Every kickoff prompt (supervisor, worker, solo, reviewer, fresh-session handoffs) carries a short Knowledge section: the
  paths under `DATA_DIR/knowledge` that apply to the task (its repositories' docs, related repositories from the approved
  system map, and the platform docs a repository doc names in `platform_docs`) with one line each, for agents to read
  before planning. Knowledge → Docs lists, searches and renders them; owners and admins can edit a doc (audited, with a
  stale-edit check), and every section they change is recorded in `human_sections` so no refresh rewrites it.
- **Knowledge docs stay current.** Repository docs record the commit they were written from (`source_commit`). A daily
  job (and Refresh now) asks GitHub, read-only, how far the default branch moved; a doc behind by N commits or by
  changes to key files (manifests, Dockerfiles, compose, CI, migrations, config) gets ONE cheap agent turn on a
  shallow clone that rewrites only the affected sections of the doc and playbook, then the repository is rescanned in
  the system map. A delivery that touched key files marks the repository's doc as possibly stale.

- **Speed: triage and a solo fast path** (`orchestrator/triage.py`, `orchestrator/solo.py`, docs/SPEED_AUDIT.md).
  Team mode per task (Auto, Solo, Team). Auto triages every task with a heuristic and, for borderline requests, a cheap
  one-shot rating that runs while dependencies install. Small and moderate single-repository work runs one agent that
  plans, builds and proves the change; Relay still verifies, gates acceptance on evidence and runs a light independent
  check only when the change is risky. The design step, design research, exploration and the final review scale with
  the task and record why they ran. An express lane starts solo tasks beside a long run instead of behind it.
- **Faster verification and setup** (`orchestrator/verifyfast.py`): results reused for an unchanged tree, builds
  skipped when only tests or docs changed and deferred per package, lint and tests side by side, baseline results
  shared across tasks, generated files restored automatically, `node_modules` reused across tasks by hard links while
  the lockfile is unchanged.
- **Where the time went** on the task page: active and queued time, kinds of work, each phase split by kind, and why
  each heavy phase ran (`orchestrator/timing.py`).

- **relay-perf: performance measured like DevTools and Lighthouse, not judged from screenshots** (`tools/perf.cjs`,
  `tools/perf_analyze.cjs`, `orchestrator/perfcheck.py`, `web/js/views/perf.js`). Agents drive a page with a scripted
  scenario (relay-browse steps plus drag, wheel and named phases) at normal and 4x/6x-throttled CPU while a Chrome trace
  and the page's CPU profile are recorded, and get FPS avg/p5 and late frames (page and iframes), long tasks and TBT with
  the functions inside them, the top 15 functions by self and total time with source-mapped file:line, scripting /
  rendering / painting / GC time, forced layouts, heap after GC, DOM nodes and listeners, busy time of every thread,
  request bursts, polling and websocket rates, and Lighthouse (pinned 13.5.0 in the image). `relay-perf compare` and
  `relay-perf assert` turn two runs into a verdict and check numeric targets; `--har` replays the same live data in every
  run and `--serve` starts the app. Performance tasks on web repositories must plan a measurement: Relay records the
  baseline on the starting commit, re-measures at verification, and a missing measurement, a missed target or a
  regression fails verification. The task page shows a performance card with before/after, targets and links to the
  reports and the DevTools trace. rules/PERFORMANCE_RELIABILITY.md: profile first, typical map/browser causes and fixes.
- **OpenRouter as a provider** (`orchestrator/openrouter.py`, `openrouter_proxy.py`, `openrouter_launch.py`,
  `web/js/views/openrouter.js`, docs/OPENROUTER.md). Any role can run its agent on OpenRouter ("Runs on · OpenRouter"
  in the team): Claude Code, Codex, OpenCode, Kilo, Cline, Goose, Aider, Crush, Qwen Code, Continue and GitHub Copilot,
  each with a per-run recipe that never touches the owner's own CLI login. Agents reach OpenRouter through a local
  gateway that keeps the key away from them, applies routing and privacy preferences, paces free models, waits out rate
  limits, rotates models and records the real cost of every request. Settings → Model providers (admins): key with
  test, routing, fallbacks, attribution, monthly caps for all projects and per project, low-credit alert. A model browser
  with filters, a computed "recommended for coding" shortlist and **Auto · best free model** (ranked from the catalog
  and Relay's own history, rotated with cooldowns). Real cost per turn in the conversation and the task's Sessions tab,
  account and spend in Models & usage, autopilot capacity checks (credits, daily free requests, caps), readable errors
  and autopsy categories, provider-aware team keys for learning. `tools/mock_openrouter.py` and
  `tests/test_openrouter.py` for development.
- **Telegram assistant** (`orchestrator/org/telegram.py`, `concierge.py`, `voice.py`). Long polling by default (nothing
  exposed; offset persisted, backoff, single-poller lock), webhook mode still available. Notifications carry the task, the
  actual question, design summary, findings or error, with buttons and a link; a plain reply answers that pending question
  or becomes guidance (resuming ended tasks). Commands for status, needs-you, tasks, logs, steering, approvals, new tasks
  (confirmed with repository, team and risk), autopilot pause/resume, digest, merge and follow. Free text goes to a
  tool-less concierge turn that answers from live context and only suggests actions as confirm buttons. Actions run as
  the linked person with the web role matrix and are audited via telegram. Integrations shows mode, live status, setup
  steps and a test send.
- **Mission Control** (`web/js/views/mission.js`, `/api/mission`). The home screen answers what the agent teams are doing,
  what needs you and how it is going: live agent cards (repositories, phase, the tool call running now, elapsed, cost,
  work-package progress), Needs you with questions, approvals and design approvals answered in place, the queue lane with
  ETAs, dependencies and drag reorder, deliveries with scorecards, a 14-day success trend, limits and spend. The digest
  is the same screen over 24 hours, 3 days or 7 days.
- **Work board** (`views/work.js`): tasks in every state, GitHub issues and the queue in six columns (Ideas, Queued,
  Running, Needs you, Review, Done), with filters, drag to queue, reorder or unqueue, and bulk actions.
- **Task page layout**: a phase outline (plan, design, build with work packages, verify, review, deliver), views for the
  conversation, the **live theatre** (relay track with animated handoffs, editor pane with the diff being written,
  terminal tail, step feed), the design, **Changes** across every repository (file navigator in merge order beside the
  diff, commits, Try it, file editor), checks and logs, and a details rail whose sections fold.
- **Review cockpit** (`#/review/<id>`, `views/review.js`): verdict strip, design and merge order, acceptance evidence,
  every diff, verification and review findings, follow-ups; **Approve and merge** merges each pull request in order with
  the GitHub CLI and stops at the first failure (`POST /api/tasks/<id>/merge`, `orchestrator/mission.py`), **Request
  changes** opens a follow-up task with your comments and the chosen findings.
- **Shareable status** (`#/status/<id>`): a read-only live page for stakeholders, behind the same sign-in.
- **Command palette**: "create task: …" detects the repositories named in the text (open the dialog or queue at once),
  `#12` jumps to a task, and actions for the task in front of you.
- **Knowledge** gathers the system map, repositories, connectors, lessons, learning, worktrees, branch graph and watched
  GitHub repositories. Settings are grouped and searchable.
- Design system: `web/tokens.css` (house palette in light and dark, state colours reserved for status), self-hosted
  Hanken Grotesk and JetBrains Mono (`web/fonts`, SIL Open Font License), motion that stops under reduced motion.

### Fixed

- An owner's "waive D1" in an answer or guidance now waives the criterion, and "just make a PR" delivers; before, the
  judge kept refusing done. Exploration no longer triggers on performance work that keeps the look. OpenRouter daily
  quota errors fail fast instead of six back-offs. The scorecard refresh no longer re-appends the same autopsy event
  every 30 minutes.

### Changed

- Navigation is five labelled places (Mission Control, Work, Knowledge, Agents, Settings); the task sidebar and the
  autopilot bar are gone, and the old addresses redirect (`#/inbox`, `#/digest`, `#/tasks`, `#/issues`, `#/github`,
  `#/repos/*`, `#/lessons`, `#/learning`, `#/settings/connectors`, old task tabs). The dashboard and Issues pages are
  removed; see `docs/UX_AUDIT.md` for why.

- **Toolbox: tools for every agent.** Settings → Tools manages MCP servers (a local command or a remote URL with
  secret headers) and command-line tools, with a vetted catalog installed on one click into Relay's tools folder
  (filesystem, fetch, git, memory, sequential thinking, time, Playwright, Context7, GitHub remote, PostgreSQL, ast-grep)
  and a Test button that lists a server's tools. Tools are enabled for all tasks, per repository (Repositories →
  Environment) or per task (New task → Advanced). Relay writes each turn's servers into the CLI's own format in the
  task's run folder (Claude `--mcp-config`, Codex `-c mcp_servers.*`, Gemini system settings file, Qwen and Amp
  `--mcp-config`, Copilot `--additional-mcp-config`, OpenCode and Kilo config content, Goose extensions); secrets
  reach the CLI only through its environment and are masked in logs, and personal CLI configuration is never
  touched. CLIs without MCP are told the command-line equivalent. Agents ask for tools with `relay-tools request`
  or a `tool_request` envelope field; a policy approves catalog tools, anything in development projects, or asks
  the owner in Needs you. Approved tools are announced on the agent's next turn; every call is counted per tool.
- **Token efficiency.** Every turn records its prompt by section, cache hits and context size; Settings → Token
  efficiency shows where tokens go by section, role, agent, day and task. Claude agents run without the owner's
  personal plugins, skills and connectors; kickoff prompts put the stable rules first; live sessions are not sent
  what they already have; failing checks send the lines that explain the failure; the reviewer gets a targeted
  diff; long sessions are compacted into a fresh one from a Relay handoff; Codex runs with low verbosity. See
  docs/TOKEN_EFFICIENCY.md for the measured effect (−16% cost on a small A/B task, −64% to −95% on repeated
  supervisor and re-review prompts).
- **Learning engine** (`orchestrator/learning_engine.py`, Learning page, `/api/learning`). Every finished run is recorded
  in an append-only outcome dataset (`DATA_DIR/learning/outcomes.jsonl`): repositories, task type, request size, team
  (agent, model, effort per role), prompt and rule version hashes, lessons injected, cost, judge events, verification
  first pass, end-to-end results, human interventions, score, post-merge signal and the pre-flight forecast. On top of it:
  - **Team recommendation** (`recommend.py`): a similarity-weighted Bayesian average per team with best, balanced and
    cheapest-good-enough modes, explained in one line in the New task wizard. Autopilot can pick the team for tasks
    nobody chose one for (`learning.auto_pick_team`, 10% exploration, never for urgent or critical tasks); a team can be
    pinned per repository.
  - **Pre-flight risk** (`risk.py`): low, medium or high with the factors behind it, mitigations (add a reviewer, use the
    recommended team, split, attach a stack, apply the environment fix) and clarifying questions for thin or vague
    requests. The forecast is stored on the task and compared with the outcome (per-level calibration, Brier score).
  - **Autopsies and one-click fixes** (`autopsy.py`): failed, low-scoring and "delivered blind" runs (blocked checks or
    unproven required criteria) get a root cause (environment, requirements, flaky checks, protocol, infrastructure,
    agent capability) and concrete proposals: system packages, a lesson, a rule line, an optional check, a setting, or a
    pinned team. Applied proposals show clean-run rates before and after.
  - **Lessons that prove themselves** (`lesson_effect.py`): categories, effect of each lesson (runs with it vs without:
    score, first pass, revisions) with retirement flags, relevance-based selection instead of every lesson, and support
    counting across retrospectives with optional auto-approval (`learning.auto_approve_lessons`, off by default).
  - **Playbooks** (`playbooks.py`): per repository, seeded from the system map, environment, lessons and outcomes,
    refreshed by a cheap agent turn, editable (edited sections are kept), and added to the supervisor's planning prompt.
  - Scorecards (version 2) detect a merged pull request reverted on its base branch within 14 days (−40, not a success).

- **The supervisor judges against an acceptance contract.** The plan carries 2 to 6 checkable criteria (id, criterion,
  how to verify, required). They show on the task Overview as a checklist with status and evidence, can be edited
  there (or with `PATCH /api/tasks/<id>/acceptance`), and gate delivery: `done` is refused until every required
  criterion is met with concrete evidence, and a check Relay ran that fails outweighs any claim. After two refused
  attempts the human decides.
- **Severity discipline.** Revisions and review findings are `blocking`, `should_fix` or `nit`. Only blocking findings
  cost another round; the rest are collected as follow-ups in the pull request, the report and the task page. A
  review FAIL without a blocking finding counts as a PASS.
- **Loop guards that ask instead of failing.** Every revision names the criteria or findings it addresses. A blocking
  finding that comes back after two fix attempts, a revision that changes nothing, a used-up work-package budget,
  used-up review rounds and verification that still fails after triage all ask the human with options (accept as a
  follow-up, give guidance, more budget, stop). "go ahead" grants more budget. Unanswered questions take the safe
  automatic choice after `judge_escalation_timeout_minutes`, and resuming a task stopped at its budget extends it.
- **Scorecards, retrospectives and lessons.** Every finished task gets a 0 to 100 scorecard: outcome and failure
  cause, duration, turns, revisions, review rounds, verification first pass and final, cost, team, and what humans
  had to do. Pull requests are re-checked every 30 minutes for two weeks, so a merge, a rejection or human commits
  after delivery change the score (the formula is in `orchestrator/scorecard.py`). A short, cheap retrospective turn
  then proposes at most three one-sentence lessons. Nothing reaches a prompt until someone approves it on the new
  Lessons page; approved lessons for a repository (and global ones) are added to later supervisor and worker kickoff
  prompts. The dashboard has a Success section (weekly success rate, average score, by repository, by team, why tasks
  fail) and each task shows its scorecard. Settings → Workflow → Learning turns the retrospective off or picks its
  agent and model.

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

- **Gemini shown as not signed in although it works.** Gemini CLI 0.60 keeps an API key or Google login in its own
  encrypted `gemini-credentials.json`, which readiness detection did not recognise.
- **Gemini logins lost on every container recreate.** The CLI derives that file's encryption key from the hostname,
  and a container gets a random one. The compose files now give the Relay container a fixed hostname.
- **A command stuck "running" forever after a restart.** A command or tool call interrupted by a Relay restart kept its
  spinner and a growing timer. At startup they are now marked "interrupted by a restart".
- **Work committed to the wrong branch.** If the worktree was switched to another branch (for example in VS Code),
  delivery committed there and pushed an empty task branch, so the pull request failed with "No commits". Relay now
  refuses to commit off the task branch and says how to switch back.
- **Generated files in the commit.** A verification build could rewrite tracked files (version stamps) after the team
  had cleaned them, and they were committed. Files changed only by the checks are now restored.
- **Endless done → verification loop.** When a verification command failed for a reason unrelated to the task, the
  supervisor declared done again and Relay re-ran the full verification, forever. A failing command is now run once on
  the task's starting commit in a temporary worktree; if it fails there too it is reported as a pre-existing failure
  and does not block. Real failures get one supervisor triage round; after that the task stops for the user.
- **Retry from scratch mixed runs together.** Elapsed time counted from the first run, the work chart and signals
  combined runs, and the conversation gave no hint where the new run began. A fresh retry now starts a new run: timing,
  work chart, blocked checks, design gate and environment reset, and a divider marks the run in the conversation.
- **"Backend offline · keep run.bat open" on a server install.** An expired login-proxy session looked like a dead
  backend. The page now says Signed out with a reload link, and a server install no longer mentions run.bat.
- **Preview failed for HTTPS dev servers.** code-server's port proxy speaks plain HTTP, so projects whose dev server
  uses HTTPS (Vite with basic-ssl) returned an error. The IDE image now has `relay-preview`, which starts the dev
  server with the right base path and bridges the proxy to it over HTTPS or HTTP, including the live-reload
  websocket; Try it's preview step runs it.
- **Duplicate axis labels.** The Finished per day chart labelled two gridlines "1" when at most one task finished a day.
- **Stale pages after a deploy.** An open browser kept running the previous JavaScript after Relay restarted, so fixed
  buttons still showed old errors. The page now notices the server restart and offers a Reload.
- **Open folder failed on a server install.** In Docker, the task menu and the Overview tab's Worktree, VS Code and
  Run folder buttons tried to open a desktop window and returned an error. With browser VS Code configured they now open there (run folders are mounted
  read-only into it), are disabled until the task has a worktree, and Copy worktree path is added.
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
