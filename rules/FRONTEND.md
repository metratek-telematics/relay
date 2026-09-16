# Professional Frontend / UI/UX Rules

Visible product work must look intentional, domain-specific, and production-grade.

## Product language first
Before designing:
- inspect existing navigation;
- inspect sibling screens;
- inspect theme/tokens;
- inspect typography;
- inspect spacing scale;
- inspect component library;
- inspect information density;
- inspect desktop/mobile conventions;
- inspect status and alert patterns.

Do not invent an unrelated design system.

## Information architecture
The most important operational information must be:
- easiest to find;
- easiest to scan;
- closest to the action it affects.

Group by user task/domain meaning, not by database origin.

Avoid:
- dumping fields into arbitrary cards;
- excessive nesting;
- equal visual emphasis for everything;
- decorative UI with no operational value.

## Hierarchy
Use hierarchy deliberately through:
- typography;
- spacing;
- grouping;
- alignment;
- contrast;
- density.

Do not rely on random colors, gradients, shadows, or oversized cards to create hierarchy.

## Domain-specific design
The interface should reflect the actual product domain.
For operational/maritime/industrial software:
- favor clarity, state, precision and situational awareness;
- keep key measurements and statuses scannable;
- preserve map/chart/data visibility;
- avoid consumer-social or generic SaaS aesthetics.

## Responsive behavior
Validate meaningful layouts at:
- 1920x1080;
- 1440x900;
- 1366x768;
- 1280x720.

If supported:
- 1024;
- 768;
- 430;
- 390.

At every supported size:
- no page-level horizontal overflow;
- no clipped primary actions;
- no hidden critical values;
- long IDs/names handled intentionally;
- tables may scroll inside their own container;
- fixed/sticky UI must not obscure content.

## States
Implement relevant:
- initial;
- loading;
- refreshing;
- empty;
- no results;
- error;
- partial data;
- stale data;
- permission denied;
- offline/disconnected;
- disabled;
- read-only;
- validation;
- destructive confirmation;
- success feedback.

Never render invalid/unknown data as a plausible real measurement.

Zero is data. Zero is not "missing".

## Accessibility
Aim for WCAG 2.2 AA:
- semantic structure;
- proper labels;
- keyboard operation;
- visible focus;
- logical tab order;
- focus restoration after dialogs;
- accessible icon buttons;
- sufficient contrast;
- non-color-only state cues;
- useful alt text;
- reduced-motion respect where applicable.

## Interaction
Actions must communicate:
- affordance;
- enabled/disabled state;
- progress;
- success/failure.

Prevent duplicate submissions.
Do not trap the user in modal flows unnecessarily.
Use confirmation only for meaningful irreversible/destructive actions.

## Motion
Motion should clarify state or spatial relationship.
Avoid:
- decorative bouncing;
- excessive transitions;
- animation that slows repeated operational work.

## No AI-slop
Reject unless product conventions require it:
- purple/blue gradient hero areas;
- floating decorative blobs;
- sparkles;
- "Welcome back 👋";
- giant slogans;
- card-inside-card-inside-card layouts;
- arbitrary KPI tiles;
- gratuitous glassmorphism;
- pill-shaped everything;
- emoji controls;
- excessive rounded corners;
- random shadows;
- fake "premium" styling;
- generic startup-dashboard design.

If the page could be dropped unchanged into 100 unrelated SaaS products, it is not sufficiently product-specific.

## Visual verification
For meaningful frontend changes:
- run the app if feasible;
- inspect browser console;
- inspect network failures;
- verify interaction;
- verify responsive behavior;
- inspect light/dark theme if both exist;
- verify keyboard focus;
- verify overlays, dialogs, maps and charts do not overlap incorrectly.

Compilation alone is not frontend verification.
