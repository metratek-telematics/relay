# Run insights: what went wrong in real Relay runs

This page is based on real runs. It lists what cost time, money or trust in real Relay runs, and says for each finding whether it was fixed, and where. Change it when new runs show something else.

## Sources

- **Task records:** `data/state/tasks.json` plus its 12 backups. Together they hold 5 tasks from 2026-09-17:
  - `20260917-093421-dec840`, `20260917-100046-9e59a6` and `20260917-114811-343740` on optimax-eta-server;
  - `20260917-095932-e4f77e` on optimizer;
  - `20260917-100241-35c4df` on navitrak-vue.
- **Run folders:** 4 of them (`raw.log`, `messages.jsonl`, the `*.md` artifacts). For task 343740, which was deleted, only `RETRO.md` is left.
- **Deleted tasks:** `deleted_tasks.json` lists 17 ids. The 12 from 2026-09-16 left no record or log, only their pull requests.
- **Other state:** `scorecards.json` and the lessons store, which holds 7 approved lessons in 3 repositories.
- **Pull requests:**
  - navitrak-vue #402, #403, #404, #405 and #407 (#406 does not exist);
  - optimax-eta-server #1;
  - optimizer #2.

Costs and durations come from each task's `metrics` and scorecard.

| Task | Repository | Result | Time | Cost |
|---|---|---|---:|---:|
| dec840 | optimax-eta-server | PR #1, 3 WPs, 1 revision | 12 min | $3.03 |
| e4f77e | optimizer | PR #2 (merged), 2 WPs | 10 min | $2.80 |
| 9e59a6 | optimax-eta-server (follow-up) | pushed to PR #1, 3 WPs, 1 revision | 9 min | $3.21 |
| 35c4df | navitrak-vue | PR #407 (merged), 2 WPs | **92 min** (78 min waiting for a human) | $3.35 |
| 343740 | optimax-eta-server (follow-up) | **stopped by the user**, score 3/100 | 18 min | $3.64 |

## Findings

### 1. A missing system library meant the database tests never ran (fixed)
- **Evidence:** on optimax-eta-server, `tests/test_connect.py` and `tests/test_disconnect.py` import `pyodbc`. Importing it fails with `ImportError: libodbc.so.2: cannot open shared object file`, because the image has no `unixodbc`.
- **How often:** in all 3 tasks on that repository.
  - The worker added `--ignore=tests/test_connect.py --ignore=tests/test_disconnect.py` to every work package.
  - Relay logged 5 "Checks could not run" events.
  - The retrospective of 343740 names it as a root cause: "verification remained blind throughout the run".
- **Fix:** repository environments now have **System packages**, a list of apt package names.
  - **Validation:** names are checked with Debian's naming rules, up to 40 packages.
  - **Install:** Relay installs the packages in its own container before the setup command. dpkg is checked first, so apt only runs for packages that are missing. Installs never run at the same time, and a package set that is already in place is not installed again.
  - **Failure:** a failed install becomes a blocked check that needs action.
  - **Non-root:** when Relay is not root and has no passwordless sudo, it says so plainly.
  - **Detection:** the environment editor suggests packages it finds in the repository's manifests, for example "Detected: pyodbc in requirements.txt needs unixodbc unixodbc-dev". It covers pyodbc, psycopg2, lxml, Pillow, mysqlclient, python-ldap, pycairo, opencv-python and others, plus node canvas and odbc.
  - **Without saved packages:** a task records a "System packages suggested" timeline entry.
  - **Code:** `orchestrator/syspkgs.py`, `repo_env.system_packages`, `Pipeline.ensure_system_packages`, `web/js/views/repoenv.js`, and the apt-cache comments in `docker-compose.yml`.

### 2. Verification passed while the test suite never ran (fixed)
- **Evidence:** in dec840 and 9e59a6, `python -m pytest -q` exited with code 2, `Interrupted: 2 errors during collection`. It failed the same way on the starting commit, so Relay labelled it "PRE-EXISTING FAILURE … does not block" and reported **"Verification passed 2/2"**.
- **What that means:** not one test ran at delivery.
- **What followed:** the next request on that repository was "revert … you have broken the scraping urls xpaths" (343740).
- **Fix:** Relay now spots a test command that stopped before running tests: pytest collection or import errors, `cannot open shared object file`, `Cannot find module`.
  - It stays non-blocking, because judging is not part of this change.
  - It is recorded as a blocked check: "the test suite did not run, so this check proves nothing".
  - When the cause is a missing library, the check needs action and names the package, for example `libodbc.so.2 (package unixodbc)`.
  - The verdict reads "PRE-EXISTING FAILURE, THE SUITE DID NOT RUN".
  - **Code:** `suite_not_run_reason` in `pipeline.py`.

### 3. Workers could not find `python` (fixed)
- **Evidence:** the Codex worker hit `/bin/bash: line 1: python: command not found` in all 3 Python tasks (dec840, e4f77e, 9e59a6), each time on its first check. It then worked around it with `PATH=.venv/bin:$PATH`, `.venv/bin/python` and `PYTHONPATH=.`. In dec840 it also recorded a blocked check: "python executable is unavailable".
- **Root cause:** agent CLIs run their shell tool as `bash -lc`. The login shell rebuilds PATH from `/etc/profile`, which drops the worktree `.venv` that Relay had put first. This was reproduced in the image: `bash -lc 'which python'` found nothing.
- Relay's own `run_shell` already worked around this, but agent turns did not.
- **Fix:** the runner exports `RELAY_PATH` to agents, and the image's `/etc/profile.d/relay-path.sh` restores it.
- **Checked in E2E:** a worker's `bash -lc 'python --version && which python'` now prints the worktree's `.venv/bin/python`.

### 4. Relay committed files that its own setup created (fixed)
- **Evidence:** optimax-eta-server PR #1 contains 6 `OPTIMAX_ETA.egg-info/*` files. Relay's detected setup runs `pip install -e .`, which creates them, and delivery's `git add -A` committed them. In 343740 the first work package had to untrack them.
- **Reproduced in E2E:** the same happened with `__pycache__/*.pyc` in a repository without a `.gitignore`.
- **Fix:** for Python repositories, `__pycache__/`, `*.egg-info/` and `.pytest_cache/` go in the repository's `.git/info/exclude` alongside `.venv/`. This also applies when a saved or custom setup command replaces detection.
- **Checked in E2E:** the delivery commit contains only the source and test files.

### 5. Nothing stopped an agent from committing (fixed)
- **Evidence:**
  - `rules/PROTOCOL.md` said "Do not commit unless the supervisor explicitly asks", and nothing enforced even that.
  - In 343740 the plan was "Undo both agent commits (568df83, a6f1ee2)". Those were Relay's own delivery commits, so the supervisor planned to rewrite history. Its run log was deleted, so how the worker did it cannot be checked.
  - In E2E, before the guard existed, a worker told to commit simply committed.
- **Fix:** Relay records the commit agents work on top of, `checkpoint.head`. After every agent turn, if HEAD has moved, it runs `git reset --soft` back to that commit.
  - Every change stays staged in the worktree.
  - A timeline entry ("Agent commit undone") and a conversation notice are added, and the supervisor gets a note at its next evaluation.
  - If the agent also pushed, delivery pushes with `--force-with-lease` against exactly that pushed commit.
  - If the agent switched branches, Relay leaves the refs alone and delivery refuses as before.
  - The rules no longer allow commits: agents never commit, amend, create revert commits or run apt-get, and the supervisor must not ask for any of these.
  - **Code:** `orchestrator/commitguard.py` and `Pipeline.guard_commits`.
- **Checked in E2E:** the worker committed `b0780bc add greeting`, Relay reset to `20bf73a`, the supervisor saw the change still staged, and Relay's single delivery commit carried it.

### 6. A refactor broke an integration that nobody could exercise (rule change)
- **Evidence:** dec840 rewrote how the vessel-data scraper fetches pages. It deleted and re-added `process_vessel.py` and added a new JSON fetch client, with tests that used a fake driver only. Nothing touched the live site, and verification had not run (finding 2).
- **What followed:** the user asked for a full revert, then stopped the follow-up (343740). Together the 3 runs cost about $9.90, and PR #1 is still open.
- **Fix:** a new section in `rules/SUPERVISOR.md`, "Code that talks to systems you cannot reach". Keep URLs, selectors, request shapes and field names unless the request asks to change them. Prefer hardening in place to rewriting. List what could not be exercised under unknowns.

### 7. Pull request titles and task names (fixed in part)
- **Evidence:**
  - Names are the first 60 characters of the request, cut mid-word: "…find holes and b" (optimizer #2), "…we should be able " (#407), "…clarity usabil" (#404).
  - #405 was titled "Skip to content", with branch `feat/skip-content`, because the request was pasted from a web page.
  - Follow-ups pile up prefixes: "Follow-up: Follow-up: …" (343740).
- **Fix:** `gitops.auto_task_name` skips pasted page boilerplate and bare URLs, and cuts at a word boundary with "…". It is used for task names and for the branch-name preview. A follow-up gets a single "Follow-up:" prefix.
- **Not fixed:** typos from the request still reach titles. Titles built from the plan summary would be better.

### 8. A question sent to a finished task was silently dropped (fixed)
- **Evidence:** at 12:04 the user wrote to 35c4df, after delivery: "are the db changes done do i have to do anythinhg ther". It was stored as guidance with `consumed: false` and nothing ever read it. The toast said "Guidance queued".
- **Fix:** a message to a done task now returns `applied: "finished"`. The UI shows a warning: "Message saved, but this task has finished · Start a follow-up task to act on it."

### 9. A human wait held the queue for 78 minutes (not changed: recommendation)
- **Evidence:** 35c4df finished its implementation at 10:30. It then waited until 11:48 for the human to waive a failure that was unrelated and already there before the task: `tests/system-metrics.spec.js`, a CPU temperature check. It delivered at 11:50. Agent work took about 13 of its 92 minutes.
- **Why it blocks the queue:** with `max_parallel: 1`, the scheduler counts a waiting task that has a runner as active, so no other task can start meanwhile. Task 343740 was created at 11:48 and started at 11:50, right after.
- **Recommendation:** for 24/7 running, let tasks that wait for a human give up their slot. Also let a failure that is proven to exist on the starting commit be recorded as a follow-up without asking. That is judge work, owned elsewhere.

### 10. "Not verified" in delivered pull requests (recommendation)
- **Evidence:**
  - navitrak-vue #405 was "not checked in a browser with real vessel data, because no API was available locally".
  - #407 says the sensor service was not reachable and the switch dialog was never opened in a browser. Its screenshot check was blocked because the local backend returned 404/502.
- **Recommendation:** use the repository environment's services, or a recorded mock backend, so UI tasks can render real states. Treat "Not verified" items as review focus in the PR checklist.

### 11. Generated files end up in pull requests (recommendation)
- **Evidence:** navitrak-vue #407 changes `public/version.json` and `public/map-preconnects.js`. The build the worker ran rewrote them.
- **Why the existing safeguard missed it:** Relay's verification restores files that only its checks rewrote, but these changed during the worker's own turn.
- **Recommendation:** keep a per-repository list of build-stamped paths that Relay restores before committing.

### 12. Smaller observations
- **Unanswered repeat finding:** in 343740 the same blocking finding came back after 2 fix attempts. The worker kept an `except … continue` that skipped the insert. The loop guard escalated as designed (12:01), and the user stopped the task at 12:09.
- **Supervisor error:** in 9e59a6 the supervisor gave the wrong UTC semantics and then revised its own instruction, which cost a paid round. The lesson it produced ("state the semantic intent explicitly") is now approved.
- **Retrospectives:** they run on a small model at low effort. They list non-problems as "what went wrong", such as "Pull request was opened in draft status" and "score of 75/100 indicates…". Scorecards and lessons are owned elsewhere.

## End-to-end check (throwaway container, Claude sonnet at low effort, no push)

1. **Detection:** a scratch Python repository with `pyodbc==5.2.0` in `requirements.txt`, and a test that imports it. The environment editor suggested `unixodbc unixodbc-dev`. Clicking Add and saving stored them.
2. **Task on a fresh container:** timeline "System packages installed · unixodbc unixodbc-dev" (about 3 s), then "Environment ready". The worker's `python -m pytest -q` gave `2 passed`, and Relay's verification passed. Before the fix, a repository that imported `pyodbc` could not import it in this image.
3. **Commit guard:** a task whose request told the worker to commit. The worker committed `b0780bc add greeting`, Relay undid it ("Agent commit undone"), and the change was delivered in Relay's single commit.
4. **Artifacts:** after the artifact fix, the delivery commit contained only `src/db.py` and `tests/test_db.py`, and `__pycache__`, `.pytest_cache` and `.venv` were ignored.
