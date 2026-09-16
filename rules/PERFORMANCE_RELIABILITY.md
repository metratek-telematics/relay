# Performance & Reliability Rules

Do not optimize blindly. Protect obvious hot paths and existing performance characteristics.

Consider:
- unnecessary repeated API calls;
- N+1 queries;
- repeated expensive computation/rendering;
- excessive DOM/component rerenders;
- unbounded lists/log buffers;
- leaking listeners/timers;
- polling frequency;
- large payloads;
- blocking work on UI/main thread;
- repeated map/chart reinitialization;
- unbounded retries;
- retry storms.

Retries must be bounded and appropriate for the failure mode.

For real-time/live systems:
- stale data must be identifiable;
- reconnection behavior must be deterministic;
- duplicate/out-of-order events must not corrupt state;
- resource cleanup must occur on reconnect/unmount.
