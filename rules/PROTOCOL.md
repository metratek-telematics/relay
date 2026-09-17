# Team Conversation Protocol

The orchestrator relays messages between the supervisor, the worker, the optional reviewer, and the human. Sessions are persistent: each agent remembers its whole conversation for this task.

Every reply MUST end with exactly one fenced JSON envelope:

```json
{ "type": "...", ... }
```

Text before the envelope is fine (reasoning, notes, evidence). The envelope is what the orchestrator acts on. Keep the JSON valid: double quotes, no trailing commas, no comments.

## Envelopes the supervisor may send
- First reply only, the plan as a context packet plus the first work package:
  ```
  {"type":"plan","summary":"one line","plan":"short markdown: the work packages in order",
   "requirements":["the user's request, restated, nothing added"],
   "acceptance":[{"id":"A1","criterion":"observable outcome derived from the requirements","how_to_verify":"test: python -m pytest tests/test_x.py","required":true}],
   "optional":["improvement the user did not ask for; never blocks done"],
   "known_files":{"primary":["path"],"supporting":["path"]},
   "findings":[{"file":"path","finding":"what you observed"}],
   "constraints":["e.g. no new dependencies"],
   "unknowns":["e.g. full verification may need private registry access"],
   "instruction":"first work package: the concern, the files, the expected result"}
  ```
- Next work package:
  `{"type":"instruction","summary":"one line","instruction":"exact work package"}`
- Decision after a worker report:
  `{"type":"decision","decision":"revise","summary":"one line","addresses":["A2","F1"],"findings":[{"severity":"blocking","file":"path:line","problem":"...","fix":"...","criterion":"A2"}],"instruction":"exact required fixes"}`
  `{"type":"decision","decision":"done","summary":"one line","criteria":[{"id":"A1","status":"met","evidence":"python -m pytest -q → 12 passed"}],"follow_ups":[{"severity":"should_fix","file":"path","problem":"..."}],"pr_summary":"markdown summary suitable for a pull request description"}`
  - `addresses`: the acceptance criterion ids (A…) and blocking finding ids (F…, assigned by Relay) the revision fixes. A revise needs these or blocking findings.
  - `criteria`: one entry per criterion; `status` is `met`, `unmet` or `waived`; `evidence` is concrete (command and result, file:line, screenshot path). A required criterion without met + evidence blocks done; only the human waives a required one.
  - `severity`: `blocking` (wrong behaviour against the contract, failing required check, security issue, data loss, broken build), `should_fix` or `nit`. Only blocking findings justify another round; the rest are `follow_ups`.
- Question to the human (only for genuine product/credential decisions):
  `{"type":"question","to":"user","question":"...","options":["Option A","Option B"]}`
- Question to the worker:
  `{"type":"question","to":"worker","question":"..."}`
- Proposal to add a repository the request depends on (the human accepts with one click; Relay clones it and adds a worktree on the task branch):
  `{"type":"question","to":"user","question":"why it is needed","add_repo":{"repo":"component id or owner/name","reason":"one line"},"options":["Add api to this task","Continue without it"]}`
- Multi-repository plans add `"work_packages":[{"id":"W1","repo":"api","summary":"...","depends_on":[]}]` and `"system_design":{"summary":"...","api_contracts":[{"provider":"api","consumer":"web","endpoint":"POST /items/{id}/archive","request":"...","response":"..."}],"data_model":[{"component":"db","change":"..."}],"sequence":["..."]}`; instructions add `"package":"W1","repo":"api"`.

## Envelopes the worker may send
- Report when a work package is finished or cannot progress further:
  ```
  {"type":"report","status":"complete","summary":"one line","report":"markdown: what changed, decisions, checks run with results, limitations","files":["path/one","path/two"],
   "blocked_checks":[{"check":"npm test","reason":"private package registry needs credentials this machine does not have","impact":"automated frontend tests could not run","action_required":false}],
   "blockers":[{"reason":"required API schema cannot be accessed","impact":"implementation cannot continue safely","action_required":true}]}
  ```
  `status` is one of `complete`, `partial`, `blocked`. In a multi-repository task add `"package":"W1"` for the package you finished. `blocked_checks` and `blockers` are optional; omit them when empty.
  - `blocked_checks`: a check that could not run for an environment reason. Record it and keep implementing.
  - `blockers`: something that stops the implementation itself.
  - `action_required: true` only when the user alone can clear it (credentials, access, a product decision). The orchestrator then asks the user; otherwise nobody is interrupted.
- Question to the supervisor or the human:
  `{"type":"question","to":"supervisor","question":"..."}`
  `{"type":"question","to":"user","question":"...","options":["..."]}`

## Envelopes the reviewer may send
- `{"type":"review","verdict":"PASS","summary":"one line","findings":[]}`
- `{"type":"review","verdict":"FAIL","summary":"one line","findings":[{"severity":"blocking","criterion":"A2","file":"path:line","problem":"...","fix":"..."},{"severity":"nit","file":"path","problem":"...","fix":"..."}]}`
- `severity` is `blocking`, `should_fix` or `nit`. FAIL needs at least one blocking finding; the others become pull-request follow-ups.

## Asking for a tool (any role)
- Add `"tool_request":{"name":"fetch","why":"one line"}` to any envelope, or run `relay-tools request <name> "<why>"`. Use a catalog id (`relay-tools catalog`) when one fits; otherwise add `"npm"`, `"pip"`, `"command"` (an MCP server command) or `"url"` (a remote MCP server). The owner's policy approves it or asks the owner; an approved tool is available from your next turn. Ask only when it clearly saves work, and never wait for the answer.

## Output budget (every reply costs tokens; later turns re-read it)
- Report what changed and what you found, not what you were asked. Never restate instructions, the plan, the diff or earlier messages.
- Reference code as `path:line` or a symbol name; paste code only when a few lines are the evidence.
- Evidence is the command and its one-line result (`python -m pytest -q` → 12 passed), not the full log.
- Envelope fields stay short: `summary` one sentence; `report` at most about 15 bullets; findings one line each.
- Text before the envelope is optional; keep it to a few lines when you use it.

## Messages you will receive
- `MESSAGE FROM SUPERVISOR`, `REPORT FROM WORKER`, `REVIEW FROM REVIEWER`: messages from teammates.
- `ANSWER FROM HUMAN`: the human answered a question.
- `USER GUIDANCE`: the human changed or clarified the request. Treat it as an update to the requirements.
- `ORCHESTRATOR`: verification results, changed files, and control notes such as an interrupted run being resumed. To save tokens Relay does not repeat what your session already has: "unchanged since your last message" means exactly that.

## Boundaries for every agent
- Work only inside the task worktree (or worktrees, when the task spans several repositories).
- Never push, merge, deploy, force-reset, delete branches, or touch production systems. The orchestrator commits and opens the pull request.
- Never commit, amend or create revert commits, even when a teammate asks; leave changes in the working tree. Relay undoes any commit an agent makes (soft reset, changes kept) and commits once at delivery. To undo earlier work, restore files (`git checkout <commit> -- <paths>`).
- Never install system packages (apt-get, sudo). A missing system library is a blocked check that names the package; the human adds it under Repositories → Environment.
- Do not wait for input inside your own tools. If you need something, send a question envelope.
