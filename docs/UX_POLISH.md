# Interaction polish (Relay 15)

The app was feature-complete but felt abrupt: the navigation snapped between two layouts, pages blanked to
"Loading…" before they filled, and motion was inconsistent from screen to screen. This pass changes how the
app *feels*, not what it does. No backend change, no new features.

Everything below was measured on a throwaway container with a copy of real data, in Chromium via Playwright,
with 120 ms of latency added to every request (the app normally sits behind a proxy, and a local request that
returns in 2 ms hides every loading flash).

## 1. The navigation

**What it was.** Collapsing swapped two complete layouts in one frame: the column jumped 232 → 64 px, labels
went `display: none`, the brand and the collapse button re-stacked into a column, so every icon in the rail
moved 28 px down, and the counts jumped from the right of the row to the corner. Nothing animated.

**What it is now.** Two registered custom properties carry the whole change:

```css
@property --nav       { syntax: "<length>"; inherits: true; initial-value: 232px; }
@property --nav-label { syntax: "<number>"; inherits: true; initial-value: 1; }
html { transition: --nav var(--dur-panel) var(--ease-out), --nav-label 130ms linear; }
html.nav-rail { --nav: var(--rail); --nav-label: 0; }
```

`--nav` is the grid column of the shell; `--nav-label` fades, slides and squeezes the wording of every row
(`opacity`, `translateX`, `max-width`). One state class, one transition, and every rule in the navigation
reads the same two numbers, so the rail, the drawer and the expanded column can never disagree.

Details that make it feel deliberate:

- **Icons do not move at all.** Every row keeps `padding: 0 11px` in both states, so an icon is at the same
  x in the rail as in the expanded column (and at the exact centre of the 64 px rail). Measured: one single
  icon position across all 38 frames of a collapse, where it used to be two.
- **Labels never re-wrap.** They are `white-space: nowrap; overflow: hidden` and shrink by `max-width`.
- **Counts slide** to the corner instead of teleporting (they are positioned in both states).
- **The active marker** sits inside the row and scales in, rather than hanging outside a clipped box.
- **The collapse button** takes the brand's place in the rail, appearing on hover or keyboard focus.
- **Tooltips** on the rail are drawn in the body (the rail clips its own overflow) with the shortcut key.
- **No flash on load.** The inline script in `index.html` applies the remembered state *and* a `booting`
  class that suppresses the transition for the first two frames.
- **Keyboard**: `[` or `Ctrl/⌘ + \` toggles it; the button carries `aria-expanded`/`aria-controls` and its
  label changes with the state.
- **Tablet and phone**: at ≤ 1240 px the toggle opens the navigation as an overlay drawer with a scrim; at
  ≤ 760 px the navigation is off-canvas and the tab bar's "More" opens the same drawer instead of a short
  menu of leftovers. Escape, the scrim, a swipe to the left or following a link closes it; focus is trapped
  while it is open and returns to the opener when it closes.
- The Work board still folds the navigation to the rail so six columns fit — but only below 1560 px, and it
  animates like everything else instead of snapping.

## 2. Pages arriving

- Every view mounts with `view-enter` (160 ms fade + 4 px lift, transform and opacity only).
- `Loading…` text placeholders were replaced by skeletons with the shape and height of the content that
  replaces them (`skeleton("page" | "cards" | "list")` in `ui.js`): Agents, Knowledge, Lessons, Learning,
  GitHub, Tools, Connectors, Tokens, every workspace page, and all six Mission Control panels.
- Mission Control panels have a floor height, so the panel grid no longer resizes when data lands.
- Scroll position is remembered per route for the life of the tab and restored on the way back.

## 3. Motion, feedback, numbers

- One motion scale in `tokens.css`: `--dur-press: 90ms`, `--dur-enter: 160ms`, `--dur-panel: 200ms`,
  `--dur-exit: 130ms`, with `--ease-out: cubic-bezier(.2, .8, .2, 1)` as the house curve. Things leave
  faster than they arrive.
- Menus grow from the corner of the button that opened them and shrink away when they close; the palette
  and modals scale and fade in and out; toasts slide in and out on transform and opacity (they used to
  animate `all`).
- Press feedback (≤ 90 ms) on every button, chip, segment, navigation row, card and tab.
- Figures and clocks use `tabular-nums`, so a ticking timer or a changing count never nudges its neighbours.
- The notification list uses `content-visibility` with an intrinsic size, so off-screen rows cost no layout.
  It was tried on the conversation and the board too and removed again: with estimated heights the
  conversation no longer lands on the newest message (measured: `scrollHeight` 8884 → 8331 as rows render,
  and the view stopped 800 px short of the bottom).
- Touch targets are at least 36 px (44 px in the tab bar) wherever the pointer is coarse.
- Pausing or resuming Autopilot answers immediately and rolls back if the server refuses.
- `prefers-reduced-motion` switches all of it off, including the navigation transition.

## 4. Measurements

Chromium, 1440 × 900, `PerformanceObserver` for layout shift and long tasks, CDP `Performance.getMetrics`
for layout counts, and a per-frame sampler for geometry.

### Navigation collapse / expand

| | before | after |
| --- | --- | --- |
| Distinct widths during the change | 2 (a snap) | 11 (a 200 ms animation) |
| Icon positions during the change | x: 2, y: 2 (a 28 px jump) | x: 1, y: 1 (no movement) |
| Label states | `display: none` ↔ `block` | 11 interpolated opacities |
| Dropped frames (> 34 ms) | 0 | 0 |
| Layouts / layout time for the whole change | 1 / 1 ms | 14 / 14 ms |
| Collapsed state after reload | one width (64 px), no flash | unchanged |

The layout count rises because the column really does animate; at 14 ms of layout across 200 ms it costs
about 7 % of one core, and no frame was dropped. Layout shift during the toggle is ~0.23 in both versions —
it is the main column resizing, which the browser reports as a shift either way, and it is excluded from the
Core Web Vitals CLS because it follows a click (`hadRecentInput: true`). What changed is that it is now
spread over 11 frames instead of landing in one.

### Route changes (120 ms latency on every request)

| route | CLS before | CLS after | frames blank before | after |
| --- | --- | --- | --- | --- |
| `#/` Mission Control | 0.270 | 0.000 | skeleton, then a jump | skeleton, no jump |
| `#/agents` | 0 | 0 | 8 blank frames (~130 ms) | 0 (skeleton) |
| `#/knowledge/system` | 0.113 | 0.123 | 8 | 6 |
| `#/work` | 0.113 | 0.127 | 0 | 0 |
| `#/org/people` | 0 | 0 | 8 | 0 (skeleton) |
| `#/task/…` | 0 | 0 | 9 | 9 |

Long tasks: none over 50 ms in either version, on any route. Maximum frame gap on a route change: 34 ms
(one skipped frame) before and after.

The residual ~0.12 on `#/work` and on the way back out of it is the navigation folding to the rail for the
board — now an animation rather than a snap, but still a real reflow of the main column. Removing it
altogether would cost the sixth board column below 1560 px.

## 5. What is still open

- **SSE updates still redraw whole sections.** The Work board and Mission Control already keep scroll,
  focus, drafts and drag state across a redraw, and they throttle, but a card's DOM is rebuilt rather than
  patched. A DOM-morph helper would remove the last flicker; it needs the event binding in those views to
  move to delegation first, so it was left out of this pass.
- The copy of the data used for the measurements had no running task, so live-update behaviour was reviewed
  by reading the code, not measured.
- `#/task/…` still shows a skeleton for ~150 ms while its conversation loads; the shell is instant but the
  conversation is not yet streamed in behind the previous content.
- `#/org/people` takes ~500 ms to settle — that is the workspace API, not the front end.
