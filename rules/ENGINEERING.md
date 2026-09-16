# Engineering Rules

## Architecture first
Inspect before editing:
- entry points;
- routing;
- state management;
- services/repositories;
- API clients;
- shared utilities;
- domain models/types;
- error handling;
- logging;
- configuration;
- tests;
- build tooling.

Prefer the repository's existing abstraction boundary even if another architecture is personally preferred.

## Reuse
Before creating something new, search for:
- similar components;
- sibling pages;
- helper utilities;
- API wrappers;
- validators;
- hooks/composables;
- stores;
- error components;
- serializers;
- domain enums/types;
- test fixtures.

Reuse when doing so keeps behavior consistent and does not create unnatural coupling.

## Code quality
Prefer:
- explicit names;
- small cohesive units;
- single-purpose helpers;
- simple control flow;
- existing language idioms;
- typed contracts where the project supports them;
- comments explaining why, not restating what.

Avoid:
- speculative abstractions;
- unnecessary factories;
- premature generic utilities;
- deep nesting;
- magic constants;
- duplicated business rules;
- hidden global state;
- catch-all exception swallowing;
- silent fallbacks that hide real failures.

## Compatibility
Preserve unless the task explicitly requires a breaking change:
- public APIs;
- route shapes;
- DB schema assumptions;
- serialized data;
- event names;
- environment variables;
- configuration keys;
- component props/events;
- externally consumed behavior.

When compatibility must change, update all in-repo consumers and document it.

## Dependencies
A new dependency requires a concrete justification.
Before adding one:
1. verify the capability does not already exist;
2. check whether native/platform functionality is enough;
3. prefer dependencies already used by the repo;
4. consider bundle/runtime/security impact;
5. avoid dependency additions for trivial helpers.

## Error handling
Errors must be:
- observable;
- actionable;
- correctly scoped;
- not silently converted into success.

Do not leak secrets or internal stack traces to end users.
Preserve useful diagnostic context for developers.

## Concurrency / async
For asynchronous code:
- handle cancellation/unmount;
- prevent stale response races;
- avoid double submission;
- avoid state updates after disposal;
- make loading and error state transitions deterministic;
- clean up timers/listeners/subscriptions.

## Resource cleanup
Clean up:
- listeners;
- intervals/timeouts;
- observers;
- sockets;
- temporary files;
- object URLs;
- map/chart instances;
- subprocesses;
- test artifacts.

## Configuration
Never hard-code environment-specific secrets, hosts, tokens, passwords, or production identifiers.
Respect existing config/env patterns.
