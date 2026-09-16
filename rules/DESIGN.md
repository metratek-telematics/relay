
## 1. Design principles

1. **Premium, not sparse.** Generous spacing, soft elevation, refined type — but the live‑monitoring surfaces (Operator, Camera detail, Berth approach) stay **information‑dense and glanceable**. Premium means *polished*, not *dumbed‑down*.
2. **Status is the loudest signal.** The state palette (OK / WARN / ALARM) is the single source of status truth and must always be the most prominent colour on screen. Never spend the alarm‑red hue on decoration.
3. **Maritime, calm, trustworthy.** Warm chart‑paper light theme, deep teal‑navy dark theme, a marine‑teal primary, and a rare brass accent. No neon, no glow.
4. **White‑label safe.** `--primary` is overridable at runtime via the `useBranding` hook (`PRODUCT_PRIMARY_HSL`); never assume the teal — always read the token.
5. **Avoid AI‑slop.** No cyan‑on‑black, no purple→blue gradients, no glassmorphism for its own sake, no identical icon‑topped card grids, no gradient text on metrics.

---

## 2. Themes

Two themes, toggled by the `.dark` class on `<html>` (persisted to storage; applied before first paint to avoid a flash). `color-scheme` is set so native controls match.

| | Light — **"Daylight Quay"** (default) | Dark — **"Night Bridge"** |
|---|---|---|
| Mood | warm chart‑paper, navy ink, marine teal | deep teal‑navy, no neon glow |
| Canvas | `#f6f1e7` warm paper | `#0c1419` deep teal‑navy |
| Ink / text | `#142338` navy | `190 24% 92%` near‑white, teal‑tinted |
| Primary | `#0a6e78` deep marine teal | `#3fc2cf` brighter teal (not neon) |

Both palettes are defined as HSL **triplets** (`H S% L%`) so opacity modifiers work: `hsl(var(--primary) / 0.22)`.

---

## 3. Colour tokens

Colours are authored as raw HSL triplets, then aliased to the shadcn names that components consume. Use the **alias** (`bg-card`, `text-muted-foreground`, `border-border`, …), not the raw token.

### Neutrals (surfaces & text)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--surface` → `--background` | `42 32% 96%` | `205 36% 8%` | app canvas |
| `--surface-container-lowest` → `--card` (light) | `0 0% 100%` | — | cards on light |
| `--surface-container` → `--card` (dark) | `42 30% 95%` | `204 24% 13%` | cards on dark |
| `--surface-container-low/high/highest` | warm steps | teal‑navy steps | layered panels, hovers |
| `--on-surface` → `--foreground` | `214 44% 14%` | `190 24% 92%` | primary text |
| `--on-surface-variant` → `--muted-foreground` | `214 16% 38%` | `195 14% 70%` | secondary text |
| `--outline-variant` → `--border` / `--input` | `40 20% 85%` | `202 16% 22%` | hairlines, inputs |

### Brand & accent

| Token | Light | Dark | Use |
|---|---|---|---|
| `--primary` | `190 84% 26%` (#0a6e78) | `186 62% 52%` (#3fc2cf) | CTAs, active nav, links, focus ring |
| `--primary-container` | `188 60% 88%` | `190 70% 30%` | tonal primary fills |
| `--accent-brand` | `38 58% 46%` (#b98637 brass) | `38 64% 58%` | the logo mark, chart ticks — **rare** metallic accent, never a gradient |

### State palette — the status truth

Reserved for live status only; deliberately off the teal hue so status never blends into brand.

| Token | Light | Dark | Meaning |
|---|---|---|---|
| `--state-ok` / `text-ok` `bg-ok` | `150 56% 34%` sea green | `152 56% 48%` | within tolerance |
| `--state-warn` / `text-warn` | `34 92% 44%` amber | `36 92% 58%` | approaching limit |
| `--state-alarm` / `text-alarm` | `4 76% 47%` signal red | `6 84% 66%` | breach |
| `--state-info` | `212 72% 42%` chart blue | `205 78% 60%` | neutral info |

**Tonal containers:** status pills/badges use the hue at low opacity over a tinted text — e.g. `bg-alarm/12 text-alarm`, `bg-ok/12 text-ok`. Critical states may pulse (`animate-pulse`).

---

## 4. Elevation

Soft, low‑opacity, **cool‑navy** tinted shadows (never neutral grey). Move from "borders only" to "subtle border **+** soft shadow."

`--shadow-xs … --shadow-xl` (mapped to Tailwind `shadow-xs…xl`). Light theme shadows are faint (5–16% opacity); dark theme shadows are deeper (45–70%) against the near‑black canvas. Cards default to a hairline border + `shadow-sm`; modals/popovers go `shadow-lg`/`xl`.

---

## 5. Typography

Three self‑hosted variable fonts (via `@fontsource-variable`, no CDN/FOUT). Loaded in [`src/main.tsx`](src/main.tsx).

| Role | Font | Tailwind / class | Notes |
|---|---|---|---|
| **Display / headings** | **Hanken Grotesk Variable** (sans) | `font-display` | page titles, hero, section headers. Headlines are **sans-serif** (product decision 2026-07); the heavier scale weights carry the hierarchy |
| **UI / body** | **Hanken Grotesk Variable** | default (`font-sans`) | all body, labels, nav, forms |
| **Numeric / machine** | **JetBrains Mono Variable** | `font-mono` | metrics, IDs, timestamps, RTSP URLs, coordinates — always `tabular-nums` |

### Type scale (`tailwind.config.ts` → `fontSize`)

| Token | Size / line | Weight | Use |
|---|---|---|---|
| `text-display` | clamp(2.75–5rem) | 560 | marketing hero only |
| `text-h1` | 2.25 / 2.5rem | 700 | top page title |
| `text-h2` | 1.75 / 2.125rem | 700 | page title (PageHeader) |
| `text-h4` | — | 600 | section header |
| `text-metric-xl` / `-lg` | 42 / 28px mono | 600 | hero KPI numbers |
| `text-body-sm` / `text-label-xs` | 14 / 11px | — | body / eyebrow labels |

**Rules:** headings → `font-display` (sans-serif). Metrics/IDs → `font-mono` + `tabular-nums`. Eyebrow labels → uppercase, `tracking‑[0.14em]`, `text-primary`. Don't use mono as a lazy "technical" vibe outside genuine machine values.

---

## 6. Shape & spacing

- **Radius:** base `--radius: 0.5rem`. Cards `rounded-xl`, controls `rounded-lg`, pills/badges `rounded-full`.
- **Spacing rhythm:** vary it — tight groupings inside a card, generous separation between sections. Don't pad everything identically.
- **Don't wrap everything in a card,** and never nest a card inside a card — flatten the hierarchy.

---

## 7. Component conventions

- **PageHeader** — eyebrow (`Camera / #1`) + `font-display` title + optional actions slot + metric chip strip. Wraps every screen for one cohesive product.
- **Card** — hairline `border-border` + `bg-card` + `shadow-sm`; optional accent top‑border for status.
- **StatusPill / Badge** — tonal `bg-{state}/12 text-{state}`, the universal status chip.
- **Buttons** — `variant` set: `brand` (gradient‑free primary), `outline`, `ghost`, `destructive`. One primary action per view; everything else ghost/outline. Hierarchy matters.
- **Metric** — `font-mono` value + small muted label; animate counters on change; never gradient‑text a metric.
- **Empty states** teach the interface, they don't just say "nothing here."
- **Motion** (framer‑motion) — high‑impact moments (page enter, KPI counters), exponential ease‑out, **gated behind `prefers-reduced-motion`**. Animate transform/opacity only.

---

## 8. White‑label & accessibility

- `--primary` (and `PRODUCT_NAME` / logo) are overridable per tenant; all primary‑accented UI must read the token, not the literal teal.
- Both themes target WCAG AA contrast; tint neutrals toward the surface hue rather than using grey text on colour.
- Never pure `#000` / `#fff` — always the tinted surface/ink tokens.
- Keyboard focus uses `--ring` (the primary hue); keep visible focus rings.

---

## 9. Quick reference — "use this, not that"

| Need | Use |
|---|---|
| App background | `bg-background` |
| A panel | `bg-card border-border shadow-sm rounded-xl` |
| Primary action | `<Button variant="brand">` |
| Status good/warn/bad | `text-ok` / `text-warn` / `text-alarm` (+ `bg-*/12` pill) |
| A number / ID | `font-mono tabular-nums` |
| A page title | `<PageHeader>` (font-display sans) |
| Secondary text | `text-muted-foreground` |
| Hairline | `border-border` |
