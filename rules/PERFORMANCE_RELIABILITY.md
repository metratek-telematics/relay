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

## Browser and map performance: measure, find the dominant cost, fix it, prove it

A screenshot is not a measurement. "Feels faster" is not evidence. Work from `relay-perf` numbers.

1. **Baseline first.** Before changing anything, run `relay-perf run <url> steps.json --throttle 1,4 --out <run>/perf/before`
   (Relay records one for performance tasks right after the plan: read its PERF_REPORT.md). Use a scenario that reproduces the
   complaint: load the busy view, then pan and zoom for ~20 s with `{"phase":"pan"}` / `{"phase":"zoom"}` so each is measured.
   4x CPU throttle is an older laptop or a mid-range phone; if it is only smooth at 1x, it is not smooth.
2. **Find the dominant cost.** Read, in order: long tasks (what ran inside them, their hottest functions), the top functions by
   self time and by total time (file:line, % of the main thread), the scripting / rendering / painting / GC split, the threads
   table (who uses the cores), forced layouts, heap after GC, requests and websocket messages during the interaction. Fix the
   biggest thing first; one change that removes 40% of main-thread time beats ten micro-optimisations.
3. **Typical causes in map apps and what fixes them:**
   - every feature re-styled or re-created every frame / on every update: cache styles by a small key (type, heading bucket,
     selected), never build `Style`/`Icon`/`Text` objects inside a style function; update geometry in place;
   - one `feature.set*` / `setCoordinates` per vessel per update, each firing change events and a re-render: batch updates,
     build features off-screen and `source.addFeatures` / `clear(true)` once, or swap the whole source; coalesce to one
     update per animation frame (`requestAnimationFrame`) instead of per message or per timer tick;
   - labels for everything: declutter, show labels only above a zoom and only for the visible extent, cap the count;
   - too many features on the canvas renderer: level of detail (clusters or a heat/tile layer when zoomed out), WebGL points
     layer / `WebGLVectorLayer` or vector-image layer for thousands of points, render only the viewport;
   - synchronous JSON parsing, geometry building or sorting of large payloads on the main thread: move it to a Web Worker
     (transfer ArrayBuffers), or `OffscreenCanvas` rendering in a worker;
   - request storms (one request per tile per refresh, hundreds per pan): debounce on `moveend`, fetch by viewport not by
     tile, cancel stale requests (`AbortController`), cache responses, back off polling while the user interacts;
   - websocket or polling handlers that update state per message: buffer and apply once per frame;
   - Vue shell: `markRaw` / `shallowRef` for map objects and large arrays (deep reactivity on thousands of vessels is a hidden
     per-change cost), no deep watchers on big collections, virtualised lists, no layout reads (`offsetWidth`,
     `getBoundingClientRect`) interleaved with writes (forced layout / thrash);
   - leaks: listeners, intervals and overlays added on each update and never removed (heap after GC and listener count grow).
4. **Targets are numbers.** Examples: "4x.pan.fps_p5 >= 45", "4x.long_task_max_ms <= 100 during pan", "4x.tbt_ms −50%",
   "4x.heap_growth_mb <= 5", "requests during pan −80%". Acceptance criteria quote them.
5. **Prove it.** Re-run the same scenario, then `relay-perf compare before/perf.json after/perf.json` and
   `relay-perf assert after/perf.json '<targets>' --baseline before/perf.json`. Paste the deltas and the new top functions.
   Relay re-measures at verification; a missing measurement, a missed target or a regression in a key metric fails
   verification and blocks delivery. Numbers from the headless container are pessimistic for GPU work (software WebGL):
   compare runs with each other, not with a real device.
