// Mockups view: the design directions the team explored before building, the internal focus group's scores, and the
// choice Relay made (orchestrator/exploration.py). Informational: the owner can switch direction with one click, and
// only a real coin flip (or "Show me mockups before building") ever waits for them.
import { $, $$, esc, icon, toast, modal, fmtCost, fmtNum } from "../ui.js";
import { api } from "../api.js";

export const CRITERIA = [["clarity", "Clarity and hierarchy"], ["efficiency", "Task efficiency"], ["accessibility", "Accessibility"], ["consistency", "Design system fit"], ["research_fit", "Research fit"], ["feasibility", "Feasibility"]];
const STATUS = { drawing: "The designer is drawing the directions", rendering: "Rendering desktop and phone screenshots, light and dark", reviewing: "The focus group is scoring the directions", deciding: "Weighing the scores", waiting: "Waiting for your pick", chosen: "Direction chosen", skipped: "Exploration skipped", failed: "Exploration failed" };
const BY = { panel: "by the focus group, without asking you", panel_timeout: "by the focus group (no answer in time)", owner: "by you", owner_override: "by you (changed after the panel's pick)" };
const pref = (k, d) => { try { return localStorage.getItem(`relay.mockups.${k}`) || d; } catch { return d; } };
const setPref = (k, v) => { try { localStorage.setItem(`relay.mockups.${k}`, v); } catch {} };
const num = (v, d = 1) => (v == null || v === "" ? "–" : Number(v).toFixed(d));

export const mockupUrl = (tid, rel) => `/api/tasks/${encodeURIComponent(tid)}/mockups/file/${String(rel).split("/").map(encodeURIComponent).join("/")}`;

/** A small screenshot of the direction being explored or chosen (Mission Control's live card). */
export function mockupThumb(t) {
  const ex = t?.exploration;
  if (!ex || !(ex.directions || []).length) return null;
  const d = ex.directions.find((x) => x.id === ex.chosen) || ex.directions.find((x) => x.id === ex.panel_pick) || ex.directions.find((x) => x.shots && x.shots["desktop-light"]);
  const rel = d?.shots?.["desktop-light"];
  return rel ? { url: mockupUrl(t.id, rel), id: d.id, title: d.title || "" } : null;
}

export function mountMockups(host, getTask) {
  const tid = getTask()?.id;
  let data = null, key = "", alive = true;
  let device = pref("device", "desktop"), theme = pref("theme", "light");
  host.innerHTML = `<div class="mk" id="mk" aria-busy="true"><div class="mk-skel"><span class="skel" style="width:40%;height:18px"></span><div class="mk-grid">${'<div class="skel-block mk-skel-card"></div>'.repeat(3)}</div></div></div>`;
  const root = () => $("#mk", host);

  async function load() {
    try { data = await api.mockups(tid); } catch (e) { if (alive) root().innerHTML = `<div class="empty-state compact">${icon("alert", "lg")}<h3>Could not load the mockups</h3><p>${esc(e.message)}</p></div>`; return; }
    if (alive) draw();
  }
  const stateKey = (t) => { const ex = t?.exploration || {}; return JSON.stringify([ex.status, ex.chosen, ex.rendered, (ex.directions || []).length, (ex.reviews || []).length, t?.pending?.kind]); };

  function shotRel(d) { return d.shots?.[`${device}-${theme}`] || d.shots?.[`${device}-light`] || d.shots?.["desktop-light"] || ""; }

  function draw() {
    const t = getTask();
    const ex = data?.exploration || {};
    const dirs = ex.directions || [];
    const r = root();
    r.removeAttribute("aria-busy");
    if (!dirs.length) {
      const busy = ["drawing", "rendering", "reviewing", "deciding"].includes(ex.status);
      r.innerHTML = `<div class="empty-state">${busy ? `<div class="es-art" aria-hidden="true">${icon("spinner", "lg")}</div>` : icon("layers", "lg")}<h3>${esc(busy ? STATUS[ex.status] : ex.status === "skipped" ? "Exploration skipped" : "No design directions")}</h3><p>${esc(ex.skipped || (busy ? "Mockups appear here as soon as they are drawn." : "Design and layout tasks explore 2 or 3 directions with an internal focus group before building."))}</p></div>`;
      return;
    }
    const pending = t?.pending?.kind === "design_pick";
    const chosen = ex.chosen || "";
    const chosenRow = dirs.find((d) => d.id === chosen);
    const reviews = ex.reviews || [];
    const cost = ex.cost || {};
    const spent = (k) => cost[k] ? `${fmtCost(cost[k].cost_usd, true)} · ${fmtNum((cost[k].input || 0) + (cost[k].output || 0))} tokens` : "";
    const ranked = [...dirs].sort((a, b) => (a.rank || 9) - (b.rank || 9));
    const busy = !chosen && !pending && ex.status !== "skipped";
    r.innerHTML = `
      <section class="mk-summary ${pending ? "is-pending" : ""}" aria-labelledby="mkSumH">
        <div class="mk-sum-main">
          <p class="eyebrow">${dirs.length} directions explored${reviews.length ? ` · ${reviews.length} persona${reviews.length === 1 ? "" : "s"} scored them` : ""}</p>
          <h2 id="mkSumH">${chosenRow ? `Building <span class="mk-letter">${esc(chosen)}</span> ${esc(chosenRow.title || "")}` : pending ? "Pick a direction" : esc(STATUS[ex.status] || "Exploring")}</h2>
          ${chosenRow ? `<p class="mk-by">${icon(ex.chosen_by?.startsWith("owner") ? "user" : "sparkles", "sm")}<span>Chosen ${esc(BY[ex.chosen_by] || "by the focus group")}${ex.panel_pick && ex.panel_pick !== chosen ? ` · the panel preferred ${esc(ex.panel_pick)}` : ""}</span></p>` : ""}
          ${pending ? `<p class="mk-by">${icon("clock", "sm")}<span>${esc(t.pending.exploration?.reason || "Relay waits for you")}${t.pending.exploration?.timeout_minutes ? `. Without an answer Relay builds ${esc(t.pending.exploration.winner)} after ${esc(Math.round(t.pending.exploration.timeout_minutes))} minutes.` : "."}</span></p>` : ""}
          ${ex.rationale ? `<p class="mk-why">${esc(ex.rationale)}</p>` : ""}
          ${(ex.hybrid || []).length && chosen ? `<ul class="mk-hybrid" aria-label="Borrowed from other directions">${ex.hybrid.map((h) => `<li>${icon("layers", "sm")}<span>From <b>${esc(h.from)}</b>: ${esc(String(h.what || "").replace(/\.$/, ""))}</span></li>`).join("")}</ul>` : ""}
          ${ex.owner_note ? `<p class="mk-why"><b>Your note:</b> ${esc(ex.owner_note)}</p>` : ""}
        </div>
        <dl class="mk-facts">
          ${ex.decision ? `<div><dt>Decision</dt><dd>${esc(ex.decision)}</dd></div>` : ""}
          ${ex.panel ? `<div><dt>Panel</dt><dd>${esc(ex.panel.agent || "")} · ${esc(ex.panel.mode === "lean" ? "one turn for the panel" : "one turn per persona")} · ${esc(ex.panel.images === "html" ? "read the HTML" : "looked at the screenshots")}</dd></div>` : ""}
          ${spent("focus_group") ? `<div><dt>Focus group cost</dt><dd class="mono">${esc(spent("focus_group"))}</dd></div>` : ""}
          ${spent("mockups") ? `<div><dt>Mockups cost</dt><dd class="mono">${esc(spent("mockups"))}</dd></div>` : ""}
        </dl>
      </section>
      <div class="mk-toolbar" role="toolbar" aria-label="Screenshot options">
        <div class="seg" role="group" aria-label="Device">${[["desktop", "Desktop", "monitor"], ["phone", "Phone", "phone"]].map(([k, l, i]) => `<button type="button" data-device="${k}" aria-pressed="${device === k}" class="${device === k ? "active" : ""}">${icon(i, "sm")}${l}</button>`).join("")}</div>
        <div class="seg" role="group" aria-label="Theme">${[["light", "Light", "sun"], ["dark", "Dark", "moon"]].map(([k, l, i]) => `<button type="button" data-theme-pick="${k}" aria-pressed="${theme === k}" class="${theme === k ? "active" : ""}">${icon(i, "sm")}${l}</button>`).join("")}</div>
        ${busy ? `<span class="mk-busy" role="status">${icon("spinner", "sm")}${esc(STATUS[ex.status] || "Working")}</span>` : ""}
      </div>
      <ul class="mk-grid ${device === "phone" ? "is-phone" : ""}" aria-label="Design directions">${ranked.map((d) => card(d, { chosen, pending, panelPick: ex.panel_pick, voters: reviews.length })).join("")}</ul>
      ${reviews.length ? scoresTable(dirs, reviews, chosen || ex.panel_pick) : ""}
      ${reviews.length ? `<section class="mk-critique" aria-labelledby="mkCritH"><h3 id="mkCritH">What each persona said</h3>${reviews.map((rv) => persona(rv, dirs)).join("")}</section>` : ""}`;
    bind();
  }

  function card(d, { chosen, pending, panelPick, voters }) {
    const rel = shotRel(d);
    const isChosen = d.id === chosen;
    const live = data?.view_base ? `${data.view_base}${encodeURIComponent(d.id)}/index.html` : "";
    return `<li class="mk-card ${isChosen ? "is-chosen" : ""}" data-dir="${esc(d.id)}">
      <button type="button" class="mk-thumb" data-zoom="${esc(d.id)}" ${rel ? "" : "disabled"} aria-label="Enlarge direction ${esc(d.id)} (${device}, ${theme})">
        ${rel ? `<img src="${esc(mockupUrl(tid, rel))}" alt="Direction ${esc(d.id)}, ${esc(d.title || "")}: ${device} screenshot, ${theme} theme" loading="lazy" decoding="async">` : `<span class="mk-noshot">${icon("image", "lg")}<span>No screenshot</span></span>`}
      </button>
      <div class="mk-card-body">
        <div class="mk-card-head"><span class="mk-letter" aria-hidden="true">${esc(d.id)}</span><h3><span class="sr-only">Direction ${esc(d.id)}: </span>${esc(d.title || `Direction ${d.id}`)}</h3>
          ${isChosen ? `<span class="badge green">${icon("check", "sm")}Building this</span>` : d.id === panelPick ? `<span class="badge accent">Panel pick</span>` : ""}</div>
        ${d.idea ? `<p class="mk-idea">${esc(d.idea)}</p>` : ""}
        ${d.mean != null ? `<div class="mk-score"><span class="mk-mean mono">${num(d.mean)}<small>/10</small></span><span class="mk-var mono" title="Variance across personas: how much they disagreed">± ${num(d.variance, 2)}</span><span class="mk-votes">${esc(d.votes || 0)} of ${esc(voters)} prefer it</span></div>` : ""}
        <div class="mk-actions">
          ${live ? `<a class="btn xs" href="${esc(live)}" target="_blank" rel="noopener noreferrer">${icon("external", "sm")}Open live<span class="sr-only"> mockup ${esc(d.id)} in a new tab</span></a>` : ""}
          ${pending ? `<button type="button" class="btn xs primary" data-pick="${esc(d.id)}">${icon("check", "sm")}Build ${esc(d.id)}</button>` : chosen && !isChosen ? `<button type="button" class="btn xs" data-pick="${esc(d.id)}">Use ${esc(d.id)} instead</button>` : ""}
        </div>
      </div>
    </li>`;
  }

  function scoresTable(dirs, reviews, lead) {
    const ids = dirs.map((d) => d.id);
    const cell = (v, d) => `<td class="mono ${d === lead ? "is-lead" : ""}">${num(v)}</td>`;
    return `<section class="mk-scores" aria-labelledby="mkScoresH">
      <h3 id="mkScoresH">Focus group scores</h3>
      <div class="mk-table-wrap" tabindex="0" role="region" aria-labelledby="mkScoresH">
      <table class="mk-table">
        <caption class="sr-only">Scores from 1 to 10 per persona and per criterion; the direction being built is marked.</caption>
        <thead><tr><th scope="col">Persona</th>${ids.map((d) => `<th scope="col" class="${d === lead ? "is-lead" : ""}">${esc(d)}${d === lead ? '<span class="sr-only"> (chosen)</span>' : ""}</th>`).join("")}<th scope="col">Prefers</th></tr></thead>
        <tbody>
          ${reviews.map((rv) => `<tr><th scope="row">${esc(rv.persona)}</th>${ids.map((d) => cell(rv.overall?.[d], d)).join("")}<td><span class="mk-letter sm">${esc(rv.preferred || "–")}</span></td></tr>`).join("")}
        </tbody>
        <tbody class="mk-crit">
          ${CRITERIA.map(([k, l]) => `<tr><th scope="row">${esc(l)}</th>${ids.map((d) => cell(dirs.find((x) => x.id === d)?.criteria?.[k], d)).join("")}<td></td></tr>`).join("")}
        </tbody>
        <tfoot>
          <tr><th scope="row">Mean</th>${ids.map((d) => `<td class="mono ${d === lead ? "is-lead" : ""}"><b>${num(dirs.find((x) => x.id === d)?.mean)}</b></td>`).join("")}<td></td></tr>
          <tr><th scope="row">Variance</th>${ids.map((d) => `<td class="mono ${d === lead ? "is-lead" : ""}">${num(dirs.find((x) => x.id === d)?.variance, 2)}</td>`).join("")}<td></td></tr>
        </tfoot>
      </table></div>
    </section>`;
  }

  function persona(rv, dirs) {
    const rows = dirs.filter((d) => rv.notes?.[d.id]).map((d) => {
      const n = rv.notes[d.id];
      return `<li><span class="mk-letter sm" aria-hidden="true">${esc(d.id)}</span><div><b class="sr-only">Direction ${esc(d.id)}: </b>${n.verdict ? `<p>${esc(n.verdict)}</p>` : ""}
        ${(n.strengths || []).length ? `<p class="mk-plus"><span>Works</span> ${esc(n.strengths.join("; "))}</p>` : ""}
        ${(n.problems || []).length ? `<p class="mk-minus"><span>Problems</span> ${esc(n.problems.join("; "))}</p>` : ""}</div></li>`;
    }).join("");
    return `<details class="mk-persona"><summary><span>${esc(rv.persona)}</span><span class="muted">prefers ${esc(rv.preferred || "–")}${(rv.borrow || []).length ? ` · would borrow from ${esc(rv.borrow.map((b) => `${b.from}: ${b.what}`).join("; "))}` : ""}</span></summary><ul>${rows}</ul></details>`;
  }

  function bind() {
    const r = root();
    $$("[data-device]", r).forEach((b) => (b.onclick = () => { device = b.dataset.device; setPref("device", device); draw(); $(`[data-device="${device}"]`, root())?.focus(); }));
    $$("[data-theme-pick]", r).forEach((b) => (b.onclick = () => { theme = b.dataset.themePick; setPref("theme", theme); draw(); $(`[data-theme-pick="${theme}"]`, root())?.focus(); }));
    $$("[data-zoom]", r).forEach((b) => (b.onclick = () => lightbox(b.dataset.zoom)));
    $$("[data-pick]", r).forEach((b) => (b.onclick = () => pick(b.dataset.pick, b)));
  }

  async function pick(d, btn) {
    if (btn) btn.disabled = true;
    try {
      const res = await api.chooseDirection(tid, d);
      toast(res.applied === "recorded" ? "warning" : "success", res.applied === "answer" ? `Building ${d}` : res.applied === "unchanged" ? `${d} is already being built` : res.applied === "recorded" ? `Recorded ${d}` : `Switched to ${d}`,
        res.note || (res.applied === "answer" ? "The team starts on your pick now." : res.applied === "unchanged" ? "" : "The worker and supervisor get the new direction at their next turn."));
      await load();
    } catch (e) { toast("error", "Could not change the direction", e.message); if (btn) btn.disabled = false; }
  }

  function lightbox(start) {
    const dirs = (data?.exploration?.directions || []).filter((d) => shotRel(d));
    let i = Math.max(0, dirs.findIndex((d) => d.id === start));
    const m = modal(`<div class="mk-lb" role="document"><header class="mk-lb-head"><h2 id="mkLbH"></h2><span class="spacer"></span>
        <button type="button" class="btn xs" data-lb="-1" aria-label="Previous direction">${icon("chevronLeft", "sm")}Previous</button>
        <button type="button" class="btn xs" data-lb="1" aria-label="Next direction">Next${icon("chevron", "sm")}</button>
        <button type="button" class="btn xs ghost icon" data-close aria-label="Close">${icon("x")}</button></header>
      <div class="mk-lb-img" tabindex="0" role="region" aria-labelledby="mkLbH"><img alt=""></div></div>`, { wide: true });
    m.body.classList.add("mk-lb-modal");
    m.body.setAttribute("aria-labelledby", "mkLbH");
    const show = () => {
      const d = dirs[i];
      $("#mkLbH", m.root).textContent = `${d.id} · ${d.title || ""} · ${device} · ${theme}`;
      const img = $("img", m.root);
      img.src = mockupUrl(tid, shotRel(d));
      img.alt = `Direction ${d.id}, ${d.title || ""}: ${device} screenshot, ${theme} theme`;
    };
    $$("[data-lb]", m.root).forEach((b) => (b.onclick = () => { i = (i + Number(b.dataset.lb) + dirs.length) % dirs.length; show(); }));
    m.root.addEventListener("keydown", (e) => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); i = (i + (e.key === "ArrowRight" ? 1 : -1) + dirs.length) % dirs.length; show(); } });
    show();
  }

  load();
  key = stateKey(getTask());
  return {
    update(reason) {
      if (reason !== "task") return;
      const k = stateKey(getTask());
      if (k !== key) { key = k; load(); }
    },
    refresh() { load(); },
    destroy() { alive = false; },
  };
}
