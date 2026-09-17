// Tools: MCP servers and command-line tools for every agent CLI Relay drives (orchestrator/toolbox.py).
// Settings → Tools, the per-task picker (New task → Advanced), the repository scope (Repositories → Environment)
// and agents' tool requests in Needs you. Self-contained: talks to /api/tools* directly.
import { $, $$, esc, icon, toast, modal, confirm, timeAgo } from "../ui.js";
import { get, post, put, del, api } from "../api.js";

const MASK = "●●●●";
const T = {
  list: () => get("/api/tools"),
  save: (t) => post("/api/tools", t),
  addCatalog: (id, body) => post(`/api/tools/catalog/${encodeURIComponent(id)}`, body),
  remove: (n) => del(`/api/tools/${encodeURIComponent(n)}`),
  install: (n, action) => post(`/api/tools/${encodeURIComponent(n)}/install`, { action }),
  test: (n) => post(`/api/tools/${encodeURIComponent(n)}/test`, {}),
  forRepo: (p) => get(`/api/tools/for-repo${p ? `?path=${encodeURIComponent(p)}` : ""}`),
  repoScope: (b) => put("/api/tools/repo-scope", b),
  decide: (id, action, b = {}) => post(`/api/tools/requests/${encodeURIComponent(id)}/${action}`, b),
};
const POLICY = {
  ask: "Ask me (Needs you) for every request",
  auto_catalog: "Approve catalog tools automatically, ask for anything else",
  auto_dev: "Approve catalog tools, and anything in development projects",
  off: "Agents cannot request tools",
};
const kindBadge = (t) => t.kind === "cli" ? `<span class="badge outline">CLI</span>` : `<span class="badge blue">MCP · ${esc(t.transport === "http" ? "remote" : "stdio")}</span>`;
const stateBadge = (t) => t.job?.state === "running" ? `<span class="badge amber">${icon("spinner", "spin")}${esc(t.job.action)}ing</span>`
  : t.job?.state === "failed" ? `<span class="badge red" title="${esc(t.job.error || "")}">install failed</span>`
  : t.installed ? `<span class="badge green">ready</span>` : `<span class="badge amber">not installed</span>`;

// ---------------------------------------------------------------------------- editor
const pairRow = (r = { name: "", value: "", secret: false }, ph = "NAME") => `<div class="renv-var tb-pair">
  <input data-pn value="${esc(r.name)}" placeholder="${ph}" spellcheck="false">
  <input data-pv type="${r.secret ? "password" : "text"}" value="${esc(r.secret ? "" : r.value)}" data-has="${r.secret && r.has_value ? 1 : ""}" placeholder="${r.secret && r.has_value ? "saved (hidden)" : esc(r.placeholder || "value")}" spellcheck="false" autocomplete="new-password">
  <label class="renv-secret"><input type="checkbox" data-ps ${r.secret ? "checked" : ""}>secret</label>
  <button type="button" class="btn xs ghost" data-pdel title="Remove">${icon("x", "sm")}</button></div>`;

export function openToolEditor(existing, { onSaved, catalogId } = {}) {
  const t = existing ? JSON.parse(JSON.stringify(existing)) : { name: "", kind: "mcp", transport: "stdio", command: "", args: [], env: [], url: "", headers: [], install: { kind: "none" }, binary: "", usage: "", description: "", enabled: false };
  const m = modal(`<h2>${icon("package")} ${catalogId ? `Add ${esc(t.label || t.name)}` : existing ? `Edit tool · ${esc(existing.name)}` : "Add a tool"}</h2>
    <p class="hint">Relay writes MCP servers into each agent CLI's own config for one turn, inside the task's run folder; your personal CLI settings are never changed. Secret values stay on this server, reach the CLI only through its environment, and are masked in logs.</p>
    <div class="grid3">
      <div class="field"><label for="tbName">Name</label><input id="tbName" value="${esc(t.name)}" placeholder="docs-search" spellcheck="false"></div>
      <div class="field"><label for="tbKind">Kind</label><select id="tbKind" ${catalogId ? "disabled" : ""}><option value="mcp" ${t.kind === "mcp" ? "selected" : ""}>MCP server</option><option value="cli" ${t.kind === "cli" ? "selected" : ""}>Command-line tool</option></select></div>
      <div class="field" data-for="mcp"><label for="tbTransport">Transport</label><select id="tbTransport" ${catalogId ? "disabled" : ""}><option value="stdio" ${t.transport !== "http" ? "selected" : ""}>Local command (stdio)</option><option value="http" ${t.transport === "http" ? "selected" : ""}>Remote URL (HTTP)</option></select></div>
    </div>
    <div class="field"><label for="tbDesc">What it does (shown to agents)</label><input id="tbDesc" value="${esc(t.description || "")}" placeholder="Search our internal API docs"></div>
    <div class="renv-sec" data-for="stdio"><h3>Command</h3>
      <div class="field"><label for="tbCmd">Command</label><input id="tbCmd" class="mono" value="${esc(t.command || "")}" placeholder="npx (or the binary the install provides)" spellcheck="false"></div>
      <div class="field"><label for="tbArgs">Arguments (one per line)</label><textarea id="tbArgs" class="mono" rows="3" spellcheck="false" placeholder="-y&#10;some-mcp-server&#10;{worktree}">${esc((t.args || []).join("\n"))}</textarea><div class="help"><code>{worktree}</code>, <code>{run_dir}</code> and <code>{task_id}</code> are filled in per task.</div></div>
      <div class="row between field-label"><span>Environment</span><button type="button" class="btn xs" id="tbAddEnv">${icon("plus", "sm")}Add</button></div>
      <div class="stack" id="tbEnv" style="gap:6px">${(t.env || []).map((r) => pairRow(r)).join("")}</div></div>
    <div class="renv-sec" data-for="http"><h3>Remote server</h3>
      <div class="field"><label for="tbUrl">URL</label><input id="tbUrl" class="mono" value="${esc(t.url || "")}" placeholder="https://example.com/mcp" spellcheck="false"></div>
      <div class="row between field-label"><span>Headers</span><button type="button" class="btn xs" id="tbAddHdr">${icon("plus", "sm")}Add</button></div>
      <div class="stack" id="tbHdr" style="gap:6px">${(t.headers || []).map((r) => pairRow(r, "Header")).join("")}</div></div>
    <div class="renv-sec"><h3>Install</h3>
      <div class="grid3">
        <div class="field"><label for="tbIk">Installed by Relay</label><select id="tbIk" ${catalogId ? "disabled" : ""}>${["none", "npm", "pip", "script"].map((k) => `<option value="${k}" ${(t.install?.kind || "none") === k ? "selected" : ""}>${{ none: "Not needed", npm: "npm package", pip: "pip package", script: "Shell script" }[k]}</option>`).join("")}</select></div>
        <div class="field" data-ik="npm pip"><label for="tbPkg">Package</label><input id="tbPkg" class="mono" value="${esc(t.install?.package || "")}" spellcheck="false" ${catalogId ? "disabled" : ""}></div>
        <div class="field" data-ik="npm pip script"><label for="tbBin">Command it installs</label><input id="tbBin" class="mono" value="${esc(t.binary || "")}" spellcheck="false" ${catalogId ? "disabled" : ""}></div>
      </div>
      <div class="field" data-ik="script"><label for="tbScript">Script</label><input id="tbScript" class="mono" value="${esc(t.install?.command || "")}" placeholder='curl -fsSL https://… | BIN_DIR="$BIN_DIR" sh' spellcheck="false"><div class="help">Runs with <code>$PREFIX</code> and <code>$BIN_DIR</code> pointing at Relay's tools folder.</div></div>
      <div class="field" data-for="cli"><label for="tbUsage">How agents call it</label><input id="tbUsage" value="${esc(t.usage || "")}" placeholder="mytool check src/ lists problems"></div></div>
    <div class="field inline"><label>Enabled for every task<div class="help">Otherwise enable it per repository (Repositories → Environment) or per task (New task → Advanced).</div></label><span class="switch ${t.enabled ? "on" : ""}" id="tbEnabled" role="switch" tabindex="0"></span></div>
    <div class="modal-error" id="tbError" hidden></div>
    <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" id="tbSave">${icon(catalogId ? "download" : "save")}${catalogId ? "Add and install" : "Save"}</button></div>`, { wide: true });
  const b = m.body;
  const sync = () => {
    const kind = $("#tbKind", b).value, tr = $("#tbTransport", b).value, ik = $("#tbIk", b).value;
    $$("[data-for]", b).forEach((x) => { const f = x.dataset.for; x.hidden = f === "mcp" ? kind !== "mcp" : f === "cli" ? kind !== "cli" : kind !== "mcp" || f !== tr; });
    $$("[data-ik]", b).forEach((x) => { x.hidden = !x.dataset.ik.split(" ").includes(ik); });
    if (kind === "cli") $$("[data-ik~='script'][data-ik~='npm']", b).forEach((x) => (x.hidden = false));
  };
  const bindPairs = () => $$(".tb-pair", b).forEach((r) => { $("[data-pdel]", r).onclick = () => r.remove(); $("[data-ps]", r).onchange = (e) => { $("[data-pv]", r).type = e.target.checked ? "password" : "text"; }; });
  $("#tbAddEnv", b).onclick = () => { $("#tbEnv", b).insertAdjacentHTML("beforeend", pairRow()); bindPairs(); };
  $("#tbAddHdr", b).onclick = () => { $("#tbHdr", b).insertAdjacentHTML("beforeend", pairRow({ name: "", value: "", secret: true }, "Header")); bindPairs(); };
  ["#tbKind", "#tbTransport", "#tbIk"].forEach((s) => ($(s, b).onchange = sync));
  const sw = $("#tbEnabled", b); sw.onclick = () => sw.classList.toggle("on");
  sync(); bindPairs();
  const pairs = (host) => $$(".tb-pair", host).map((r) => { const v = $("[data-pv]", r), s = $("[data-ps]", r).checked; return { name: $("[data-pn]", r).value.trim(), value: s && v.value === "" && v.dataset.has ? MASK : v.value, secret: s }; }).filter((x) => x.name);
  $("#tbSave", b).onclick = async () => {
    const ik = $("#tbIk", b).value;
    const payload = { original_name: existing?.name, name: $("#tbName", b).value.trim().toLowerCase(), kind: $("#tbKind", b).value, transport: $("#tbTransport", b).value,
      description: $("#tbDesc", b).value, command: $("#tbCmd", b).value.trim(), args: $("#tbArgs", b).value.split("\n").map((x) => x.trim()).filter(Boolean),
      env: pairs($("#tbEnv", b)), url: $("#tbUrl", b).value.trim(), headers: pairs($("#tbHdr", b)), binary: $("#tbBin", b).value.trim(), usage: $("#tbUsage", b).value,
      install: { kind: ik, package: $("#tbPkg", b).value.trim(), command: $("#tbScript", b).value }, enabled: sw.classList.contains("on") };
    const err = $("#tbError", b); err.hidden = true;
    const btn = $("#tbSave", b); btn.disabled = true;
    try {
      const saved = catalogId ? await T.addCatalog(catalogId, { name: payload.name, env: payload.env, headers: payload.headers, enabled: payload.enabled })
        : await T.save(payload);
      if (!catalogId && !saved.installed && ik !== "none") await T.install(saved.name, "install");
      toast("success", `Tool ${saved.name} saved`, saved.installed ? "" : "Installing in the background.");
      m.close(); onSaved && onSaved(saved);
    } catch (e) { err.textContent = e.message; err.hidden = false; btn.disabled = false; }
  };
}

// ---------------------------------------------------------------------------- requests (Settings and Needs you)
export function toolRequestActionsHtml(x) {
  const p = x.proposal || {};
  const needs = (p.missing_secrets || []).length;
  return `${p.kind ? `<div class="tb-proposal mono">${esc(p.kind === "cli" ? `CLI ${p.binary || ""}` : p.transport === "http" ? `MCP ${p.url || ""}` : `MCP ${[p.command, ...(p.args || [])].join(" ")}`)}${p.install?.package ? ` · ${esc(p.install.kind)} ${esc(p.install.package)}` : ""}</div>` : ""}
    ${needs ? `<div class="nx-auto">${icon("alert", "sm")}<span>Needs ${esc(p.missing_secrets.join(", "))}: add it under Settings → Tools after approving.</span></div>` : ""}
    <div class="nx-opts"><button type="button" class="btn sm primary" data-tool-approve="task">${icon("check")}Approve for this task</button><button type="button" class="btn sm" data-tool-approve="repo">Approve for the repository</button><button type="button" class="btn sm" data-tool-deny>${icon("x")}Deny</button></div>`;
}

export function bindToolRequests(root, onDone) {
  const act = async (btn, fn, ok) => {
    const card = btn.closest("[data-inbox],[data-treq]");
    $$("button", card).forEach((x) => (x.disabled = true));
    try { await fn(); card.classList.add("nx-done"); toast("success", ok); onDone && onDone(); }
    catch (e) { $$("button", card).forEach((x) => (x.disabled = false)); toast("error", "Could not decide", e.message); }
  };
  const idOf = (el) => (el.closest("[data-treq]") || el.closest("[data-inbox]")).dataset.treq || el.closest("[data-inbox]").dataset.inbox;
  $$("[data-tool-approve]", root).forEach((b) => (b.onclick = () => act(b, () => T.decide(idOf(b), "approve", { scope: b.dataset.toolApprove }), "Tool approved · available on the agent's next turn")));
  $$("[data-tool-deny]", root).forEach((b) => (b.onclick = () => act(b, () => T.decide(idOf(b), "deny"), "Tool request denied")));
}

// ---------------------------------------------------------------------------- Settings → Tools
export function mountTools(body) {
  let d = null, results = {}, timer = null;
  const load = async () => {
    try { d = await T.list(); } catch (e) { body.innerHTML = `<div class="card"><div class="card-body"><div class="empty small">${icon("alert")} ${esc(e.message)}</div></div></div>`; return; }
    draw();
    clearTimeout(timer);
    if (body.isConnected && d.tools.some((t) => t.job?.state === "running")) timer = setTimeout(load, 2500);
  };
  const usage = (n) => (d.stats || {})[n] || null;
  const draw = () => {
    const pending = d.requests.filter((r) => r.status === "pending");
    const decided = d.requests.filter((r) => r.status !== "pending").slice(0, 12);
    const agents = Object.entries(d.agents || {});
    body.innerHTML = `
      <div class="card"><div class="card-head"><div><h3>Tools</h3><p class="card-sub">MCP servers and command-line tools every agent can use. Relay translates them into each CLI's own configuration for the turn and never edits your personal CLI settings.</p></div><button type="button" class="btn sm primary" id="tbAdd">${icon("plus")}Add tool</button></div>
        <div class="card-body">${d.tools.length ? `<div class="tb-list">${d.tools.map((t) => { const u = usage(t.name); return `<div class="tb-item" data-tool="${esc(t.name)}">
            <div class="tb-main"><div class="row wrap" style="gap:6px"><strong class="mono-strong">${esc(t.name)}</strong>${kindBadge(t)}${stateBadge(t)}${t.source === "agent" ? `<span class="badge purple" title="Requested by ${esc(t.created_by || "an agent")}">agent request</span>` : ""}${(t.missing_secrets || []).length ? `<span class="badge red">needs ${esc(t.missing_secrets.join(", "))}</span>` : ""}</div>
              <div class="muted tb-desc">${esc(t.description || t.usage || "")}</div>
              <div class="tb-meta mono truncate">${esc(t.kind === "cli" ? t.binary : t.transport === "http" ? t.url : [t.command, ...(t.args || [])].join(" "))}</div>
              <div class="tb-meta">${u ? `${u.calls} call${u.calls === 1 ? "" : "s"} · last ${esc(timeAgo(u.last))} · ${esc(Object.entries(u.agents || {}).map(([a, n]) => `${(d.agents || {})[a] || a} ${n}`).join(", "))}` : "not used yet"}</div></div>
            <div class="tb-actions"><label class="tb-enable" title="Give it to every task"><span class="switch ${t.enabled ? "on" : ""}" data-enable role="switch" tabindex="0" aria-label="Enabled for every task"></span><span>All tasks</span></label>
              <button type="button" class="btn sm" data-test>${icon("activity")}<span>Test</span></button>${t.install?.kind !== "none" ? `<button type="button" class="btn sm" data-install>${icon(t.installed ? "refresh" : "download")}<span>${t.installed ? "Update" : "Install"}</span></button>` : ""}<button type="button" class="btn sm" data-edit>${icon("edit")}<span>Edit</span></button><button type="button" class="btn sm danger" data-del title="Remove">${icon("trash")}</button></div>
            ${results[t.name] ? `<div class="tb-res conn-result ${results[t.name].ok ? "green" : "red"}"><strong>${icon(results[t.name].ok ? "check" : "alert", "sm")} ${esc(results[t.name].summary || "")}</strong>${(results[t.name].tools || []).length ? `<pre>${esc(results[t.name].tools.join("  "))}</pre>` : ""}</div>` : ""}
            ${t.job?.state === "failed" ? `<pre class="tb-log">${esc((t.job.log || "").slice(-1500))}</pre>` : ""}
          </div>`; }).join("")}</div>` : `<div class="empty small">${icon("package", "lg")}<p>No tools yet. Add one from the catalog below, or your own MCP server or command-line tool.</p></div>`}</div></div>
      <div class="card"><div class="card-head"><div><h3>Requests from agents</h3><p class="card-sub">Agents ask with <code>relay-tools request</code> or a <code>tool_request</code> in their envelope. Pending requests also appear in Needs you.</p></div></div><div class="card-body">
        <div class="field"><label for="tbPolicy">Policy</label><select id="tbPolicy">${d.policies.map((p) => `<option value="${p}" ${d.policy === p ? "selected" : ""}>${esc(POLICY[p] || p)}</option>`).join("")}</select><div class="help">Development projects are marked under Repositories → Environment → Tools.</div></div>
        ${pending.length ? `<div class="tb-reqs">${pending.map((r) => `<div class="tb-req" data-treq="${esc(r.id)}"><div class="row wrap" style="gap:6px"><span class="badge amber">pending</span><strong class="mono-strong">${esc(r.name)}</strong><span class="muted">${esc((d.agents || {})[r.agent] || r.agent || "")} (${esc(r.role || "")}) · <a href="#/task/${esc(r.task)}">${esc(r.task_name || r.task)}</a> · ${esc(timeAgo(r.time))}</span></div><div class="tb-why">${esc(r.why || "")}</div>${toolRequestActionsHtml({ proposal: r.proposal })}</div>`).join("")}</div>` : `<div class="muted">No pending requests.</div>`}
        ${decided.length ? `<div class="tb-decided">${decided.map((r) => `<div class="conn-call"><span class="badge ${r.status === "approved" ? "green" : "red"}">${esc(r.status)}</span><span class="mono-strong truncate">${esc(r.name)}</span><span class="truncate muted" title="${esc(r.why || "")}">${esc(r.why || "")}</span><span class="muted conn-when">${esc(r.decided_by === "policy" ? "by policy" : "by you")} · ${esc(timeAgo(r.decided || r.time))}</span></div>`).join("")}</div>` : ""}
      </div></div>
      <div class="card"><div class="card-head"><div><h3>Catalog</h3><p class="card-sub">Widely used, reputable servers for coding work. Nothing installs until you click. Installs go to <code>${esc(d.tools_dir)}</code>.</p></div></div><div class="card-body"><div class="tb-catalog">
        ${d.catalog.map((c) => `<div class="tb-cat" data-cat="${esc(c.id)}"><div class="row wrap" style="gap:6px"><strong>${esc(c.label)}</strong>${kindBadge(c)}</div><div class="muted tb-desc">${esc(c.description)}</div>
          <div class="tb-meta">${esc(c.publisher || "")} · <a href="${esc(c.homepage)}" target="_blank" rel="noopener">source ${icon("external", "sm")}</a>${c.install?.package ? ` · <span class="mono">${esc(c.install.package)}</span>` : ""}</div>
          <div>${c.added ? `<span class="badge green">${icon("check", "sm")}added</span>` : `<button type="button" class="btn sm" data-add>${icon("download")}Add${c.install?.kind !== "none" ? " and install" : ""}</button>`}</div></div>`).join("")}
      </div></div></div>
      <div class="card"><div class="card-head"><div><h3>Built in</h3><p class="card-sub">Always on PATH for agents where available.</p></div></div><div class="card-body"><div class="tb-builtins">
        ${d.builtins.map((x) => `<div class="tb-builtin" title="${esc(x.path || "not found")}"><span class="badge ${x.available ? "green" : "outline"}">${x.available ? "on PATH" : "missing"}</span><span class="mono-strong">${esc(x.name)}</span><span class="muted truncate">${esc(x.description)}</span></div>`).join("")}
      </div></div></div>
      <div class="card"><div class="card-head"><div><h3>How each CLI receives MCP servers</h3><p class="card-sub">CLIs not listed cannot load MCP servers from Relay; their agents are told to use the command-line equivalent instead.</p></div></div><div class="card-body"><div class="tb-builtins">
        ${agents.map(([id, label]) => `<div class="tb-builtin"><span class="badge ${d.mcp_support[id] ? "blue" : "outline"}">${d.mcp_support[id] ? "MCP" : "CLI tools only"}</span><span class="mono-strong">${esc(label)}</span><span class="muted truncate mono">${esc(d.mcp_support[id] || "")}</span></div>`).join("")}
      </div></div></div>`;
    $("#tbAdd", body).onclick = () => openToolEditor(null, { onSaved: load });
    $("#tbPolicy", body).onchange = async (e) => { try { await api.saveSettings({ tools_request_policy: e.target.value }); toast("success", "Policy saved"); } catch (err) { toast("error", "Could not save", err.message); } };
    bindToolRequests(body, () => setTimeout(load, 300));
    $$(".tb-item", body).forEach((row) => {
      const t = d.tools.find((x) => x.name === row.dataset.tool);
      const sw = $("[data-enable]", row);
      sw.onclick = async () => { try { await T.save({ ...t, original_name: t.name, enabled: !t.enabled }); load(); } catch (e) { toast("error", "Could not save", e.message); } };
      $("[data-edit]", row).onclick = () => openToolEditor(t, { onSaved: load });
      $("[data-install]", row) && ($("[data-install]", row).onclick = async () => { try { await T.install(t.name, t.installed ? "update" : "install"); load(); } catch (e) { toast("error", "Install failed", e.message); } });
      $("[data-del]", row).onclick = async () => { if (!(await confirm(`Remove ${t.name}?`, "Tasks lose it from their next turn. Relay-installed packages are uninstalled.", { danger: true, okLabel: "Remove" }))) return; try { await del(`/api/tools/${encodeURIComponent(t.name)}?uninstall=1`); setTimeout(load, 500); } catch (e) { toast("error", "Could not remove", e.message); } };
      $("[data-test]", row).onclick = async (e) => { const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}<span>Testing…</span>`; try { results[t.name] = await T.test(t.name); } catch (err) { results[t.name] = { ok: false, summary: err.message }; } draw(); };
    });
    $$(".tb-cat [data-add]", body).forEach((btn) => (btn.onclick = async () => {
      const c = d.catalog.find((x) => x.id === btn.closest("[data-cat]").dataset.cat);
      const secrets = [...(c.env || []), ...(c.headers || [])].some((r) => r.secret);
      if (secrets) return openToolEditor({ ...c, name: c.id, enabled: false }, { catalogId: c.id, onSaved: load });
      btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Adding…`;
      try { await T.addCatalog(c.id, {}); toast("success", `${c.label} added`, c.install?.kind !== "none" ? "Installing in the background." : ""); load(); }
      catch (e) { toast("error", "Could not add", e.message); btn.disabled = false; }
    }));
  };
  body.innerHTML = `<div class="card"><div class="card-body"><div class="empty small">${icon("spinner", "spin")} Loading tools…</div></div></div>`;
  load();
}

// ---------------------------------------------------------------------------- pickers
// Checkbox list of tools for a task. Returns { value() }.
export function toolPicker(host, { tools, selected, onChange }) {
  const set = new Set(selected || []);
  if (!tools.length) {
    host.innerHTML = `<div class="help">No tools yet. Add them in <a href="#/settings/tools" data-close>Settings → Tools</a>.</div>`;
    return { value: () => [...set] };
  }
  host.innerHTML = `<div class="conn-pick">${tools.map((t) => `<label class="conn-pick-row"><input type="checkbox" value="${esc(t.name)}" ${set.has(t.name) ? "checked" : ""}><span class="mono-strong truncate">${esc(t.name)}</span>${kindBadge(t)}${t.installed ? "" : '<span class="badge amber">not installed</span>'}</label>`).join("")}</div>`;
  $$("input", host).forEach((i) => (i.onchange = () => { i.checked ? set.add(i.value) : set.delete(i.value); onChange && onChange([...set]); }));
  return { value: () => [...set] };
}

// New task → Advanced · tools. data.tools stays null until the user changes it (the defaults then apply at start).
export function mountTaskTools(host, summaryEl, data) {
  T.forRepo(data.repo).then((r) => {
    const sum = (names) => { if (summaryEl) summaryEl.textContent = `· ${names.length ? names.join(", ") : "none"}${data.tools ? "" : " (defaults)"}`; };
    const current = data.tools || r.defaults;
    sum(current);
    toolPicker(host, { tools: r.tools, selected: current, onChange: (names) => { data.tools = names; sum(names); } });
  }).catch(() => { if (summaryEl) summaryEl.textContent = "· unavailable"; });
}

// Repositories → Environment: tools added or removed for tasks in this repository, and the development-project flag.
export async function mountRepoTools(host, repo) {
  if (!host) return;
  host.innerHTML = `<h3>Tools</h3><p class="hint">Tools enabled for every task are used here unless you turn them off; add others just for this repository.</p><div id="rtBody" class="muted">Loading…</div>`;
  let r;
  try { r = await T.forRepo(repo.path); } catch (e) { $("#rtBody", host).textContent = `Could not load tools: ${e.message}`; return; }
  const draw = () => {
    const box = $("#rtBody", host); box.className = "";
    if (!r.tools.length) { box.innerHTML = `<div class="help">No tools yet. Add them in <a href="#/settings/tools" data-close>Settings → Tools</a>.</div>`; return; }
    const on = new Set(r.defaults);
    box.innerHTML = `<div class="conn-pick">${r.tools.map((t) => `<label class="conn-pick-row"><input type="checkbox" data-rt="${esc(t.name)}" ${on.has(t.name) ? "checked" : ""}><span class="mono-strong truncate">${esc(t.name)}</span>${kindBadge(t)}${t.enabled ? '<span class="badge outline">all tasks</span>' : ""}</label>`).join("")}</div>
      <label class="row" style="gap:6px;margin-top:8px"><input type="checkbox" id="rtDev" ${r.scope?.dev ? "checked" : ""}> Development project: with the "anything in development projects" policy, agents' tool requests here are approved automatically</label><div class="help" id="rtState"></div>`;
    const save = async (patch) => {
      try { const res = await T.repoScope({ path: repo.path, ...patch }); r.scope = res.scope; r.defaults = res.defaults; $("#rtState", host).textContent = "Saved"; }
      catch (e) { $("#rtState", host).textContent = `Could not save: ${e.message}`; }
    };
    $$("[data-rt]", box).forEach((i) => (i.onchange = () => {
      const t = r.tools.find((x) => x.name === i.dataset.rt);
      const enable = new Set(r.scope?.enable || []), disable = new Set(r.scope?.disable || []);
      if (i.checked) { disable.delete(t.name); if (!t.enabled) enable.add(t.name); } else { enable.delete(t.name); if (t.enabled) disable.add(t.name); }
      save({ enable: [...enable], disable: [...disable] });
    }));
    $("#rtDev", box).onchange = (e) => save({ dev: e.target.checked });
  };
  draw();
}
