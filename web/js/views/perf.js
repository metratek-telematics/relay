// Performance card: a performance task's baseline vs latest measurement (orchestrator/perfcheck.py, tools/perf.cjs),
// the targets and the comparison verdict, with links to the reports and the DevTools trace.
import { esc, icon, timeAgo } from "../ui.js";

// The metrics a person reads first; the full table is in the comparison report.
const ROWS = [
  ["fps_avg", "FPS avg", 1], ["fps_p5", "FPS p5", 1], ["jank_pct", "% late frames", -1], ["long_task_max_ms", "Longest task ms", -1],
  ["tbt_ms", "Blocking time ms", -1], ["main_busy_pct", "Main thread busy %", -1], ["scripting_ms", "Scripting ms", -1],
  ["heap_growth_mb", "Heap growth MB", -1], ["requests", "Requests", -1],
];

const fileUrl = (t, label, name) => `/api/tasks/${encodeURIComponent(t.id)}/perf/${encodeURIComponent(label)}/${encodeURIComponent(name)}`;
const fmt = (v) => (v === null || v === undefined ? "–" : Number.isInteger(v) ? String(v) : String(Math.round(v * 10) / 10));

function profileOf(summary) {
  // The most throttled profile is the one that shows the problem (older devices).
  const names = Object.keys(summary || {}).filter((k) => /^\d+(\.\d+)?x$/.test(k)).sort((a, b) => parseFloat(b) - parseFloat(a));
  return names[0] || null;
}

function verdictOf(p) {
  if (!p.baseline && !p.latest) return { text: p.reason ? "not measured" : "measuring", tone: p.reason ? "red" : "" };
  if (!p.latest) return { text: p.baseline?.ok ? "baseline recorded" : "baseline failed", tone: p.baseline?.ok ? "" : "red" };
  if (p.ok) return { text: p.verdict === "improved" ? "improved · targets met" : `targets met · ${p.verdict || "compared"}`, tone: "green" };
  return { text: p.verdict === "regressed" ? "regressed" : "targets missed", tone: "red" };
}

export function perfCardHtml(t) {
  const p = t.perf;
  if (!p) return "";
  const v = verdictOf(p);
  const b = p.baseline?.summary || {}, a = p.latest?.summary || {};
  const prof = profileOf(a) || profileOf(b);
  const rows = prof ? ROWS.filter(([k]) => (b[prof] || {})[k] !== undefined || (a[prof] || {})[k] !== undefined).map(([k, label, dir]) => {
    const x = (b[prof] || {})[k], y = (a[prof] || {})[k];
    const cmp = (p.compare?.rows || []).find((r) => r.metric === `${prof}.${k}`);
    const verdict = cmp ? cmp.verdict : "";
    const d = x !== undefined && y !== undefined && x !== null && y !== null ? y - x : null;
    return `<tr><td>${esc(label)}</td><td class="num">${fmt(x)}</td><td class="num">${p.latest ? fmt(y) : "…"}</td>
      <td class="num pf-d ${verdict === "improved" ? "good" : verdict === "regressed" ? "bad" : ""}">${d === null ? "" : `${d > 0 ? "+" : ""}${fmt(d)}${dir && verdict && verdict !== "unchanged" ? (verdict === "improved" ? " ▲" : " ▼") : ""}`}</td></tr>`;
  }).join("") : "";
  const targets = (p.asserts || (p.spec?.targets || []).map((expr) => ({ expr }))).map((r) => `<li class="${r.ok === true ? "ok" : r.ok === false ? "fail" : ""}">${icon(r.ok === true ? "check" : r.ok === false ? "x" : "clock")}<span class="truncate">${esc(r.expr)}</span>${r.value !== undefined ? `<small>${fmt(r.value)}${r.target !== undefined && r.target !== null ? ` / ${fmt(r.target)}` : ""}</small>` : ""}</li>`).join("");
  const top = (p.latest?.top || p.baseline?.top || []).slice(0, 3);
  const links = [];
  if (p.baseline?.label && p.baseline.ok !== undefined) links.push(`<a href="${fileUrl(t, p.baseline.label, "PERF_REPORT.md")}" target="_blank" rel="noopener">Baseline report</a>`);
  if (p.latest?.label) {
    links.push(`<a href="${fileUrl(t, p.latest.label, "PERF_REPORT.md")}" target="_blank" rel="noopener">Latest report</a>`);
    if (p.compare) links.push(`<a href="${fileUrl(t, p.latest.label, "COMPARE.md")}" target="_blank" rel="noopener">Comparison</a>`);
    if (prof) links.push(`<a href="${fileUrl(t, p.latest.label, `trace-${prof}.json.gz`)}" download title="Open in Chrome DevTools › Performance › Load profile">Trace ${esc(prof)}</a>`);
  }
  const lhLabel = p.latest?.label || p.baseline?.label;
  if (p.spec?.lighthouse && lhLabel) links.push(`<a href="${fileUrl(t, lhLabel, "lighthouse.report.html")}" target="_blank" rel="noopener">Lighthouse</a>`);
  return `<div class="card pf-card ${v.tone === "red" ? "fail" : v.tone === "green" ? "pass" : ""}" id="perfCard">
    <div class="card-head"><h3>${icon("gauge", "sm")}Performance</h3><span class="badge ${esc(v.tone)}">${esc(v.text)}</span></div>
    <div class="card-body stack">
      ${p.spec ? `<div class="muted pf-spec mono truncate" title="${esc(p.spec.url)}">${esc(p.spec.url)} · CPU ${esc(String(p.spec.throttle || "").split(",").map((x) => x + "×").join(" / "))}</div>` : ""}
      ${rows ? `<div class="tk-table-wrap"><table class="tk-table pf-table"><thead><tr><th>${esc(prof)} CPU</th><th class="num">Before</th><th class="num">After</th><th class="num">Δ</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      ${targets ? `<div><div class="pf-sub">Targets</div><ul class="pf-targets">${targets}</ul></div>` : ""}
      ${p.reason && !p.ok ? `<div class="pf-reason">${icon("alert", "sm")}<span>${esc(p.reason)}</span></div>` : ""}
      ${top.length ? `<div><div class="pf-sub">Hottest functions ${p.latest ? "now" : "at baseline"}</div><ul class="pf-top">${top.map((x) => `<li class="mono truncate" title="${esc(x)}">${esc(x)}</li>`).join("")}</ul></div>` : ""}
      <div class="row between pf-foot"><span class="row wrap pf-links">${links.join("")}</span>${p.time ? `<span class="muted">${esc(timeAgo(p.time))}</span>` : ""}</div>
    </div></div>`;
}
