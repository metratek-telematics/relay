# Data / API / Database Rules

## Contracts
Treat existing API and persisted data shapes as contracts.

Before changing:
- inspect consumers;
- inspect nullability;
- inspect units;
- inspect enum values;
- inspect time-zone/time semantics;
- inspect error semantics.

## Values
Never confuse:
- zero with missing;
- empty with unavailable;
- stale with current;
- null with false;
- sentinel values with legitimate readings.

## Time
Preserve explicit time-zone semantics.
Do not silently convert timestamps unless required.
Avoid local-time assumptions in persisted/server data.

## Units
Do not mix units.
Use existing unit conversion helpers and domain conventions.

## Database
Prefer:
- constraints for invariants;
- parameterized queries;
- existing transaction boundaries;
- indexes justified by access patterns.

Do not perform destructive schema/data actions without explicit authorization.

## API changes
When request/response behavior changes:
- update clients;
- update types;
- update tests;
- preserve compatibility when possible;
- document breaking behavior.

## Database changes
- Change schemas with migration files in the repository (its migration tool or numbered SQL files), with a way back
  (down migration or reversal notes). Never change a shared or production database by hand.
- Read the current schema first: use a database connector (`relay-connect schema <name> <table>`, read-only) when the
  task has one, otherwise the repository's migrations and models.
- Prove the migration on a disposable database: the task's integration stack (`relay-stack up`, which starts the
  repository's own Postgres service and runs its fixtures) or a local one the tests create. Run it up, run the tests,
  and run it down again when a down path exists.
- Report the migration, how it was tested, and anything a human must do when deploying it (order, backfill, downtime).
