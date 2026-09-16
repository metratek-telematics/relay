# Testing & Verification Rules

Testing is evidence, not ceremony.

## Determine the correct verification stack
Inspect repository configuration before inventing commands.
Use existing scripts and tooling.

Possible layers:
- unit;
- component;
- integration;
- API;
- contract;
- E2E/browser;
- lint;
- formatter check;
- typecheck;
- build/package;
- migration validation;
- smoke test.

## Test changed behavior
Every bug fix should receive a regression test when practical.

Every new behavior should be tested at the lowest useful layer and, when important, at a higher integration layer.

## Avoid useless tests
Do not add tests that only:
- assert mocks were called without verifying behavior;
- duplicate compiler/typechecker guarantees;
- snapshot huge unstable trees without reason;
- encode implementation details unnecessarily.

## Failure handling
If a verification command fails:
1. capture the exact failure;
2. determine whether it is caused by the change;
3. fix change-induced failures;
4. distinguish unrelated/pre-existing failures;
5. do not suppress or weaken tests merely to obtain green output.

## Who runs what
The worker runs fast checks focused on its change. The orchestrator runs the full verification commands as a separate phase, and the reviewer reads the results. A check that cannot run for an environment reason is reported in `blocked_checks`, never worked around.

## Proportion
Verification exists to catch real problems, not to produce paperwork. Use the repository's existing test, lint and build commands. Add tests for changed logic where the repository already tests that layer.

Do not build new verification infrastructure (browser harnesses, fixture recorders, mock servers, screenshot matrices, results tables) unless the task asks for it.

## Frontend verification
Run the app and look at the result: the design loop in `DESIGN.md` (desktop and phone width, both themes) is the browser check. While it is open, note console errors and failed requests caused by the change, and try the main interactions you touched.

## Backend verification
Where practical verify:
- happy path;
- invalid input;
- missing entities;
- authorization;
- idempotency where applicable;
- transactional failure;
- concurrency/race behavior where relevant;
- response schema compatibility.

## Database verification
For schema/query/migration work verify:
- migration forwards;
- migration against representative data;
- constraints/indexes;
- query plans where performance matters;
- null/edge cases;
- rollback strategy if project convention supports it.

## Report
Implementation report must list:
- each command run;
- pass/fail;
- meaningful result;
- any command not run and why;
- any pre-existing failures.
