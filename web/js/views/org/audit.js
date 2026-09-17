// Audit log (admins): every change with who, when, what and before/after; filters, chain verification and CSV export.
import { $, $$, esc, icon, toast, timeAgo, fmtDateTime, debounce } from "../../ui.js";
import { ORG, loadMe, orgApi, can, xicon, readOnlyNote, currentProject } from "./org.js";

const TYPE_ICON = { task: "tasks", connector: "zap", settings: "settings", project: "grid", user: "users", token: "key", integration: "plug", repository: "folder", stack: "layers", auth: "lock", profile: "user", queue: "play", autopilot: "clock", lesson: "brain", audit: "scroll" };

export function mountAudit(body) {
  let alive = true, data = null, chain = null, open = new Set();
  const f = { q: "", actor: "", type: "", outcome: "", since: "", until: "", project: "", limit: 200 };

  async function load() {
    if (!ORG.me) await loadMe();
    if (!can("admin")) { body.innerHTML = `<div class="page-head"><div><h1>Audit log</h1></div></div>${readOnlyNote("admin", "Reading the audit log")}`; return; }
    try { data = await orgApi.audit(f); } catch (e) { body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (!alive) return;
    if (!body.querySelector("#auRows")) shell();
    rows();
  }

  function shell() {
    body.innerHTML = `
      <div class="page-head"><div><h1>Audit log</h1><p>Every change to Relay: who made it, from where, and what it changed. Secrets are masked before anything is written; entries are hash-chained so edits show up.</p></div>
        <div class="page-actions"><span id="auChain"></span><button class="btn sm" id="auVerify">${xicon("lock")}Verify chain</button><a class="btn sm" id="auCsv" href="#">${icon("download")}Export CSV</a></div></div>
      <div class="card"><div class="card-body org-filters">
        <div class="org-pop-search grow" style="margin:0">${icon("search", "sm")}<input id="auQ" placeholder="Search actions, objects, people, values…" autocomplete="off"></div>
        <select id="auActor" aria-label="Person"><option value="">Everyone</option></select>
        <select id="auType" aria-label="Object"><option value="">All objects</option></select>
        <div class="seg" id="auOutcome">${[["", "All"], ["ok", "OK"], ["denied", "Denied"], ["error", "Errors"]].map(([k, l]) => `<button data-o="${k}" class="${f.outcome === k ? "active" : ""}">${l}</button>`).join("")}</div>
        <input type="date" id="auSince" aria-label="From"><input type="date" id="auUntil" aria-label="Until">
        <label class="org-chip" title="Only the project chosen at the top left"><input type="checkbox" id="auProj">current project</label>
      </div></div>
      <div class="card"><div class="card-head"><h3 id="auCount">Entries</h3><span class="muted" style="font-size:12px">newest first · click a row for details</span></div><div class="card-body org-table-wrap" style="padding:4px 8px" id="auRows"></div></div>`;
    const reload = debounce(load, 250);
    $("#auQ", body).oninput = (e) => { f.q = e.target.value.trim(); reload(); };
    $("#auActor", body).onchange = (e) => { f.actor = e.target.value; load(); };
    $("#auType", body).onchange = (e) => { f.type = e.target.value; load(); };
    $$("#auOutcome button", body).forEach((b) => (b.onclick = () => { f.outcome = b.dataset.o; $$("#auOutcome button", body).forEach((x) => x.classList.toggle("active", x === b)); load(); }));
    $("#auSince", body).onchange = (e) => { f.since = e.target.value ? `${e.target.value}T00:00:00` : ""; load(); };
    $("#auUntil", body).onchange = (e) => { f.until = e.target.value ? `${e.target.value}T23:59:59+99` : ""; load(); };
    $("#auProj", body).onchange = (e) => { f.project = e.target.checked && currentProject() !== "all" ? currentProject() : ""; load(); };
    $("#auVerify", body).onclick = async () => { try { chain = await orgApi.auditVerify(); drawChain(); toast(chain.ok ? "success" : "error", chain.ok ? "Audit chain intact" : "Audit chain broken", chain.ok ? `${chain.entries} entries verified` : chain.reason); } catch (e) { toast("error", "Verify failed", e.message); } };
    $("#auCsv", body).onclick = (e) => { e.preventDefault(); location.href = orgApi.auditCsvUrl({ ...f, limit: undefined }); };
  }

  const drawChain = () => { const el = $("#auChain", body); if (el && chain) el.innerHTML = `<span class="org-chain ${chain.ok ? "ok" : "bad"}">${xicon(chain.ok ? "checkCircle" : "lock", "sm")}${chain.ok ? `${chain.entries} entries intact` : `broken at #${chain.broken_at}`}</span>`; };

  function rows() {
    const fill = (id, list, cur) => { const s = $(id, body); if (!s) return; const first = s.options[0].outerHTML; s.innerHTML = first + list.map((x) => `<option ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join(""); };
    fill("#auActor", data.facets.actors, f.actor);
    fill("#auType", data.facets.types, f.type);
    $("#auCount", body).textContent = `${data.total} entr${data.total === 1 ? "y" : "ies"}`;
    const list = data.entries;
    $("#auRows", body).innerHTML = list.length ? `<table class="org-table"><thead><tr><th>When</th><th>Who</th><th>Action</th><th class="org-hide-sm">Object</th><th>Outcome</th><th class="org-hide-sm">Via</th></tr></thead><tbody>
      ${list.map((e) => {
        const o = e.object || {};
        const main = `<tr class="clickable org-audit-row" data-id="${esc(e.id)}"><td title="${esc(fmtDateTime(e.time))}">${esc(timeAgo(e.time))}</td>
          <td><strong>${esc(e.actor.username)}</strong>${e.actor.role ? `<div class="muted" style="font-size:11px">${esc(e.actor.role)}</div>` : ""}</td>
          <td><span class="org-action">${esc(e.action)}</span>${e.changes?.length ? ` <span class="badge outline">${e.changes.length} change${e.changes.length === 1 ? "" : "s"}</span>` : ""}</td>
          <td class="org-hide-sm"><span class="row" style="gap:6px">${xicon(TYPE_ICON[o.type] || "info", "sm")}<span class="truncate" style="max-width:260px">${esc(o.name || o.id || o.type || "")}</span></span></td>
          <td><span class="org-outcome ${esc(e.outcome)}">${icon(e.outcome === "ok" ? "check" : e.outcome === "denied" ? "x" : "alert", "sm")}${esc(e.outcome)}${e.status ? ` · ${e.status}` : ""}</span></td>
          <td class="org-hide-sm muted">${esc(e.via || "")}</td></tr>`;
        if (!open.has(e.id)) return main;
        const ch = e.changes || [];
        return main + `<tr class="org-audit-detail"><td colspan="6"><div class="stack" style="gap:10px;padding:4px 0">
          ${e.detail ? `<div class="org-note ${e.outcome === "ok" ? "" : "warn"}">${esc(e.detail)}</div>` : ""}
          ${ch.length ? `<div class="org-diff"><div class="h">Field</div><div class="h">Before</div><div class="h">After</div>${ch.map((c) => `<div>${esc(c.field)}</div><div class="b">${c.before == null ? "—" : esc(c.before)}</div><div class="a">${c.after == null ? "—" : esc(c.after)}</div>`).join("")}</div>` : '<span class="muted">No field-level changes recorded for this action.</span>'}
          <dl class="kv" style="margin:0"><dt>Time</dt><dd>${esc(e.time)}</dd><dt>Request</dt><dd class="mono" style="font-size:11.5px">${esc(e.request ? `${e.request.method} ${e.request.path} from ${e.request.ip}` : "—")}</dd>
            ${e.request?.body ? `<dt>Body</dt><dd><pre class="org-code" style="max-height:200px">${esc(JSON.stringify(e.request.body, null, 2))}</pre></dd>` : ""}
            ${o.project ? `<dt>Project</dt><dd>${esc((ORG.projects.find((p) => p.id === o.project) || {}).name || o.project)}</dd>` : ""}
            <dt>Hash</dt><dd class="mono muted" style="font-size:11px;overflow-wrap:anywhere">${esc(e.hash)}</dd></dl></div></td></tr>`;
      }).join("")}</tbody></table>` : `<div class="org-empty" style="border:0"><span class="org-empty-art">${xicon("scroll")}</span><h2>No entries</h2><p>${f.q || f.actor || f.type || f.outcome ? "Nothing matches these filters." : "Changes appear here as people and tokens use Relay."}</p></div>`;
    $$("[data-id]", body).forEach((r) => (r.onclick = () => { open.has(r.dataset.id) ? open.delete(r.dataset.id) : open.add(r.dataset.id); rows(); }));
    drawChain();
  }

  load();
  return { destroy() { alive = false; } };
}
