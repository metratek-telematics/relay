// System map: components of the system, the dependencies between them (proposed by a code scan, approved by a person) and a graph.
import { $, $$, esc, icon, toast, confirm, modal, timeAgo } from "../ui.js";
import { navigate } from "../state.js";
import { api } from "../api.js";

const KINDS = ["frontend", "api", "worker", "database", "library", "infra"];
const KIND_TONE = { frontend: "blue", api: "purple", worker: "amber", database: "green", library: "", infra: "" };
const VIAS = ["http", "sql", "queue", "file", "lib"];

export function renderSystemMap(host, { isAlive = () => true } = {}) {
  let map = null;
  let error = null;
  const ui = { focus: "", busy: "" };

  async function load() {
    try { map = await api.system(); error = null; } catch (e) { error = e.message; }
    if (isAlive()) draw();
  }
  const comp = (id) => (map?.components || []).find((c) => c.id === id);
  const label = (id) => comp(id)?.name || id;

  function draw() {
    if (error) { host.innerHTML = `<div class="card"><div class="empty">${icon("alert", "lg")}<h3>Could not load the system map</h3><p>${esc(error)}</p></div></div>`; return; }
    if (!map) { host.innerHTML = '<div class="card"><div class="card-body"><span class="skel" style="width:100%;height:160px;display:block"></span></div></div>'; return; }
    const edges = map.edges || [];
    const proposed = edges.filter((e) => e.status === "proposed");
    const approved = edges.filter((e) => e.status === "approved");
    const rejected = edges.filter((e) => e.status === "rejected");
    host.innerHTML = `<div class="stack sysmap" style="gap:14px">
      <div class="card"><div class="card-head sysmap-head">
        <div class="min0"><h3>System map</h3><p class="muted">${map.components.length ? `${map.components.length} component${map.components.length === 1 ? "" : "s"} · ${approved.length} approved · ${proposed.length} proposed dependenc${proposed.length === 1 ? "y" : "ies"}${map.scanned_at ? ` · scanned ${esc(timeAgo(map.scanned_at))}` : ""}` : "Relay has not scanned your repositories yet."}</p></div>
        <div class="row wrap" style="gap:6px">
          <button class="btn sm" data-sm="discover" title="List the repositories your GitHub account can reach and clone the ones that belong to this system">${icon("github")}Discover</button>
          <button class="btn sm" data-sm="describe" ${map.components.length ? "" : "disabled"} title="One cheap agent turn writes a one-line description per component">${icon("sparkles")}Describe</button>
          <button class="btn sm" data-sm="edge" ${map.components.length > 1 ? "" : "disabled"}>${icon("plus")}Dependency</button>
          <button class="btn sm primary" data-sm="scan">${ui.busy === "scan" ? icon("spinner", "spin") : icon("refresh")}Rescan</button>
        </div></div>
        ${map.components.length ? `<div class="sysmap-graph" role="img" aria-label="Dependency graph">${graphSvg(map)}</div>
          <div class="sysmap-legend muted"><span><i class="lg-approved"></i>approved</span><span><i class="lg-proposed"></i>proposed</span><span>arrows point from the component that calls to the one it depends on</span></div>`
        : `<div class="empty">${icon("globe", "lg")}<h3>No components yet</h3><p>Rescan reads every repository under the repositories folder: HTTP calls and base URLs, route definitions, SQL functions and compose services. Matches become proposed dependencies with file and line evidence for you to approve.</p></div>`}
      </div>
      ${proposed.length ? `<div class="card"><div class="card-head"><h3>Proposed dependencies</h3><span class="muted">From the code scan. Approved ones are used when planning tasks and suggesting related repositories.</span></div>
        <div class="card-body stack" style="gap:10px">${proposed.map(edgeRow).join("")}</div></div>` : ""}
      <div class="card"><div class="card-head"><h3>Approved dependencies</h3></div><div class="card-body stack" style="gap:10px">
        ${approved.length ? approved.map(edgeRow).join("") : '<div class="muted">None yet. Approve a proposed dependency or add one.</div>'}</div></div>
      <div class="card"><div class="card-head"><h3>Components</h3><button class="btn xs" data-sm="component">${icon("plus", "sm")}Add</button></div>
        <div class="sysmap-comps">${map.components.map(compRow).join("") || '<div class="card-body muted">No components.</div>'}</div></div>
      ${rejected.length ? `<details class="card sysmap-rejected"><summary class="card-head"><h3>Rejected (${rejected.length})</h3></summary><div class="card-body stack" style="gap:10px">${rejected.map(edgeRow).join("")}</div></details>` : ""}
    </div>`;
    bind();
  }

  function edgeRow(e) {
    const ev = e.evidence || [];
    return `<div class="sm-edge ${esc(e.status)}" data-edge="${esc(e.id)}">
      <div class="row between wrap" style="gap:8px">
        <div class="row wrap min0" style="gap:6px"><strong>${esc(label(e.from))}</strong>${icon("arrowRight", "sm")}<strong>${esc(label(e.to))}</strong>
          <span class="badge outline">${esc(e.via)}</span>${e.status === "approved" ? '<span class="badge green">approved</span>' : e.status === "rejected" ? '<span class="badge red">rejected</span>' : '<span class="badge amber">proposed</span>'}
          ${e.source === "manual" ? '<span class="badge">added by hand</span>' : ""}</div>
        <div class="row" style="gap:4px">
          ${e.status !== "approved" ? `<button class="btn xs primary" data-edge-act="approve">${icon("check", "sm")}Approve</button>` : ""}
          ${e.status !== "rejected" ? `<button class="btn xs" data-edge-act="reject">${icon("x", "sm")}Reject</button>` : `<button class="btn xs" data-edge-act="reset">Undo</button>`}
          <button class="btn xs ghost" data-edge-act="edit" title="Edit details">${icon("edit", "sm")}</button>
        </div></div>
      <div class="sm-details">${esc(e.details || "(no details)")}</div>
      ${ev.length ? `<details class="sm-evidence"><summary>${ev.length} piece${ev.length === 1 ? "" : "s"} of evidence</summary><ul>${ev.map((x) => `<li><code class="mono">${esc(x.file)}:${esc(x.line)}</code><span class="mono truncate" title="${esc(x.text)}">${esc(x.text)}</span></li>`).join("")}</ul></details>` : ""}
    </div>`;
  }

  function compRow(c) {
    const p = c.provides || {};
    const deps = (c.depends_on || []).filter((d) => d.status === "approved");
    return `<div class="sm-comp" data-comp="${esc(c.id)}">
      <div class="row between wrap" style="gap:8px">
        <div class="row wrap min0" style="gap:6px"><strong>${esc(c.name)}</strong><span class="badge ${esc(KIND_TONE[c.kind] || "")}">${esc(c.kind || "component")}</span>
          ${c.repo ? `<span class="badge outline">${icon("github", "sm")}${esc(c.repo)}</span>` : ""}${c.cloned ? "" : '<span class="badge amber" title="No local clone">not cloned</span>'}</div>
        <div class="row" style="gap:4px"><button class="btn xs" data-comp-act="edit">${icon("edit", "sm")}Edit</button><button class="btn xs ghost" data-comp-act="delete" title="Remove from the map">${icon("trash", "sm")}</button></div>
      </div>
      ${c.description ? `<div class="sm-desc">${esc(c.description)}</div>` : '<div class="sm-desc muted">No description.</div>'}
      <div class="sm-facts muted">
        ${c.path ? `<span class="mono truncate" title="${esc(c.path)}">${esc(c.path)}</span>` : ""}
        ${c.runs ? `<span>${esc(c.runs)}</span>` : ""}
        ${(p.endpoints || []).length ? `<span title="${esc((p.endpoints || []).slice(0, 40).join("\n"))}">${p.endpoints.length} endpoints</span>` : ""}
        ${(p.sql_functions || []).length ? `<span title="${esc((p.sql_functions || []).slice(0, 40).join("\n"))}">${p.sql_functions.length} SQL functions</span>` : ""}
        ${(p.services || []).length ? `<span>services: ${esc(p.services.slice(0, 6).join(", "))}</span>` : ""}
        ${deps.length ? `<span>depends on ${deps.map((d) => esc(label(d.component))).join(", ")}</span>` : ""}
      </div></div>`;
  }

  function bind() {
    $$("[data-sm]", host).forEach((b) => (b.onclick = () => action(b.dataset.sm, b)));
    $$("[data-edge-act]", host).forEach((b) => (b.onclick = () => edgeAction(b.dataset.edgeAct, b.closest("[data-edge]").dataset.edge)));
    $$("[data-comp-act]", host).forEach((b) => (b.onclick = () => compAction(b.dataset.compAct, b.closest("[data-comp]").dataset.comp)));
    $$("[data-node]", host).forEach((n) => (n.onclick = () => { const row = $(`[data-comp="${CSS.escape(n.dataset.node)}"]`, host); row?.scrollIntoView({ block: "center", behavior: "smooth" }); row?.classList.add("flash"); setTimeout(() => row?.classList.remove("flash"), 1200); }));
  }

  async function action(what, btn) {
    if (what === "scan") {
      ui.busy = "scan"; btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Scanning…`;
      try { map = await api.systemScan(); toast("success", "Scan finished", `${map.components.length} components · ${map.counts.proposed} proposed dependencies`); }
      catch (e) { toast("error", "Scan failed", e.message); }
      ui.busy = ""; draw(); return;
    }
    if (what === "describe") {
      btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Describing…`;
      try { map = await api.systemDescribe(); toast("success", "Descriptions written"); } catch (e) { toast("error", "Could not describe components", e.message); }
      draw(); return;
    }
    if (what === "edge") return edgeEditor();
    if (what === "component") return componentEditor();
    if (what === "discover") return discover();
  }

  async function edgeAction(act, id) {
    const e = map.edges.find((x) => x.id === id);
    if (!e) return;
    try {
      if (act === "approve") map = await api.systemPatchEdge(id, { status: "approved" });
      else if (act === "reject") map = await api.systemPatchEdge(id, { status: "rejected" });
      else if (act === "reset") map = await api.systemPatchEdge(id, { status: "proposed" });
      else if (act === "edit") return edgeEditor(e);
      draw();
    } catch (err) { toast("error", "Could not update the dependency", err.message); }
  }

  function edgeEditor(e = null) {
    const opts = (sel) => map.components.map((c) => `<option value="${esc(c.id)}" ${sel === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    const m = modal(`<h2>${e ? "Edit dependency" : "Add a dependency"}</h2>
      <p class="hint">A dependency says the first component relies on the second: it calls its endpoints, its SQL functions, reads its files or messages.</p>
      <div class="grid2"><div class="field"><label>Component</label><select id="edFrom" ${e ? "disabled" : ""}>${opts(e?.from)}</select></div>
        <div class="field"><label>depends on</label><select id="edTo" ${e ? "disabled" : ""}>${opts(e?.to || map.components[1]?.id)}</select></div></div>
      <div class="grid2"><div class="field"><label>Via</label><select id="edVia">${VIAS.map((v) => `<option ${v === (e?.via || "http") ? "selected" : ""}>${v}</option>`).join("")}</select></div>
        <div class="field"><label>Details</label><input id="edDetails" value="${esc(e?.details || "")}" placeholder="/operation/* endpoints, nv_* SQL functions"></div></div>
      <div class="modal-actions">${e ? '<button class="btn danger" id="edDelete">Delete</button>' : ""}<span style="flex:1"></span><button class="btn" data-close>Cancel</button><button class="btn primary" id="edSave">${e ? "Save" : "Add as approved"}</button></div>`);
    $("#edSave", m.body).onclick = async () => {
      try {
        map = e ? await api.systemPatchEdge(e.id, { details: $("#edDetails", m.body).value, via: $("#edVia", m.body).value })
          : await api.systemAddEdge({ from: $("#edFrom", m.body).value, to: $("#edTo", m.body).value, via: $("#edVia", m.body).value, details: $("#edDetails", m.body).value });
        m.close(); draw();
      } catch (err) { toast("error", "Could not save", err.message); }
    };
    $("#edDelete", m.body) && ($("#edDelete", m.body).onclick = async () => { map = await api.systemDeleteEdge(e.id); m.close(); draw(); });
  }

  async function compAction(act, id) {
    const c = comp(id);
    if (!c) return;
    if (act === "edit") return componentEditor(c);
    if (act === "delete" && await confirm(`Remove ${c.name} from the map?`, "Its dependencies are removed too. A rescan adds it back if the repository is still local.", { danger: true, okLabel: "Remove" })) {
      map = await api.systemDeleteComponent(id); draw();
    }
  }

  function componentEditor(c = null) {
    const m = modal(`<h2>${c ? `Edit ${esc(c.name)}` : "Add a component"}</h2>
      <p class="hint">${c ? "Edited fields are kept when the map is rescanned." : "For a service Relay has no clone of yet, or something outside git (a managed database)."}</p>
      <div class="grid2"><div class="field"><label>Name</label><input id="ceName" value="${esc(c?.name || "")}"></div>
        <div class="field"><label>Kind</label><select id="ceKind">${KINDS.map((k) => `<option ${k === (c?.kind || "api") ? "selected" : ""}>${k}</option>`).join("")}</select></div></div>
      <div class="grid2"><div class="field"><label>GitHub repository</label><input id="ceRepo" value="${esc(c?.repo || "")}" placeholder="owner/name"></div>
        <div class="field"><label>Local path</label><input id="cePath" class="mono" value="${esc(c?.path || "")}" placeholder="cloned on demand when empty"></div></div>
      <div class="field"><label>Description</label><textarea id="ceDesc" rows="2">${esc(c?.description || "")}</textarea></div>
      <div class="field"><label>How to run and test it</label><input id="ceRuns" value="${esc(c?.runs || "")}" placeholder="run: npm run dev · test: npm test"></div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ceSave">Save</button></div>`, { wide: true });
    $("#ceSave", m.body).onclick = async () => {
      const body = { name: $("#ceName", m.body).value, kind: $("#ceKind", m.body).value, repo: $("#ceRepo", m.body).value, path: $("#cePath", m.body).value,
        description: $("#ceDesc", m.body).value, runs: $("#ceRuns", m.body).value };
      try { map = c ? await api.systemPatchComponent(c.id, body) : await api.systemAddComponent(body); m.close(); draw(); }
      catch (err) { toast("error", "Could not save", err.message); }
    };
  }

  async function discover() {
    const m = modal(`<h2>${icon("github")} Discover repositories</h2><p class="hint">Repositories your GitHub account reaches. Tick the ones that belong to this system: Relay clones them next to your other repositories and rescans the map.</p>
      <div class="field"><input id="dsFilter" placeholder="Filter by name"></div><div id="dsList" class="sm-discover"><div class="muted">Asking GitHub…</div></div>
      <div class="modal-actions"><span class="muted" id="dsRoot" style="margin-right:auto"></span><button class="btn" data-close>Cancel</button><button class="btn primary" id="dsGo" disabled>Clone and scan</button></div>`, { wide: true });
    let rows = [];
    const picked = new Set();
    const drawList = () => {
      const q = $("#dsFilter", m.body).value.trim().toLowerCase();
      $("#dsList", m.body).innerHTML = rows.filter((r) => !q || r.repo.toLowerCase().includes(q)).slice(0, 300).map((r) => `<label class="sm-disc-row">
        <input type="checkbox" data-disc="${esc(r.repo)}" ${r.local_path ? "disabled checked" : picked.has(r.repo) ? "checked" : ""}>
        <span class="min0"><strong>${esc(r.repo)}</strong>${r.description ? `<span class="muted truncate"> · ${esc(r.description)}</span>` : ""}</span>
        ${r.local_path ? '<span class="badge green">cloned</span>' : ""}${r.component ? '<span class="badge outline">on the map</span>' : ""}</label>`).join("") || '<div class="muted">No repositories match.</div>';
      $$("[data-disc]", m.body).forEach((c) => (c.onchange = () => { c.checked ? picked.add(c.dataset.disc) : picked.delete(c.dataset.disc); $("#dsGo", m.body).disabled = !picked.size; $("#dsGo", m.body).textContent = picked.size ? `Clone ${picked.size} and scan` : "Clone and scan"; }));
    };
    $("#dsFilter", m.body).addEventListener("input", drawList);
    try { const r = await api.systemDiscover(); rows = r.repos; $("#dsRoot", m.body).textContent = `Clones go to ${r.root}`; drawList(); }
    catch (e) { $("#dsList", m.body).innerHTML = `<div class="modal-error">${esc(e.message)}</div>`; }
    $("#dsGo", m.body).onclick = async () => {
      const btn = $("#dsGo", m.body); btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Cloning…`;
      try {
        const r = await api.systemClone([...picked]);
        map = r;
        toast(r.errors?.length ? "warning" : "success", `Cloned ${r.cloned.length}`, r.errors?.length ? r.errors.map((x) => `${x.repo}: ${x.error}`).join("\n") : "The map was rescanned.");
        m.close(); draw();
      } catch (e) { toast("error", "Clone failed", e.message); btn.disabled = false; btn.textContent = "Clone and scan"; }
    };
  }

  draw();
  load();
  return { reload: load };
}

// A layered drawing: callers on the left, what they depend on to the right. No layout library, no external assets.
export function graphSvg(map) {
  const comps = map.components;
  const edges = (map.edges || []).filter((e) => e.status !== "rejected");
  const depth = new Map(comps.map((c) => [c.id, 0]));
  // Longest-path layering over dependencies (cycles are cut after a few passes).
  for (let pass = 0; pass < comps.length; pass++) {
    let changed = false;
    for (const e of edges) {
      if (!depth.has(e.from) || !depth.has(e.to) || e.from === e.to) continue;
      const want = depth.get(e.from) + 1;
      if (depth.get(e.to) < want && want < comps.length) { depth.set(e.to, want); changed = true; }
    }
    if (!changed) break;
  }
  const kindRank = (k) => { const i = KINDS.indexOf(k); return i < 0 ? 9 : i; };
  const cols = [];
  for (const c of [...comps].sort((a, b) => kindRank(a.kind) - kindRank(b.kind) || a.name.localeCompare(b.name))) {
    const d = depth.get(c.id);
    (cols[d] = cols[d] || []).push(c);
  }
  const W = 184, H = 50, GX = 96, GY = 22, PAD = 18;
  const pos = new Map();
  const colsList = cols.filter(Boolean);
  const tallest = Math.max(...colsList.map((c) => c.length));
  colsList.forEach((col, x) => col.forEach((c, y) => {
    const offset = (tallest - col.length) * (H + GY) / 2;
    pos.set(c.id, { x: PAD + x * (W + GX), y: PAD + offset + y * (H + GY) });
  }));
  const width = PAD * 2 + colsList.length * W + (colsList.length - 1) * GX;
  const height = PAD * 2 + tallest * H + (tallest - 1) * GY;
  const paths = edges.filter((e) => pos.has(e.from) && pos.has(e.to) && e.from !== e.to).map((e) => {
    const a = pos.get(e.from), b = pos.get(e.to);
    const forward = b.x > a.x;
    const x1 = forward ? a.x + W : a.x + W / 2, y1 = forward ? a.y + H / 2 : a.y + H;
    const x2 = forward ? b.x : b.x + W / 2, y2 = forward ? b.y + H / 2 : b.y;
    const dx = forward ? Math.max(30, (x2 - x1) / 2) : 0;
    const d = forward ? `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2 - 6},${y2}` : `M${x1},${y1} C${x1 + 60},${y1 + 40} ${x2 + 60},${y2 - 40} ${x2},${y2 - 6}`;
    return `<path class="sm-link ${esc(e.status)}" d="${d}" marker-end="url(#smArrow${e.status === "approved" ? "A" : "P"})"><title>${esc(e.from)} → ${esc(e.to)} via ${esc(e.via)}: ${esc(e.details || "")}</title></path>`;
  }).join("");
  const nodes = comps.filter((c) => pos.has(c.id)).map((c) => {
    const p = pos.get(c.id);
    const name = c.name.length > 22 ? c.name.slice(0, 21) + "…" : c.name;
    return `<g class="sm-node kind-${esc(c.kind || "library")}" data-node="${esc(c.id)}" transform="translate(${p.x},${p.y})" tabindex="0">
      <rect width="${W}" height="${H}" rx="9"></rect><rect class="sm-kind-bar" width="5" height="${H}" rx="2"></rect>
      <text x="16" y="21" class="sm-name">${esc(name)}</text><text x="16" y="38" class="sm-kind">${esc(c.kind || "component")}${c.cloned ? "" : " · not cloned"}</text>
      <title>${esc(c.name)}${c.description ? `: ${esc(c.description)}` : ""}</title></g>`;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">
    <defs><marker id="smArrowA" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="sm-arrow approved"></path></marker>
      <marker id="smArrowP" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="sm-arrow proposed"></path></marker></defs>
    ${paths}${nodes}</svg>`;
}

export function openSystemMap() { navigate("#/knowledge/system"); }
