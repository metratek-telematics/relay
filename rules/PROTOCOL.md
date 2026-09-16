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
   "acceptance":["observable criterion derived from the requirements"],
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
  `{"type":"decision","decision":"revise","summary":"one line","instruction":"exact required fixes"}`
  `{"type":"decision","decision":"done","summary":"one line","pr_summary":"markdown summary suitable for a pull request description"}`
- Question to the human (only for genuine product/credential decisions):
  `{"type":"question","to":"user","question":"...","options":["Option A","Option B"]}`
- Question to the worker:
  `{"type":"question","to":"worker","question":"..."}`

## Envelopes the worker may send
- Report when a work package is finished or cannot progress further:
  ```
  {"type":"report","status":"complete","summary":"one line","report":"markdown: what changed, decisions, checks run with results, limitations","files":["path/one","path/two"],
   "blocked_checks":[{"check":"npm test","reason":"private package registry needs credentials this machine does not have","impact":"automated frontend tests could not run","action_required":false}],
   "blockers":[{"reason":"required API schema cannot be accessed","impact":"implementation cannot continue safely","action_required":true}]}
  ```
  `status` is one of `complete`, `partial`, `blocked`. `blocked_checks` and `blockers` are optional; omit them when empty.
  - `blocked_checks`: a check that could not run for an environment reason. Record it and keep implementing.
  - `blockers`: something that stops the implementation itself.
  - `action_required: true` only when the user alone can clear it (credentials, access, a product decision). The orchestrator then asks the user; otherwise nobody is interrupted.
- Question to the supervisor or the human:
  `{"type":"question","to":"supervisor","question":"..."}`
  `{"type":"question","to":"user","question":"...","options":["..."]}`

## Envelopes the reviewer may send
- `{"type":"review","verdict":"PASS","summary":"one line","findings":[]}`
- `{"type":"review","verdict":"FAIL","summary":"one line","findings":[{"severity":"blocking","file":"path","problem":"...","fix":"..."}]}`

## Messages you will receive
- `MESSAGE FROM SUPERVISOR`, `REPORT FROM WORKER`, `REVIEW FROM REVIEWER`: messages from teammates.
- `ANSWER FROM HUMAN`: the human answered a question.
- `USER GUIDANCE`: the human changed or clarified the request. Treat it as an update to the requirements.
- `ORCHESTRATOR`: verification results, changed files, and control notes such as an interrupted run being resumed.

## Boundaries for every agent
- Work only inside the task worktree.
- Never push, merge, deploy, force-reset, delete branches, or touch production systems. The orchestrator commits and opens the pull request.
- Do not commit unless the supervisor explicitly asks; leave changes in the working tree.
- Do not wait for input inside your own tools. If you need something, send a question envelope.
