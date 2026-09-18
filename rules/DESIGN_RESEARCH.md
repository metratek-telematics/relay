# Design research

For a new design, redesign, new layout or visual refresh, research before you draw. The goal is a result that
follows current, proven practice, not the first idea.

1. Research, with web search when you have it (keep it short: about 10 minutes of reading, not a survey):
   - current UI/UX principles relevant to this screen: Nielsen's usability heuristics, Laws of UX (Fitts, Hick,
     Jakob, proximity, Miller), WCAG 2.2 AA (contrast, target size 24x24 px minimum, focus visible, reflow), and the
     platform guidelines that fit (Material 3, Apple HIG, or the product's own system);
   - how established products solve the same problem (maps, dashboards, tables, forms: whatever this is), and the
     conventions users will expect;
   - for data-dense or domain UIs (e.g. maritime/map symbology), the domain's own conventions.
2. Write `DESIGN_RESEARCH.md` in the run folder report (not in the repository unless asked): 5 to 10 principles you
   will apply, each with one line on how it shapes this design, plus 2 or 3 references.
3. Precedence: the repository's own design rules and tokens (DESIGN.md, FRONTEND.md, existing components, brand
   colours, fonts) always win. Research fills gaps and informs choices; it never overrides the house style.
4. Then design with intent: one clear layout, a visual hierarchy, consistent spacing and type scale, all states
   (empty, loading, error, long content), light and dark, keyboard and screen-reader access, and responsive widths.
5. In your report, show before/after screenshots and say which principles drove the main decisions.
