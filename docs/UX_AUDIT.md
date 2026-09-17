# Relay UX audit (before Mission Control)

Audited on build 14.1.0 (main at `6b46e78`) with a copy of real task data, at 1440 px and 420 px in both themes.
Screenshots of every page were taken before any change. This document records what a person meets today and why
the redesign moves things; the redesign itself is described at the end.

## 1. What a first-time visitor sees

- **Two navigation systems at once.** A 56 px icon rail with 10 unlabelled icons (home, tasks toggle, inbox,
  sunrise, bot, target, GitHub, folder, brain) plus 5 more at the bottom, and a 292 px task sidebar. Nothing says
  that "sunrise" is the Digest, "target" is Issues or "brain" is Lessons; the only labels are tooltips and a `G`
  chord pill.
- **An Autopilot bar above every page** that repeats "Needs you" and "Digest" links which are also rail icons, and a
  "Run queue" button that is also in the sidebar footer and on the Overview page header.
- **The Overview answers "how did it go" but not "what is happening now".** Six KPI tiles and four charts come first;
  running agents, what they are doing, and what waits for you are below the fold or on other pages.
- **The brand colour is also the primary action colour, the live-phase colour and the "Claude" agent colour.** Orange
  means four things, so status never stands out.

## 2. Duplication (the same thing in several places)

| Information / action | Where it appears today |
| --- | --- |
| Questions and approvals waiting for you | Needs you page, Digest "Needs you" card, Overview "Needs your attention" card, task composer, conversation question card, sidebar "Needs attention" group, rail badge, Autopilot bar count, toasts |
| Queue state and start/halt | Sidebar footer box, Autopilot bar, Overview header, command palette |
| Delivered work | Overview "Open pull requests", Digest "Delivered", GitHub inbox "Tasks from GitHub", sidebar "Recent" |
| Task lists | Sidebar (six filter chips), Issues board status badges, GitHub inbox, Digest "Next in the queue" |
| Spend | Overview KPI + chart + "Usage by agent", Digest KPI + "Usage and limits", Autopilot bar, task roster, task Overview stats, Sessions tab |
| Connectors | Settings → Connectors, repository environment dialog, new-task dialog |
| System map | Repositories → System map tab, new-task related repositories, Issues dialog |
| Try it / branch / PR | Task header button, header PR pill, "Try it" tab, "Delivered" card in the conversation, Overview Signals card |
| Digest vs Overview | Two pages summarising the same tasks over different time windows, with different KPI tiles |

## 3. Dead ends and weak information scent

- **Tasks rail icon** does not open a tasks page: it toggles the sidebar. `#/tasks` silently redirects to the most
  recently updated task.
- **Issues and GitHub inbox** are separate pages for one source (GitHub). Creating a task from an issue happens on
  one; watching the repository on the other.
- **Task page has 10 inspector tabs** (Overview, Try it, History, Timeline, Changes, Checks, Review, Repository,
  Logs, Sessions). At 1440 px only five are visible; the rest scroll off under a fade. Files, diffs and commits
  are split across Changes, History and Repository, and the diff only covers the primary repository.
- **The Overview tab is 14 stacked cards** (stats, summary, acceptance, scorecard, stack, time chart, repositories,
  team, autopilot, plan, signals, request, open buttons). Acceptance evidence and the scorecard, the two things a
  reviewer needs, sit halfway down a 440 px column.
- **Nothing shows the agent actually working.** The "typing" line at the end of the conversation is the only live
  signal; the file being edited, its diff and the terminal output are hidden in collapsed tool rows.
- **No review workflow.** A delivered multi-repository change has PR links per repository in three places, but no
  single screen that walks acceptance → diffs → verification → merge.
- **At 420 px** the task sidebar is a drawer that covers the page, the task header wraps into five rows, the
  roster takes a whole screen before the conversation, and the conversation tool bar wraps.
- **Command palette** only navigates and toggles; it cannot create a task from text.

## 4. Inconsistent components

- Three page header styles (Overview/Settings `page-head`, Repositories with actions below the subtitle, task `ws-head`).
- Four "status chip" styles: `badge` (uppercase), `status-pill`, `pr-pill`, sidebar `st` dot.
- KPI tiles differ between Overview and Digest (different labels for the same numbers).
- Empty states range from a centred icon + paragraph to a one-line grey sentence.
- Loading states: "Loading…" text in most places, skeletons in History and Repositories only.
- Focus ring uses the brand orange at 2 px on a warm background; contrast is borderline in the light theme.
- Buttons: `primary` orange is used for "New task", "Run", "Try it", "Clone repository" and "Rescan" on the same screens.

## 5. What the redesign does

1. **Five places, labelled**: Home (Mission Control), Work, Knowledge, Agents, Settings. The task sidebar and the
   Autopilot bar are gone; their contents live on Home and Work. Old URLs redirect.
2. **Home = Mission Control**: live agents with the current tool call, Needs-you answers inline, the queue lane with
   ETAs and drag reorder, today's deliveries with scores, success trend and spend. Digest is a time-range switch on
   the same screen.
3. **Work**: one board for tasks, GitHub issues and the queue, in columns Ideas → Queued → Running → Needs you →
   Review → Done, with filters and bulk actions.
4. **Task page**: header with status, team, repositories and PRs; a phase outline; the conversation or the live
   theatre in the middle; a collapsible context rail. Files, diffs, commits and "Try it" become one Changes view
   across repositories. A Review cockpit and a read-only shareable status view complete it.
5. **Knowledge**: System map, Lessons, Repositories and Connectors together.
6. **One design system**: the house palette (marine teal primary, warm paper light theme, deep teal-navy dark theme,
   state colours reserved for status), Hanken Grotesk and JetBrains Mono self-hosted, a type and spacing scale,
   elevation tokens, motion gated behind `prefers-reduced-motion`, and visible focus rings.
