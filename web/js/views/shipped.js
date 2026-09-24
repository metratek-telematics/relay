// Shipped: how often a task actually reaches production without a person rescuing it (#65).
// Every number here comes from GET /api/shipped (orchestrator/shipped.py), which computes it from merge,
// deployment and commit facts — never from Relay's own report of how it went. Each row shows the facts
// behind its verdict, so any figure on this page can be traced to them.
import { $, $$, esc, icon, timeAgo, skeleton, toast } from "../ui.js";
import { api } from "../api.js";

const pct = (x) => (x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`);
const tone = (x, tooFew) => (tooFew || x === null || x === undefined ? "" : x >= 0.9 ? "green" : x >= 0.6 ? "amber" : "red");
const weekLabel = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
const FEW = "Too few to mean anything";

const factRow = (label, value, detail = "") =>
  `<div class="sh-fact"><span>${esc(label)}</span><b class="${value === true ? "ok" : value === false ? "bad" : "unk"}">${
    value === true ? "yes" : value === false ? "no" : "not known"}</b><i>${esc(detail)}</i></div>`;

function groupRows(rows, min, empty) {
  if (!rows || !rows.length) return `<div class="chart-empty">${esc(empty)}</div>`;
  return `<div class="lx-rows">${rows.map((r) => `<div class="lx-row" title="${esc(r.key)}">
      <span class="truncate">${esc(r.label)}</span>
      <span class="lx-track" aria-hidden="true"><i class="${tone(r.rate, r.too_few)}" style="width:${Math.max(2, (r.rate || 0) * 100)}%"></i></span>
      <span class="lx-v">${r.too_few ? `<span class="sh-few">${FEW}</span>` : `<b>${pct(r.rate)}</b>`}
        <span class="muted">· ${r.shipped} of ${r.known} known${r.unknown ? ` · ${r.unknown} unclear` : ""} · n ${r.n}</span></span></div>`).join("")}</div>`;
}

function weeks(series, min) {
  const shown = series.slice(-12);
  const cols = shown.map((w) => {
    const h = w.rate === null ? 0 : Math.max(3, Math.round(w.rate * 100));
    const title = `${weekLabel(w.week)}: ${w.n} finished, ${w.shipped} shipped, ${w.rescued} rescued, ${w.unknown} unclear`;
    return `<div class="lx-wk" title="${esc(title)}">
      <span class="lx-wk-v">${w.known ? (w.too_few ? `${w.shipped}/${w.known}` : pct(w.rate)) : ""}</span>
      <span class="lx-wk-col"><i class="${tone(w.rate, w.too_few)}" style="height:${h}%"></i></span>
      <span class="lx-wk-v">${esc(weekLabel(w.week))}</span></div>`;
  }).join("");
  return `<div class="lx-weeks" style="grid-template-columns:repeat(${shown.length || 1},minmax(0,1fr))">${cols}</div>`;
}

function failureRow(r) {
  const f = r.facts || {};
  return `<details class="sh-fail"><summary>
      <span class="sh-dot bad"></span>
      <span class="truncate">${r.number ? `#${r.number} ` : ""}${esc(r.name || r.task_id)}</span>
      <span class="muted truncate">${esc(r.repo_label || "")}</span>
      <span class="sh-why truncate">${esc((r.reasons || []).map((x) => x.text).join(" · "))}</span>
      <time>${esc(timeAgo(r.finished_at))}</time></summary>
    <div class="sh-facts">
      ${factRow("Branch cut from current code", f.base_current, f.base_behind ? `${f.base_behind} commits behind ${f.base_ref || "the base branch"}` : "")}
      ${factRow("Pull request merged", f.merged, f.merge_state || "")}
      ${factRow("Merged without conflict", f.merge_clean, "")}
      ${factRow("A person changed it afterwards", f.human_touched, [
        f.human_branch_commits ? `${f.human_branch_commits} commit(s) on the branch` : "",
        f.human_commits_after_merge ? `${f.human_commits_after_merge} commit(s) on the base branch` : "",
        (f.human_files || []).join(", "), f.human_touched_scope ? `checked: ${f.human_touched_scope}` : ""].filter(Boolean).join(" · "))}
      <div class="sh-fact"><span>Review rounds</span><b class="unk">${f.review_rounds ?? "—"}</b><i></i></div>
      <div class="sh-fact"><span>Deployment</span><b class="${f.deploy_state === "succeeded" ? "ok" : f.deploy_state === "failed" ? "bad" : "unk"}">${esc(f.deploy_state || "unknown")}</b>
        <i>${f.deploy_state === "none_configured" ? "this repository has no deployment target, so merging is where it ships" : ""}</i></div>
      ${factRow("Reverted", f.reverted, f.reverted_within_day ? "within a day of merging" : "")}
      <div class="sh-links">
        <a class="btn sm" href="#/task/${encodeURIComponent(r.task_id)}">${icon("layers", "sm")}Task</a>
        ${r.pr_url ? `<a class="btn sm" href="${esc(r.pr_url)}" target="_blank" rel="noopener">${icon("github", "sm")}Pull request</a>` : ""}
      </div>
    </div></details>`;
}

export function mountShipped(main) {
  main.innerHTML = `<div class="page lx-page sh-page" id="shPage">${skeleton("page", 3)}</div>`;
  const page = $("#shPage", main);
  let data = null, alive = true, busy = false;

  async function load() {
    if (busy) return;
    busy = true;
    try { data = await api.shipped(); if (alive) draw(); }
    catch (e) { if (alive) page.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; }
    finally { busy = false; }
  }

  function draw() {
    const d = data || {};
    const min = d.min_sample || 10;
    const card = d.scorecard || {};
    const headline = d.too_few
      ? `<b class="sh-few-big">${FEW}</b><span>${d.shipped} of ${d.known} known runs shipped · under ${min} is noise</span>`
      : `<b class="${tone(d.rate, false)}">${pct(d.rate)}</b><span>${d.shipped} of ${d.known} known runs shipped</span>`;
    page.innerHTML = `
      <header class="page-head">
        <div><h1>Shipped</h1>
          <p>A run counts only when it <b>merged</b>, <b>reached production</b> and <b>nobody had to touch it afterwards</b>.
             Every figure comes from merge, deployment and commit facts; what cannot be told is counted as unclear, not as a win.</p></div>
        <button class="btn sm" id="shRefresh">${icon("refresh", "sm")}Recompute</button>
      </header>

      <p class="sh-def">A run counts only when it <b>merged</b>, <b>reached production</b> and <b>nobody had to touch it afterwards</b>.
         Every figure comes from merge, deployment and commit facts; what cannot be told is counted as unclear, never as a win.</p>

      <div class="lx-kpis">
        <div class="lx-kpi sh-head">${headline}</div>
        <div class="lx-kpi"><b>${d.rescued ?? 0}</b><span>had to be rescued</span></div>
        <div class="lx-kpi"><b>${d.unknown ?? 0}</b><span>cannot be told${(d.unknown_facts || []).length ? `: ${esc(d.unknown_facts.map((u) => u.fact).join(", "))}` : ""}</span></div>
        <div class="lx-kpi"><b>${card.rate === null || card.rate === undefined ? "—" : pct(card.rate)}</b><span>what the old scorecard claims (${card.success ?? 0} of ${card.n ?? 0})</span></div>
      </div>

      <section class="card"><div class="card-head"><h2>Over time</h2>
        <span class="card-sub">By the week a task finished · a bar with fewer than ${min} known runs shows the count instead of a rate</span></div>
        <div class="card-body">${weeks(d.weeks || [], min)}</div></section>

      <div class="lx-grid">
        <section class="card"><div class="card-head"><h2>By repository</h2></div>
          <div class="card-body">${groupRows(d.by_repo, min, "No finished task has a repository recorded.")}</div></section>
        <section class="card"><div class="card-head"><h2>By team preset</h2></div>
          <div class="card-body">${groupRows(d.by_preset, min, "No finished task has a team recorded.")}</div></section>
        <section class="card"><div class="card-head"><h2>By agent</h2>
          <span class="card-sub">A task counts for every agent on its team</span></div>
          <div class="card-body">${groupRows(d.by_agent, min, "No finished task has an agent recorded.")}</div></section>
        <section class="card"><div class="card-head"><h2>Why they did not ship</h2></div>
          <div class="card-body">${(d.causes || []).length ? `<div class="lx-rows">${(d.causes || []).map((c) => `<div class="lx-row">
              <span class="truncate">${esc(c.label)}</span>
              <span class="lx-track" aria-hidden="true"><i class="red" style="width:${Math.round((c.count / Math.max(1, d.rescued || 1)) * 100)}%"></i></span>
              <span class="lx-v"><b>${c.count}</b> <span class="muted">run(s)</span></span></div>`).join("")}</div>`
            : '<div class="chart-empty">Nothing has failed to ship yet.</div>'}</div></section>
      </div>

      <section class="card"><div class="card-head"><h2>The ones that did not ship</h2>
        <span class="card-sub">Newest first · open one to see the facts behind the verdict</span></div>
        <div class="card-body sh-fails">${(d.failures || []).length ? (d.failures || []).map(failureRow).join("")
          : '<div class="chart-empty">Nothing needed rescuing.</div>'}</div></section>`;
    $("#shRefresh", page).onclick = async (e) => {
      e.target.disabled = true;
      try { data = await api.shippedBackfill(); draw(); toast("success", "Recomputed", `${data.rebuilt} run(s) rebuilt from the facts.`); }
      catch (err) { toast("error", "Could not recompute", err.message); }
      finally { const b = $("#shRefresh", page); if (b) b.disabled = false; }
    };
  }

  load();
  return { update: (reason) => { if (reason === "task" || reason === "learning") load(); }, destroy: () => { alive = false; } };
}
