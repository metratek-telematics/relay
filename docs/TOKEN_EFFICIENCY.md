# Token efficiency

What Relay does to spend fewer tokens per task, how it measures it, and what it measured.
Code: `orchestrator/tokens.py`, `orchestrator/protocol.py`, the hooks in `orchestrator/pipeline.py` and
`orchestrator/runner.py`, and the Claude and Codex adapters in `orchestrator/agents.py`.
UI: Settings → Token efficiency.

## Measuring first

Every agent turn records, in the task's `metrics.log`:

| Field | Meaning |
|---|---|
| `input`, `cached`, `cache_write`, `output`, `cost_usd` | What the CLI reported. Claude's `input` excludes cache reads and cache writes; Codex's `input` includes its cache reads. `tokens.prompt_tokens()` normalizes both. |
| `sections` | Relay's own part of the prompt by section, in estimated tokens (characters ÷ 4): `rules`, `protocol`, `tools`, `task`, `lessons`, `context` (repositories, system map), `environment`, `packet`, `judge` (acceptance contract), `report`, `verification`, `diff`, `guidance`, `instruction`. |
| `prompt_est`, `context` | Estimated prompt size, and the session's context size (Claude reports it per API call; other CLIs are estimated from what went in and came out). |
| `mcp` | MCP servers loaded for the turn. |

Prompts are `tokens.Prompt` strings that carry their section sizes, so measuring costs nothing extra.
`GET /api/tokens?days=30` aggregates by role, agent, section, day and task. Claude's usage is also counted once per
API message now; before, a message split over several stream events could be counted more than once.

## Techniques

1. **Lean agent context (Claude, `token_lean_agent_context`, on).** Agents run with `--strict-mcp-config`,
   `--disable-slash-commands`, `--setting-sources project,local` and `ENABLE_CLAUDEAI_MCP_SERVERS=false`, so the
   owner's personal plugins, skills, slash commands and claude.ai connectors (41 MCP servers, 65 skills and
   7 plugins on the test machine) are not described on every API call. The task's tools come from Relay instead,
   and the repository's own `CLAUDE.md` and project settings still load. Measured on a one-line reply with Sonnet:
   36,995 → 31,113 context tokens per call (−16%). A turn makes 10 to 40 calls.
2. **Stable prefix first.** Kickoff prompts are ordered role → rules → how the session works → tools → task →
   lessons and system map → environment → packet → guidance → the job. The rules are byte-identical for the same
   role and task keywords, so providers that cache on a prefix (OpenAI automatically, from 1,024 tokens) reuse it
   across tasks. Anthropic's cache only matches up to the breakpoint Claude Code writes, so there it helps within a
   session, not across tasks.
3. **Deltas, not re-pastes (`token_prompt_deltas`, on).** The CLIs keep session memory, so Relay does not send a
   live session what it already has: an unchanged acceptance contract becomes one line, unchanged verification
   results become their verdict lines, the reply format after a worker report is explained once, and files already
   shown by git status are not listed twice. A new or restarted session gets everything again.
4. **Failure excerpts (`token_failure_excerpts`, on).** A failing check sends the runner's failure summary
   (pytest's short test summary, jest `●` blocks, compiler errors) and error lines with a line of context,
   deduplicated, plus the final verdict line, instead of the last N characters of the log.
5. **Targeted diffs (`token_diff_mode`, `targeted`).** The reviewer gets a diffstat, whole hunks for as many files as
   fit the budget (smallest first) and, for the rest, the `git diff <base> -- <path>` command to fetch them. On a
   re-review, files whose diff did not change since the last round are named, not re-sent.
6. **Output contract.** `rules/PROTOCOL.md` → *Output budget*: report what changed, never restate instructions,
   reference `path:line` instead of pasting code, evidence as command + one-line result, short envelope fields.
   Codex runs with `model_verbosity="low"` (`token_codex_verbosity`); Claude's per-response cap is available as
   `token_claude_max_output_tokens` (off by default: too low truncates large file writes).
7. **Compaction (`token_compact_threshold`, 120,000).** When a session's context passes the threshold, the next
   turn starts a fresh session whose prompt is the role's stable prefix plus a handoff Relay builds from its own
   records (plan, contract with statuses, open findings, progress, changed files, blocked checks) and the current
   message. It needs no model call and works for every CLI. The same handoff now rebuilds a session that broke,
   instead of sending a follow-up message to a session that has never seen the task.
8. **Cheaper models where the work is mechanical.** Claude subagents already run on `subagent_models` (haiku) and
   retrospectives on the cheap subagent model at the lowest effort. Relay does not switch models or effort inside a
   live session: a different model misses the whole prompt cache, and on Anthropic a thinking change invalidates it.
9. **Only the tools a task needs.** MCP tool definitions cost tokens on every call. Tools are scoped globally, per
   repository and per task (Settings → Tools), and Claude loads only the task's servers.

## Measured

### Relay's prompt text for the same simulated dialogue

`promptsize.py` builds the prompts of origin/main (ead2091) and this branch from identical inputs: a four-criterion
contract, a failing pytest run (600 passing lines and one failure), a worker report, and a review diff of two small
files plus a 3,000-line lockfile. Characters:

| Prompt | Before | After | Change |
|---|---:|---:|---:|
| Supervisor kickoff | 35,670 | 36,838 | +3% (tools block, output budget rules) |
| Supervisor after a worker report, first | 3,137 | 2,266 | −28% |
| Supervisor after a worker report, later turns | 3,137 | 891 | −72% |
| Six after-report turns in total | 18,822 | 6,721 | −64% |
| Reviewer kickoff | 50,295 | 28,832 | −43% |
| Re-review, round 2 | 25,048 | 1,240 | −95% |

### A/B on a real task

Same small task ("add `word_count` and `truncate_words` with tests") run end to end in throwaway containers built
from origin/main (ead2091) and from this branch: Codex supervisor and reviewer at low effort on the CLI's default
model, Claude Sonnet worker at low effort, verification after each report, no push, no pull request. Two runs on
main, three on the branch.
Every run finished in one work package, verification passed and the review passed in round 1, so quality was the
same.

| Run | Prompt tokens | Cached | Output | Cost (reported/estimated) |
|---|---:|---:|---:|---:|
| main #1 | 465,426 | 402,649 | 4,486 | $0.310 |
| main #2 | 488,584 | 433,819 | 4,988 | $0.309 |
| branch #1 | 507,129 | 420,846 | 5,112 | $0.286 |
| branch #2 | 393,888 | 332,293 | 4,803 | $0.234 |
| branch #3 (after merging main again) | 482,388 | 410,957 | 4,717 | $0.262 |
| **mean, main → branch** | 477,005 → 461,135 (−3%) | | 4,737 → 4,877 (+3%) | **$0.310 → $0.261 (−16%)** |

The Claude worker turn fell from $0.185 to $0.143 on average (−23%): the lean agent context, since each of its API
calls re-reads the whole context. An earlier branch run without the lean context cost exactly as much as main
($0.310), so on a one-package task the prompt deltas, diffs and excerpts have nothing to act on: they pay off from
the second work package and review round, as the table above shows. This is a small sample: the branch runs
range from $0.234 to $0.286 because the worker read files and ran tests a different number of times.

Most of a turn's tokens are the CLI's own system prompt, tool definitions and the tool results of the calls inside
the turn, re-read on every call; Relay's text was about 5% of the prompt tokens in these runs. That is why the
Token efficiency panel shows cache hits and output share next to Relay's sections, and why cutting what the CLI
loads (technique 1) and how many calls a turn needs matters more than trimming Relay's own wording.
