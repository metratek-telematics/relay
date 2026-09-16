# Frontend / UI Rules

Visible product work must look intentional, domain-specific and production-grade. The visual language and the
design process are in `DESIGN.md`; this file covers structure, behaviour and quality floors.

## Learn the product first
Before designing, look at the navigation, sibling screens, existing components and how the app handles data
loading, errors and status. Reuse what works; replace what does not meet `DESIGN.md`.

## Information architecture
- The most important operational information is the easiest to find, scan and act on.
- Group by the user's task and domain meaning, not by database origin.
- Avoid dumping fields into arbitrary cards, equal emphasis for everything, and decoration with no operational value.
- For operational and maritime software favour clarity, state, precision and situational awareness; keep maps,
  charts and key measurements visible.

## Behaviour to preserve and handle
- Keep existing features, routes and data flows working unless the task removes them.
- Handle the states the data can actually be in: loading, empty, error, stale or partial. Design them; do not leave default text.
- Never show unknown data as a plausible measurement. Zero is data, not "missing".
- Prevent duplicate submissions; actions show progress and success or failure.

## Quality floor
- Works from phone width to wide desktop: no page-level horizontal scroll, no clipped primary actions,
  wide tables scroll inside their own container.
- Semantic structure, labelled controls, keyboard operable, visible focus, sufficient contrast, status not
  conveyed by colour alone, focus returns after dialogs, reduced motion respected.

These are floors to build in while designing, not a checklist to prove item by item.

## No AI-slop
Reject purple/blue gradient heroes, decorative blobs, sparkles, "Welcome back 👋", giant slogans,
card-in-card layouts, arbitrary KPI tiles, gratuitous glassmorphism, pill-shaped everything, emoji controls,
random shadows and generic startup-dashboard styling. If the page could be dropped unchanged into a hundred
unrelated SaaS products, it is not finished.
