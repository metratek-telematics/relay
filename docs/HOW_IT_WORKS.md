# How it works

## A task from start to finish
1. You pick a repository, describe the change once, and choose a team preset (for example *Codex supervises Claude*).
2. The orchestrator creates an isolated git worktree and branch. Uncommitted changes in your checkout are carried over so the team builds on your work.
3. **Supervisor kickoff.** The supervisor inspects the repository with its own tools and replies with a plan, objective acceptance criteria and the first work package.
4. **Worker turn.** The worker receives the work package, implements it with full tool access, runs local checks, and replies with a report (`complete`, `partial` or `blocked`). It may ask the supervisor or you a question instead.
5. **Verification.** The orchestrator runs detected or configured commands (pytest, npm test, gradle, …) and records the results.
6. **Supervisor evaluation.** The supervisor sees the report, verification output, `git status` and the acceptance contract, inspects the diff itself, and decides: next work package, a revision naming the criteria or blocking findings it addresses, a question, or `done` with per-criterion evidence and a PR summary. Relay refuses `done` while a required criterion is unproven or its own checks fail.
7. **Independent review (optional).** A separate session (any agent) inspects the diff and returns PASS or FAIL with findings classified `blocking`, `should_fix` or `nit`. Only blocking findings go back to the supervisor; the rest become pull-request follow-ups. Findings that keep recurring, revisions that change nothing and exhausted budgets are put to you as a question instead of looping or failing.
8. **Delivery.** Optionally after your approval: commit, push, draft PR, final report.

## Talking to the team
- The composer sends **guidance** to the next turn, a specific role, or both. Choose *Interrupt now* to stop the running agent immediately and restart its turn with your note.
- When an agent asks a **question**, the task shows *Needs input*; answer from the card or the composer.
- With the **approval gate** on, delivery waits for you; *Request changes* sends your note back to the supervisor as a revision.
- **Pause** takes effect after the current turn; **Stop** terminates the process. **Resume** continues from the checkpoint using the same agent sessions, even after the app restarted.
- The **Repository** tab edits files directly in the worktree; agents and reviewers see the change on their next turn.

## Agents, sessions and tools
Each role has its own persistent CLI session, so agents remember everything about the task and inter-agent messages stay short.
Agents run unattended with their native tools (file edits, shell, search, web, configured MCP servers) inside the worktree:
Codex in a `workspace-write` sandbox, Claude with permissions bypassed, Gemini in YOLO / auto-edit mode.
Change these under **Settings → Agents**, including per-agent environment variables (for example `GOOGLE_CLOUD_PROJECT` for Gemini).

## Costs and observability
Token usage per role and agent is read from the CLI streams; Claude also reports dollar cost. The Sessions tab shows session ids you can resume in your own terminal, the Logs tab shows the raw stream with per-agent filters, and every artifact is a plain file under `runtime/<task>/`.

## Learning from finished tasks
When a task ends, Relay scores it (0 to 100, formula in `orchestrator/scorecard.py`) and keeps checking its pull request for two weeks: merged, closed unmerged, or fixed by a human after delivery. One short retrospective turn (the supervisor's agent at its lowest effort, on its cheap subagent model when one is set) reads a digest of the run and proposes a few one-sentence lessons. They wait on the **Lessons** page until you approve, edit or reject them; approved lessons for a repository, plus global ones, are added to the supervisor's and worker's kickoff prompts on later tasks (15 at most). The dashboard's Success section shows the weekly success rate, average score, results by repository and by team, and why tasks fail.

## Safety
Agents are told not to commit, push, merge or deploy; the orchestrator performs commits and opens **draft** PRs only. Nothing is merged automatically.
