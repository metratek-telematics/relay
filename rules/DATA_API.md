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
