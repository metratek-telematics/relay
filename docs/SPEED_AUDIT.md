# Speed audit: where a Relay task's time goes

The owner's feedback: *"A task that takes one agent 4 minutes, they make it in like 40 minutes. On others it fails
miserably."* This audit measures every real run on record, finds where the time goes, classifies the failures, and
lists what changed. All numbers come from the run records (`state/tasks.json` timeline events, the per-turn metrics
log, `runtime/<task>/messages.jsonl` command durations), computed with `orchestrator/timing.py`, the same code that
now draws the time breakdown on the task page.

Data set: the 13 runs recorded between 2026-09-17 and 2026-09-21 (12 on the navigation web app, 1 on the berth
optimizer), team *Claude supervises Codex* (one run *Codex supervises Claude*), some with an independent reviewer.

## 1. Every run, by phase (minutes:seconds)

`wall` is from the moment the owner submitted the task to delivery; `queue` is time waiting for a free slot;
`active` is the run itself. Scores are the scorecard (PR merged = high, closed unmerged = low).

| task | rated | wall | queue | active | setup | plan | design | explore | build | verify | review | deliver | score |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| optimizer: debug, find holes | – | 9:55 | 0:00 | 9:55 | 0:14 | 1:39 |  |  | 7:58 |  |  | 0:04 | 95 |
| vessel symbols v1 (redesign) | moderate | 21:39 | 0:00 | 21:39 | 0:50 | 5:20 | 4:40 |  | 8:04 | 1:46 | 0:53 | 0:05 | 26 |
| vessel symbols v2 (thin line) | simple | 12:40 | 0:19 | 12:21 | 0:28 | 1:36 |  |  | 7:20 | 1:46 | 1:08 | 0:02 | 39 |
| vessel symbols v3 (new idea) | moderate | 31:09 | 0:00 | 31:09 | 0:21 | 3:46 | 9:32 |  | 14:15 | 1:46 | 1:27 | 0:02 | 18 |
| vessel symbols v4 (layered) | moderate | 47:11 | 0:01 | 47:10 | 0:30 | 2:19 | 5:29 |  | 36:29 | 1:46 | 0:34 | 0:03 | 14 |
| sea route planner integration | moderate | 58:18 | 0:01 | 58:17 | 0:29 | 1:31 | 4:21 |  | 49:14 | 1:47 | 0:50 | 0:05 | 69 |
| map tiles load slowly | moderate | 81:14 | 0:01 | 81:13 | 0:41 | 1:20 | 12:45 |  | 64:34 | 1:47 |  | 0:06 | 71 |
| map stutter (1st run, stopped) | complex | 80:38 | 0:02 | 80:36 | 0:24 | 5:34 | 54:09 | 20:29 |  |  |  |  | 7 |
| map stutter (2nd run) | moderate | 161:48 | 0:00 | 161:48 | 0:25 | 6:23 | 6:46 | 13:25 | 130:17 | 1:47 | 2:38 | 0:06 | 6 |
| playback UTC toggle + picker | moderate | 68:27 | 0:00 | 68:27 | 0:23 | 0:55 | 6:34 |  | 58:43 | 1:46 |  | 0:06 | 71 |
| metrics date picker | simple | 78:22 | **66:21** | 12:01 | 0:27 | 0:37 |  |  | 9:05 | 1:46 |  | 0:05 | 82 |
| fender average line | simple | 86:47 | **75:17** | 11:30 | 0:28 | 0:28 |  |  | 8:42 | 1:47 |  | 0:05 | 95 |
| playback wind widget | moderate | 91:01 | **64:52** | 26:09 | 0:26 | 1:36 | 4:31 |  | 17:46 | 1:46 |  | 0:04 | 88 |

## 2. What filled the time (all 13 runs, 830 minutes of wall time)

| kind of work | minutes | share |
|---|---:|---:|
| Worker turns (implementation) | 384 | 46% |
| **Waiting in the queue** | **207** | **25%** |
| Supervisor turns | 103 | 12% |
| Reviewer turns (design review, focus group, final review) | 79 | 10% |
| Waiting for the owner to answer | 28 | 3% |
| Relay's own checks (lint, tests, build) | 19 | 2% |
| Dependency install (`npm ci`) | 6 | 1% |
| Git, push, pull request | 1 | 0.1% |
| Relay overhead (orchestration, idle gaps between turns) | 1 | 0.2% |

**Relay itself is not idle.** Turns follow each other within a second; the pacing, polling and scheduler loops add
nothing measurable (0.2%). The time goes to *what* the team does, and to the queue.

Worker time split by what the package was for (349 minutes of worker turns after planning):

| worker turn | minutes | share |
|---|---:|---:|
| first package | 104 | 30% |
| further packages (the plan split into several) | 143 | 41% |
| **revision requests** | **102** | **29%** |

## 3. Where the 10x goes, ranked by measured impact

1. **The queue (207 min, 25% of all wall time).** `max_parallel` is 1. The owner submitted four tasks between
   13:49 and 14:16; the three small ones then waited 65 to 75 minutes behind a 68-minute run. The fender average line
   was 11.5 minutes of work and 87 minutes from request to pull request: *this is the "4 minutes becomes 40" the owner
   sees.* The simple tasks themselves ran at 1.5 to 2 times a single agent's time.
2. **Revision rounds (102 min, 29% of worker time).** 21 revision requests. About half were real defects (a thrown
   `moveend`, a retry back-off bypass, a stale-response guard, a reused Response body). The other half were process:
   screenshots "of a mock-up, not the real page", missing before/after measurements, "restore the generated build
   files" (three separate revisions for `public/version.json` and `public/map-preconnects.js`, which Relay's own
   verification already restores), a test-count line in a doc.
3. **Too many packages (143 min).** Moderate tasks were split into 3 to 9 packages; each new package costs a full
   worker turn with re-orientation, a supervisor turn, and often a context compaction (up to 5 per task at the 120k
   threshold, each a fresh session that re-reads the repository). Two packages were pure ceremony (a docs-only package,
   "correct the documented full-suite count"), one was scope creep from a repository convention (converting a
   3,800-line component to `<script setup>`: 21 minutes nobody asked for).
4. **The design step on moderate tasks (109 min).** Every moderate feature got a reviewed system design: 4 to 13
   minutes on a good day, **5 design-review rounds (12:45)** on the map-tiles task, 54 minutes on the stopped run.
   Some reviews caught real problems (shared wind-canvas ownership); most rounds after the first polished details.
5. **Exploration and focus group triggered by accident (34 min + the worst failure).** Both map-performance runs
   explored three *visual* directions with a four-persona panel, because the request contained the word "visually"
   ("*visually it look identically the same*"). The chosen direction became a required criterion D1 (a new
   "command deck" home screen) that the owner never asked for; see section 4 (map stutter, 2nd run).
6. **Reviewer turns on a free OpenRouter model (55 min on one run).** Idle timeouts and "free-model requests are used
   up (429)" were retried with back-off six times per role; one design-review turn took 22 minutes.
7. **Owner answers that did not take effect (22 min of the owner's time, 6 refused "done"s).** See section 4.
8. **Verification: 1:46 per run, every run.** `npm run build` is 84 s of it; lint 14 s; the test script fails in 4 s
   on the starting commit (pre-existing), and the baseline re-check runs it again in a separate worktree every task.
   Small in the total (2%), but it is on every run and it repeats identical work.
9. **Environment setup: 21 to 51 s of `npm ci` per task** and 1.5 GB of `node_modules` per worktree (13 worktrees kept
   ≈ 20 GB; one run hit "no space left on device").

## 4. Failures and low scores, classified

| run | score | what went wrong | root cause | class |
|---|---:|---|---|---|
| map stutter (2nd) | 6 | 2h42m; built an unrequested "command deck" UI, removed it again; judge refused "done" 6 times after the owner waived D1 twice and wrote "just make a pr"; PR closed | exploration mis-triggered on "visually … identical"; an owner's waiver in an answer was not applied (only a UI edit counted); "just make a PR" was treated as guidance, not a decision | ceremony mis-trigger, goes in circles |
| map stutter (1st) | 7 | stopped by the owner after 80 min with no code | design reviewer on a free OpenRouter model: idle timeout, then daily quota (429) retried with back-off; focus group failed persona after persona, 3 min each | wrong model pairing, retry storm |
| vessel symbols v1 to v4 | 14 to 39 | four follow-ups, PR closed unmerged | taste: the owner wanted a "fresh idea"; the team iterated on geometry correctness through design reviews instead of showing options first (exploration did not exist yet) | requirements (visual direction) |
| sea route planner | 69 | review round FAIL delivered as follow-up; owner fixed it by hand | real defect ("use selected vessel" never set a point) found only at final review | agent capability |
| playback UTC toggle | 71 | 68 min, human commit after merge | scope creep (script-setup conversion package), revision loop over mock screenshots | ceremony, scope creep |
| map tiles | 71 | 81 min | 5 design-review rounds; 4 revisions (two of them evidence and generated files) | ceremony |

Infrastructure seen across runs: a full root file system (ENOSPC) blocked checks in one run; the scorecard refresh
re-appended the same "Autopsy" event every 30 minutes (300+ duplicate timeline events on one task).

## 5. What changed

| finding | change | where |
|---|---|---|
| queue (1) | **Express lane**: a task triaged solo at intake starts beside a running task instead of waiting (default 1 extra slot; Settings → Workflow) | `autopilot.schedule`, `manager.create_task` |
| ceremony on small work (2, 3, 4) | **Triage + solo fast path.** Team mode per task: Auto (default), Solo, Team. Auto: a keyword/shape heuristic, and for borderline requests a cheap one-shot rating by the supervisor's agent on its cheapest model, run in parallel with `npm ci`. Simple/moderate single-repository work without risk signals runs **one agent** (the worker) that writes its plan and acceptance criteria first (`SOLO_PLAN.md`), implements, runs the tests nearest its change and reports once. Relay still verifies, still gates acceptance on evidence (one nudge), and runs a **light independent check only when risk warrants** (risky area, large change, sensitive files, unproven criteria). A solo worker can hand over to a supervisor (`needs_team`), keeping its session and tree. | `orchestrator/triage.py`, `orchestrator/solo.py` |
| design step (4) | Runs for complex, multi-repository, or moderate contract-changing work (API, schema, integration) only; below complex, one design revision, then the findings become follow-ups | `triage.design_wanted`, `design.py` |
| exploration mis-trigger (5) | Design research and exploration only for an explicit redesign or new look; never when the request keeps the look ("visually the same", "identical"), never for a small tweak, never in solo | `triage.research_wanted`, `triage.exploration_wanted` |
| final review | In Auto, the independent reviewer runs for complex, risky or large changes; the reason is recorded either way | `triage.review_wanted`, `pipeline.finish_done` |
| revisions over generated files (2) | Relay learns which tracked files its checks rewrite and restores them after every worker turn; worker rules: no production build, one browser attempt, no mocks | `pipeline.restore_generated`, `rules/WORKER.md` |
| too many packages (3) | Supervisor rules: one package for simple/moderate, at most three for complex; no docs-only, count-fix, generated-file or evidence-only packages; large convention rewrites become follow-ups | `rules/SUPERVISOR.md` |
| compactions (3) | Compaction threshold 120k → 180k tokens (migrated for installs still on the old default) | `config.py` |
| owner decisions ignored (7) | "waive D1" in any answer or guidance waives it as the owner; "just make a PR" / "deliver now" delivers at the next boundary (checks still run; open items become follow-ups) | `solo.apply_human_text`, `pipeline.done_gate`, `dialogue`, `review` |
| retry storm (6) | A daily quota or an empty balance fails the turn at once instead of six back-offs; a setup-level failure stops the focus group instead of trying each persona | `pipeline.run_role`, `exploration._explore_panel` |
| verification (8) | Same tree + same commands → results reused; build skipped when only tests, docs or specs changed; per-package verification defers the build to the final run; lint and tests run side by side (a build stays alone); "fails on the starting commit too" answered once per repository, commit and command across tasks | `orchestrator/verifyfast.py`, `pipeline.run_verification` |
| setup (9) | `node_modules` restored from a warm hard-linked copy when the lockfile and Node version match (about a second, no extra disk), saved after each successful install; newest 3 per repository kept | `verifyfast.deps_*`, `pipeline.prepare_environment` |
| duplicate autopsy events | A diagnosis is announced once per distinct result | `learning_engine.diagnose` |
| visibility | Task page → Details → **Where the time went**: active and queued time, a stacked bar of the kinds of work, each phase split by kind, and why each heavy phase ran or was skipped | `orchestrator/timing.py`, `/api/tasks/<id>/work`, `inspector.js` |

## 6. Measurement

See the table below (re-runs on scratch copies of the repository at the same starting commit, same agents, push and
pull requests off).

Re-runs of real tasks from the history on a scratch clone of the repository at the same starting commit
(`f5e66121`), in a throwaway container built from this branch, same team (Claude supervisor, Codex worker), Team mode
Auto, push and pull requests off, run one at a time. **Single agent** is Codex alone (`codex exec`, same CLI and login,
same container) in a fresh clone with `node_modules` already installed, given the owner's request verbatim; it runs no
lint, test suite or build of its own unless it chooses to.

| task | before: active (wall incl. queue) | after: Relay | single agent | after ÷ single | outcome after |
|---|---|---|---|---|---|
| fender average line (simple) | 11:30 (86:47) | 6:56 ¹ | 3:42 | 1.87 | solo; lint, tests, build pass; 2 files |
| metrics date picker (simple) | 12:01 (78:22) | **5:47** | 4:32 | **1.27** | solo; checks pass; 2 files |
| playback UTC toggle + picker (moderate) | 68:27 (68:27) | **7:16** | 7:21 | **0.99** | solo (rated moderate by the cheap rating); checks pass |
| playback wind widget (moderate) | 26:09 (91:01) | **13:08** ² | 12:54 | **1.02** | solo + independent check, which found a real bug (range playback never loaded weather); fixed in one round |
| new: rename a chart title (trivial) | – | 5:07 | 2:38 | 1.94 | solo; checks pass; 1 line |
| metrics date picker, Team mode forced | 12:01 | 10:08 | – | – | team path on the new build: 1 package, generated files restored automatically, no review (simple) |

¹ First run on a cold package cache: `npm ci` took 52 s. Every later run reused `node_modules` in about 2 s.
² Before the evidence gate moved ahead of verification: the proof nudge changed files and forced a second full
verification (100 s); the current build runs the checks once on the final tree.

Where the remaining time goes (from the task page breakdown of the re-runs): the worker's own turn is 60 to 70% and
Relay's full verification (`npm run lint`, `npm run test`, `npm run build`, 98 to 116 s, of which the production build
is 83 to 93 s) is most of the rest; setup is 2 s, triage 0 to 13 s (the cheap rating finishes after a cached setup),
orchestration overhead 3 s. For moderate work Relay now matches a single agent while still running the full checks and
the gate. For a trivial change the fixed ~100 s of full verification is the whole difference: the agent part is 1.3x
of a single agent (205 s vs 158 s, the plan file and one browser attempt); a single agent that also ran the same
checks would take 4:16, making Relay 1.2x of that.

The queue share (25% of all historical wall time) is removed for small work by the express lane, tested live: with
one slot busy on a Team task, a queued simple task started at once ("Express lane" event) instead of waiting.

Against the history: the same four requests took 118 minutes of active time before (259 minutes from request to pull
request, queue included) and 33 minutes after, each still verified by the full lint, test and build.
