// Connectors: named ways for agents to look at the real environments behind the code
// (HTTP APIs, PostgreSQL, logs, containers, hosts over SSH, the running app in a browser). See orchestrator/connectors.py.
import { $, $$, esc, icon, toast, modal, confirm, timeAgo, basename, skeleton } from "../ui.js";
import { api } from "../api.js";

const MASK = "●●●●";
const TYPE_LABEL = { http: "HTTP API", postgres: "PostgreSQL", logs: "Logs", docker: "Docker", ssh: "SSH", browser: "Browser" };
const TYPE_ICON = { http: "globe", postgres: "layers", logs: "list", docker: "package", ssh: "terminal", browser: "eye" };
const TYPE_HELP = {
  http: "Agents call the API with relay-connect http. Relay adds the credentials; read access allows GET and HEAD only.",
  postgres: "Agents run SQL with relay-connect sql. Read access runs every statement in a read-only transaction with a timeout and a row limit. Use a database role with SELECT grants only as well.",
  logs: "Agents read recent log lines with relay-connect logs, from containers through the Docker socket or from Loki.",
  docker: "Agents check container state with relay-connect docker. Restarting needs write access and the restart switch.",
  ssh: "Agents run one allow-listed command per call with relay-connect ssh. Chaining, pipes and redirection are refused; the key stays a file on this server and never reaches the agent.",
  browser: "Agents open the running app with relay-connect browser: navigation, screenshots and console errors. Relay performs the login steps; the password never leaves Relay.",
};

export const envBadge = (e) => `<span class="badge ${e === "prod" ? "red" : e === "staging" ? "amber" : "green"}" title="Environment">${esc(e)}</span>`;
export const accessBadge = (a) => `<span class="badge ${a === "write" ? "amber" : "outline"}" title="Access level">${a === "write" ? "read + write" : "read only"}</span>`;

// [key, label, kind, placeholder|options, help, showWhen]
const FIELDS = {
  http: [
    ["base_url", "Base URL", "text", "http://api.internal:8000"],
    ["auth_type", "Authentication", "select", ["none", "bearer", "basic", "header"]],
    ["auth_token", "Bearer token", "secret", "", "", "auth_type=bearer"],
    ["auth_user", "User", "text", "", "", "auth_type=basic"],
    ["auth_password", "Password", "secret", "", "", "auth_type=basic"],
    ["auth_header", "Header name", "text", "X-Api-Key", "", "auth_type=header"],
    ["auth_value", "Header value", "secret", "", "", "auth_type=header"],
    ["headers", "Default headers", "headers"],
    ["allowed_methods", "Allowed methods", "methods", "", "Methods other than GET, HEAD and OPTIONS also need write access."],
    ["path_allowlist", "Path allowlist (optional)", "lines", "/api/*\n/health", "One pattern per line; * matches anything. Empty allows every path under the base URL."],
    ["health_path", "Test path", "text", "/health"],
    ["timeout", "Timeout (seconds)", "number"],
    ["verify_tls", "Verify TLS certificates", "bool"],
  ],
  postgres: [
    ["host", "Host", "text", "db.internal"], ["port", "Port", "number"], ["database", "Database", "text", "app"],
    ["user", "User", "text", "readonly"], ["password", "Password", "secret"],
    ["sslmode", "SSL mode", "select", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]],
    ["allowed_schemas", "Allowed schemas (optional)", "lines", "public", "Becomes the search path; queries naming another schema are refused."],
    ["row_limit", "Row limit", "number"], ["statement_timeout_ms", "Statement timeout (ms)", "number"],
  ],
  logs: [
    ["source", "Source", "select", ["docker", "loki"]],
    ["containers", "Containers", "lines", "api\nworker", "Only these containers can be read.", "source=docker"],
    ["loki_url", "Loki URL", "text", "http://loki:3100", "", "source=loki"],
    ["loki_query", "LogQL stream selector", "text", '{app="api"}', "Agents can filter with --grep but cannot change the selector.", "source=loki"],
    ["loki_token", "Bearer token", "secret", "", "", "source=loki"],
    ["loki_user", "Basic auth user", "text", "", "", "source=loki"],
    ["loki_password", "Basic auth password", "secret", "", "", "source=loki"],
    ["max_lines", "Lines per call (at most)", "number"],
  ],
  docker: [
    ["containers", "Containers", "lines", "api\nworker", "Only these containers can be inspected."],
    ["allow_restart", "Allow restarting these containers (needs write access)", "bool"],
  ],
  ssh: [
    ["host", "Host", "text", "10.0.0.2"], ["port", "Port", "number"], ["user", "User", "text", "deploy"],
    ["key_path", "Private key path on this server", "text", "/root/.ssh/id_ed25519", "The key file Relay reads. Its contents are never stored here and never leave this server."],
    ["known_hosts", "known_hosts file (optional)", "text", "/root/.ssh/known_hosts"],
    ["strict_host_key", "Refuse an unknown host key", "bool"],
    ["allowed_commands", "Allowed commands", "lines", "docker ps*\nuptime", "One glob pattern per line; * matches anything. A command matching none of them is refused."],
    ["write_commands", "Commands that need write access", "lines", "docker compose up -d*", "Only a connector with write access may run these, and on production each one also needs --confirm-prod."],
    ["allow_shell_operators", "Allow ; && || | > < in commands", "bool", "", "Off by default: with it off a command can never chain a second one or redirect its output."],
    ["timeout", "Timeout (seconds)", "number"],
    ["workdir", "Working directory (optional)", "text", "/opt/stack"],
  ],
  browser: [
    ["base_url", "Base URL", "text", "http://web.internal:3000"],
    ["username", "Username", "text"], ["password", "Password", "secret"],
    ["login_steps", "Login steps (optional)", "steps", "goto /login\nfill #email {{username}}\nfill #password {{password}}\nclick button[type=submit]\nwaitfor .app-shell", "One step per line: goto, fill, click, press, wait, waitfor. {{username}} and {{password}} are filled in by Relay."],
    ["width", "Viewport width", "number"], ["height", "Viewport height", "number"],
  ],
};

function fieldHtml(c, f) {
  const [key, label, kind, ph = "", help = "", when = ""] = f;
  const cfg = c.config || {};
  const v = cfg[key];
  const w = when ? ` data-when="${esc(when)}"` : "";
  const helpHtml = help ? `<div class="help">${esc(help)}</div>` : "";
  const id = `cf_${key}`;
  if (kind === "select") return `<div class="field"${w}><label for="${id}">${esc(label)}</label><select id="${id}" data-k="${key}">${ph.map((o) => `<option ${v === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>${helpHtml}</div>`;
  if (kind === "secret") return `<div class="field"${w}><label for="${id}">${esc(label)}</label><input id="${id}" type="password" data-k="${key}" data-secret data-has="${cfg[`has_${key}`] ? 1 : ""}" value="" placeholder="${cfg[`has_${key}`] ? "saved (hidden); type to replace" : esc(ph)}" autocomplete="new-password" spellcheck="false">${helpHtml}</div>`;
  if (kind === "number") return `<div class="field"${w}><label for="${id}">${esc(label)}</label><input id="${id}" type="number" data-k="${key}" value="${esc(v ?? "")}">${helpHtml}</div>`;
  if (kind === "bool") return `<div class="field inline"${w}><label>${esc(label)}${helpHtml}</label><span class="switch ${v ? "on" : ""}" data-k="${key}" data-bool role="switch" tabindex="0" aria-checked="${v ? "true" : "false"}" aria-label="${esc(label)}"></span></div>`;
  if (kind === "lines" || kind === "steps") return `<div class="field"${w}><label for="${id}">${esc(label)}</label><textarea id="${id}" data-k="${key}" data-lines="${kind === "lines" ? 1 : ""}" rows="${kind === "steps" ? 5 : 3}" class="mono" spellcheck="false" placeholder="${esc(ph)}">${esc(Array.isArray(v) ? v.join("\n") : v || "")}</textarea>${helpHtml}</div>`;
  if (kind === "methods") return `<div class="field"${w}><label>${esc(label)}</label><div class="row wrap conn-methods">${["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"].map((m) => `<label class="renv-secret"><input type="checkbox" data-method="${m}" ${(v || []).includes(m) ? "checked" : ""}>${m}</label>`).join("")}</div>${helpHtml}</div>`;
  if (kind === "headers") return `<div class="field"${w}><div class="row between field-label"><span>${esc(label)}</span><button type="button" class="btn xs" id="cfAddHeader">${icon("plus", "sm")}Add</button></div><div class="stack" id="cfHeaders" style="gap:6px">${(v || []).map(headerRow).join("")}</div></div>`;
  return `<div class="field"${w}><label for="${id}">${esc(label)}</label><input id="${id}" data-k="${key}" value="${esc(v ?? "")}" placeholder="${esc(ph)}" spellcheck="false">${helpHtml}</div>`;
}
const headerRow = (h = { name: "", value: "", secret: false }) => `<div class="renv-var conn-header">
  <input data-hn value="${esc(h.name)}" placeholder="Header" spellcheck="false">
  <input data-hv type="${h.secret ? "password" : "text"}" value="${esc(h.secret ? "" : h.value)}" data-has="${h.secret && h.has_value ? 1 : ""}" placeholder="${h.secret && h.has_value ? "saved (hidden)" : "value"}" spellcheck="false" autocomplete="new-password">
  <label class="renv-secret"><input type="checkbox" data-hs ${h.secret ? "checked" : ""}>secret</label>
  <button type="button" class="btn xs ghost" data-hdel title="Remove">${icon("x", "sm")}</button></div>`;

export async function openConnectorEditor(existing, { onSaved, defaults } = {}) {
  let repos = [];
  try { repos = (await api.repos()).repos || []; } catch {}
  const c = existing ? JSON.parse(JSON.stringify(existing)) : { name: "", type: "http", environment: "dev", access: "read", description: "", repos: [], components: [], config: JSON.parse(JSON.stringify((defaults || {}).http || {})) };
  const m = modal(`<h2>${icon("zap")} ${existing ? `Edit connector · ${esc(existing.name)}` : "Add connector"}</h2>
    <p class="hint">Credentials stay on this server. Agents call the connector through Relay, which applies the access level, masks secrets and records every call in the task.</p>
    <div class="grid3">
      <div class="field"><label for="cfName">Name</label><input id="cfName" value="${esc(c.name)}" placeholder="items-api" spellcheck="false" autocomplete="off"></div>
      <div class="field"><label for="cfType">Type</label><select id="cfType" ${existing ? "disabled" : ""}>${Object.entries(TYPE_LABEL).map(([k, l]) => `<option value="${k}" ${c.type === k ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="field"><label for="cfEnv">Environment</label><select id="cfEnv">${["dev", "staging", "prod"].map((e) => `<option ${c.environment === e ? "selected" : ""}>${e}</option>`).join("")}</select></div>
    </div>
    <div class="field"><label for="cfDesc">What it is (shown to agents)</label><input id="cfDesc" value="${esc(c.description)}" placeholder="Items API behind the dashboard"></div>
    <p class="hint" id="cfTypeHelp"></p>
    <div id="cfFields"></div>
    <div class="renv-sec"><h3>Access</h3>
      <div class="row wrap" style="gap:14px">
        <label class="row" style="gap:6px"><input type="radio" name="cfAccess" value="read" ${c.access !== "write" ? "checked" : ""}> Read only <span class="muted">(recommended)</span></label>
        <label class="row" style="gap:6px"><input type="radio" name="cfAccess" value="write" ${c.access === "write" ? "checked" : ""}> Read and write</label>
      </div>
      <div class="field inline conn-prod" id="cfProdRow" hidden><label>Allow writes to production<div class="help">Agents must still add --confirm-prod to each write, and the rules tell them to write to production only when the task asks.</div></label><span class="switch ${c.prod_write_enabled ? "on" : ""}" id="cfProdWrite" role="switch" tabindex="0"></span></div>
    </div>
    <div class="renv-sec"><h3>Linked repositories</h3>
      <p class="hint">Tasks in a linked repository get this connector by default (never for prod). Change it per task in the New task wizard, or per repository under Repositories → Environment.</p>
      <div class="conn-repos">${repos.length ? repos.map((r) => `<label class="row" style="gap:6px" title="${esc(r.path)}"><input type="checkbox" data-repo="${esc(r.path)}" ${(c.repos || []).includes(r.path) ? "checked" : ""}><span class="truncate">${esc(r.name || basename(r.path))}</span></label>`).join("") : '<span class="muted">No repositories found.</span>'}</div>
      <div class="field"><label for="cfComponents">Components (optional)</label><input id="cfComponents" value="${esc((c.components || []).join(", "))}" placeholder="items service, dashboard" spellcheck="false"></div>
    </div>
    <div class="modal-error" id="cfError" hidden></div>
    <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" id="cfSave">${icon("save")}Save</button></div>`, { wide: true });
  const body = m.body;

  const drawFields = () => {
    const type = $("#cfType", body).value;
    if (type !== c.type) { c.type = type; c.config = JSON.parse(JSON.stringify((defaults || {})[type] || {})); }
    $("#cfTypeHelp", body).textContent = TYPE_HELP[type];
    $("#cfFields", body).innerHTML = `<div class="renv-sec"><h3>${esc(TYPE_LABEL[type])}</h3>${FIELDS[type].map((f) => fieldHtml(c, f)).join("")}</div>`;
    const box = $("#cfFields", body);
    const applyWhen = () => $$("[data-when]", box).forEach((el) => {
      const [k, val] = el.dataset.when.split("=");
      const src = $(`[data-k="${k}"]`, box);
      el.hidden = src && src.value !== val;
    });
    $$("select[data-k]", box).forEach((s) => s.addEventListener("change", applyWhen));
    applyWhen();
    $$("[data-bool]", box).forEach((s) => { const flip = () => { s.classList.toggle("on"); s.setAttribute("aria-checked", s.classList.contains("on")); }; s.onclick = flip; s.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } }; });
    const bindHeaders = () => $$(".conn-header", box).forEach((r) => {
      $("[data-hdel]", r).onclick = () => r.remove();
      $("[data-hs]", r).onchange = (e) => { $("[data-hv]", r).type = e.target.checked ? "password" : "text"; };
    });
    const add = $("#cfAddHeader", box);
    if (add) add.onclick = () => { $("#cfHeaders", box).insertAdjacentHTML("beforeend", headerRow()); bindHeaders(); };
    bindHeaders();
  };
  const drawProd = () => {
    const prod = $("#cfEnv", body).value === "prod" && $("[name=cfAccess]:checked", body).value === "write";
    $("#cfProdRow", body).hidden = !prod;
  };
  $("#cfType", body).onchange = drawFields;
  $("#cfEnv", body).onchange = drawProd;
  $$("[name=cfAccess]", body).forEach((r) => (r.onchange = drawProd));
  $("#cfProdWrite", body).onclick = () => $("#cfProdWrite", body).classList.toggle("on");
  drawFields(); drawProd();

  const collect = () => {
    const box = $("#cfFields", body);
    const cfg = {};
    $$("[data-k]", box).forEach((el) => {
      const k = el.dataset.k;
      if (el.dataset.bool !== undefined) cfg[k] = el.classList.contains("on");
      else if (el.dataset.secret !== undefined) cfg[k] = el.value === "" && el.dataset.has ? MASK : el.value;
      else if (el.dataset.lines) cfg[k] = el.value.split("\n").map((x) => x.trim()).filter(Boolean);
      else if (el.type === "number") cfg[k] = el.value === "" ? null : Number(el.value);
      else cfg[k] = el.value;
    });
    if ($$("[data-method]", box).length) cfg.allowed_methods = $$("[data-method]", box).filter((x) => x.checked).map((x) => x.dataset.method);
    if ($("#cfHeaders", box)) cfg.headers = $$(".conn-header", box).map((r) => {
      const secret = $("[data-hs]", r).checked, v = $("[data-hv]", r);
      return { name: $("[data-hn]", r).value.trim(), value: secret && v.value === "" && v.dataset.has ? MASK : v.value, secret };
    }).filter((h) => h.name);
    return {
      original_name: existing?.name, name: $("#cfName", body).value.trim().toLowerCase(), type: $("#cfType", body).value,
      environment: $("#cfEnv", body).value, access: $("[name=cfAccess]:checked", body).value, description: $("#cfDesc", body).value,
      prod_write_enabled: $("#cfProdWrite", body).classList.contains("on"),
      repos: $$("[data-repo]", body).filter((x) => x.checked).map((x) => x.dataset.repo),
      components: $("#cfComponents", body).value.split(",").map((x) => x.trim()).filter(Boolean), config: cfg,
    };
  };
  $("#cfSave", body).onclick = async () => {
    const payload = collect();
    const err = $("#cfError", body);
    err.hidden = true;
    const newlyProdWrite = payload.environment === "prod" && payload.access === "write" && payload.prod_write_enabled
      && !(existing && existing.environment === "prod" && existing.access === "write" && existing.prod_write_enabled);
    if (newlyProdWrite) {
      const ok = await confirm("Let agents write to production?", `Agents working on tasks that use ${payload.name} will be able to change production through it (each write still needs --confirm-prod). Only continue if a task really needs this.`, { danger: true, okLabel: "Allow production writes" });
      if (!ok) return;
      payload.confirm_prod_write = true;
    }
    const btn = $("#cfSave", body);
    btn.disabled = true;
    try {
      const saved = await api.saveConnector(payload);
      toast("success", `Connector ${saved.name} saved`, saved.environment === "prod" ? "Production connector: not given to tasks by default." : "");
      m.close();
      onSaved && onSaved(saved);
    } catch (e) { err.textContent = e.message; err.hidden = false; btn.disabled = false; }
  };
}

function resultHtml(r) {
  if (!r) return "";
  const cls = r.call_status === "refused" ? "amber" : r.ok ? "green" : "red";
  return `<div class="conn-result ${cls}"><div class="row between"><strong>${r.ok ? icon("check", "sm") : icon("alert", "sm")} ${esc(r.summary || (r.ok ? "OK" : "Failed"))}</strong><span class="muted">${r.duration ? `${Number(r.duration).toFixed(2)} s` : ""}</span></div>${r.output ? `<pre>${esc(String(r.output).slice(0, 3000))}</pre>` : ""}</div>`;
}

export function mountConnectors(body) {
  let data = { connectors: [], docker: {} }, calls = [];
  const results = {};
  const load = async () => {
    try { [data, calls] = await Promise.all([api.connectors(), api.connectorCalls().then((r) => r.calls)]); } catch (e) { toast("error", "Could not load connectors", e.message); }
    draw();
  };
  const draw = () => {
    const rows = data.connectors || [];
    const needsDocker = rows.some((c) => c.type === "docker" || (c.type === "logs" && c.config.source !== "loki"));
    body.innerHTML = `<div class="card"><div class="card-head"><div><h3>Connectors</h3><p class="card-sub">Let agents check the real API, database, logs and running app behind the code, instead of guessing. Relay performs each call and records it in the task.</p></div><button type="button" class="btn sm primary" id="cnAdd">${icon("plus")}Add connector</button></div>
      <div class="card-body">
        ${needsDocker && !data.docker?.available ? `<div class="modal-error" style="margin-bottom:12px">${icon("alert", "sm")} ${esc(data.docker?.message || "Docker socket unavailable")}</div>` : ""}
        ${rows.length ? `<div class="conn-list">${rows.map((c) => `<div class="conn-item" data-name="${esc(c.name)}">
            <div class="conn-main">
              <span class="conn-ic ${esc(c.type)}">${icon(TYPE_ICON[c.type] || "zap")}</span>
              <div class="conn-text">
                <div class="row wrap" style="gap:6px"><strong class="mono-strong">${esc(c.name)}</strong><span class="badge outline">${esc(TYPE_LABEL[c.type] || c.type)}</span>${envBadge(c.environment)}${accessBadge(c.access)}${c.environment === "prod" && c.access === "write" && c.prod_write_enabled ? '<span class="badge red">prod writes on</span>' : ""}</div>
                <div class="conn-target mono truncate" title="${esc(c.target)}">${esc(c.target || "not configured")}</div>
                ${c.description ? `<div class="muted conn-desc">${esc(c.description)}</div>` : ""}
                ${(c.repos || []).length ? `<div class="row wrap conn-links">${c.repos.map((r) => `<span class="badge outline" title="${esc(r)}">${icon("folder", "sm")}${esc(basename(r))}</span>`).join("")}</div>` : ""}
              </div>
            </div>
            <div class="conn-actions"><button type="button" class="btn sm" data-test>${icon("activity")}<span>Test</span></button><button type="button" class="btn sm" data-edit>${icon("edit")}<span>Edit</span></button><button type="button" class="btn sm danger" data-del title="Delete">${icon("trash")}</button></div>
            <div class="conn-res" ${results[c.name] ? "" : "hidden"}>${resultHtml(results[c.name])}</div>
          </div>`).join("")}</div>`
          : `<div class="empty small">${icon("zap", "lg")}<p>No connectors yet. Add one for the API, database or app your repositories talk to, starting with a read-only dev or staging environment.</p></div>`}
      </div></div>
      <div class="card"><div class="card-head"><div><h3>Recent calls</h3><p class="card-sub">Every agent call and test, newest first.</p></div><button type="button" class="btn sm" id="cnRefresh">${icon("refresh")}Refresh</button></div><div class="card-body">
        ${calls.length ? `<div class="conn-calls">${calls.slice(0, 40).map((x) => `<div class="conn-call ${esc(x.status || "")}">
            <span class="badge ${x.status === "ok" ? "green" : x.status === "refused" ? "amber" : "red"}">${esc(x.status || "?")}</span>
            <span class="mono-strong truncate">${esc(x.connector)}</span>
            <span class="mono truncate conn-op" title="${esc(x.operation)}">${esc(x.operation)}</span>
            <span class="muted conn-when">${x.task ? `<a href="#/task/${esc(x.task)}">task</a> · ` : x.source === "settings" ? "test · " : ""}${esc(timeAgo(x.time))}</span>
          </div>`).join("")}</div>` : '<div class="empty small"><p>No calls yet.</p></div>'}
      </div></div>`;
    $("#cnAdd", body).onclick = () => openConnectorEditor(null, { defaults: data.defaults, onSaved: load });
    $("#cnRefresh", body).onclick = load;
    $$(".conn-item", body).forEach((row) => {
      const c = rows.find((x) => x.name === row.dataset.name);
      $("[data-edit]", row).onclick = () => openConnectorEditor(c, { defaults: data.defaults, onSaved: load });
      $("[data-del]", row).onclick = async () => {
        if (!(await confirm(`Delete ${c.name}?`, "Tasks that use it lose access immediately.", { danger: true, okLabel: "Delete" }))) return;
        try { await api.deleteConnector(c.name); load(); } catch (e) { toast("error", "Could not delete", e.message); }
      };
      $("[data-test]", row).onclick = async (e) => {
        const btn = e.currentTarget; btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}<span>Testing…</span>`;
        try { results[c.name] = await api.testConnector(c.name); } catch (err) { results[c.name] = { ok: false, summary: "Test failed", output: err.message }; }
        try { calls = (await api.connectorCalls()).calls; } catch {}
        draw();
      };
    });
  };
  body.innerHTML = skeleton("cards", 3);
  load();
}

// Checkbox list of connectors for a task or a repository's defaults. Returns { value() } (names).
export function connectorPicker(host, { connectors, selected, onChange }) {
  const set = new Set(selected || []);
  if (!connectors.length) {
    host.innerHTML = `<div class="help">No connectors yet. Add them in <a href="#/settings/connectors" data-close>Settings → Connectors</a> so agents can check the real services.</div>`;
    return { value: () => [...set] };
  }
  host.innerHTML = `<div class="conn-pick">${connectors.map((c) => `<label class="conn-pick-row" title="${esc(c.target || "")}">
      <input type="checkbox" value="${esc(c.name)}" ${set.has(c.name) ? "checked" : ""}>
      <span class="mono-strong truncate">${esc(c.name)}</span><span class="badge outline">${esc(TYPE_LABEL[c.type] || c.type)}</span>${envBadge(c.environment)}${accessBadge(c.access)}
    </label>`).join("")}</div>`;
  $$("input", host).forEach((i) => (i.onchange = async () => {
    const c = connectors.find((x) => x.name === i.value);
    if (i.checked && c.environment === "prod" && !(await confirm(`Give this task ${c.name}?`, `It points at production${c.access === "write" ? " with write access" : ""}.`, { danger: c.access === "write", okLabel: "Use it" }))) { i.checked = false; return; }
    i.checked ? set.add(i.value) : set.delete(i.value);
    onChange && onChange([...set]);
  }));
  return { value: () => [...set] };
}

// Repositories → Environment: which connectors tasks in this repository get by default. Saves on change.
export async function mountRepoConnectors(host, repo) {
  if (!host) return;
  host.innerHTML = `<h3>Connectors</h3><p class="hint">Real environments the agents of new tasks in this repository may check through Relay. Each task can still change this in the New task wizard.</p><div id="rcBody" class="muted">Loading…</div>`;
  let r;
  try { r = await api.connectorsForRepo(repo.path); } catch (e) { $("#rcBody", host).textContent = `Could not load connectors: ${e.message}`; return; }
  const draw = () => {
    const auto = r.saved === null || r.saved === undefined;
    const box = $("#rcBody", host);
    box.className = "";
    box.innerHTML = `<label class="row" style="gap:6px;margin-bottom:8px"><input type="checkbox" id="rcAuto" ${auto ? "checked" : ""}> Automatic: every non-production connector linked to this repository${auto ? ` <span class="muted">(${esc(r.defaults.join(", ") || "none")})</span>` : ""}</label>
      <div id="rcPick" ${auto ? "hidden" : ""}></div><div class="help" id="rcState"></div>`;
    const save = async (names) => {
      try { const res = await api.saveConnectorDefaults(repo.path, names); r.saved = res.saved; r.defaults = res.defaults; $("#rcState", host).textContent = "Saved"; }
      catch (e) { $("#rcState", host).textContent = `Could not save: ${e.message}`; }
    };
    connectorPicker($("#rcPick", host), { connectors: r.connectors, selected: auto ? r.defaults : r.saved, onChange: (names) => save(names) });
    $("#rcAuto", host).onchange = async (e) => { await save(e.target.checked ? null : r.defaults); draw(); };
  };
  draw();
}
