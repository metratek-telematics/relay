// Integration stacks: the editor (Repositories → Environment) and the task page's Stack card.
import { $, $$, esc, icon, toast, modal, confirm, fmtDur, basename } from "../ui.js";
import { api, get, post, del } from "../api.js";

const MASK = "●●●●";
const secretName = (n) => /PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|AUTH|COOKIE|TOTP|PRIVATE/i.test(n || "");
export const stackApi = {
  list: (force) => get(`/api/stacks${force ? "?force=1" : ""}`),
  save: (d) => post("/api/stacks", d),
  remove: (id) => del(`/api/stacks/${encodeURIComponent(id)}`),
  importCompose: (payload) => post("/api/stacks/import", payload),
  task: (id) => get(`/api/tasks/${encodeURIComponent(id)}/stack`),
  action: (id, action, body = {}) => post(`/api/tasks/${encodeURIComponent(id)}/stack/${action}`, body),
  logs: (id, svc, tail = 300) => get(`/api/tasks/${encodeURIComponent(id)}/stack/logs/${encodeURIComponent(svc)}?tail=${tail}`),
};

export function dockerNote(docker) {
  if (!docker) return "";
  return docker.available
    ? `<div class="stk-note ok">${icon("check", "sm")}<span>Docker ${esc(docker.server)} is reachable${docker.self_container ? "; Relay joins each stack's network" : ""}.</span></div>`
    : `<div class="stk-note warn">${icon("alert", "sm")}<span>${esc(docker.reason || "Docker is not available.")} Stacks can be defined, but tasks record their end-to-end checks as blocked until Docker is reachable.</span></div>`;
}

// ---------------------------------------------------------------------------- editor
export async function openStackEditor(stack, { onSaved, repoPath } = {}) {
  let repos = [], docker = null;
  try { const [r, s] = await Promise.all([api.repos(), stackApi.list()]); repos = r.repos || []; docker = s.docker; } catch (e) { toast("error", "Could not load repositories", e.message); }
  let d = stack ? structuredClone(stack) : { name: repoPath ? basename(repoPath) : "", description: "", vars: [], services: [], fixtures: [], checks: [] };
  if (!d.services.length) d.services.push(blankService(repoPath));

  const repoOptions = (sel) => `<option value="">— image, no repository —</option>` + repos.map((r) => `<option value="${esc(r.path)}" ${r.path === sel ? "selected" : ""}>${esc(r.name)}</option>`).join("")
    + (sel && !repos.some((r) => r.path === sel) ? `<option value="${esc(sel)}" selected>${esc(sel)}</option>` : "");
  const envRow = (v = { name: "", value: "", secret: false }) => `<div class="renv-var">
      <input data-en value="${esc(v.name)}" placeholder="NAME" spellcheck="false" autocomplete="off">
      <input data-ev type="${v.secret ? "password" : "text"}" value="${esc(v.secret && v.value === MASK ? "" : v.value)}" placeholder="${v.secret && v.has_value ? "saved (hidden)" : "value, \${OTHER} allowed"}" spellcheck="false" autocomplete="new-password" data-had="${v.secret && v.has_value ? "1" : ""}">
      <label class="renv-secret"><input type="checkbox" data-es ${v.secret ? "checked" : ""}>secret</label>
      <button class="btn xs ghost" data-del title="Remove">${icon("x", "sm")}</button></div>`;
  const svcBlock = (s) => {
    const hc = s.healthcheck || {};
    return `<div class="stk-svc" data-svc>
      <div class="stk-svc-head"><input data-s="name" value="${esc(s.name)}" placeholder="service name" spellcheck="false" aria-label="Service name">
        <select data-s="role" aria-label="Role"><option value="service">service</option><option value="simulator" ${s.role === "simulator" ? "selected" : ""}>simulator</option></select>
        <button class="btn xs ghost" data-del title="Remove service">${icon("x", "sm")}</button></div>
      <div class="stk-grid">
        <div class="field"><label>Build from repository</label><select data-s="repo">${repoOptions(s.repo)}</select></div>
        <div class="field" data-when="image"><label>Image</label><input data-s="image" value="${esc(s.image || "")}" placeholder="postgres:16-alpine" spellcheck="false"></div>
        <div class="field" data-when="repo"><label>Build context</label><input data-s="context" value="${esc(s.build?.context || ".")}" spellcheck="false"></div>
        <div class="field" data-when="repo"><label>Dockerfile</label><input data-s="dockerfile" value="${esc(s.build?.dockerfile || "Dockerfile")}" spellcheck="false"></div>
        <div class="field"><label>Command <span class="muted">(optional)</span></label><input data-s="command" value="${esc(s.command || "")}" placeholder="image default" spellcheck="false"></div>
        <div class="field"><label>Container ports</label><input data-s="ports" value="${esc((s.ports || []).join(", "))}" placeholder="8000" spellcheck="false"></div>
        <div class="field"><label>Health: HTTP path</label><input data-s="hhttp" value="${esc(hc.http || "")}" placeholder="/health" spellcheck="false"></div>
        <div class="field"><label>or command in the container</label><input data-s="hcmd" value="${esc(hc.command || "")}" placeholder="pg_isready -U postgres" spellcheck="false"></div>
        <div class="field"><label>Health timeout (s)</label><input data-s="htimeout" type="number" min="5" value="${esc(hc.timeout || 120)}"></div>
        <div class="field"><label>Depends on</label><input data-s="depends" value="${esc((s.depends_on || []).join(", "))}" placeholder="db, pricing" spellcheck="false"></div>
        <div class="field"><label>Memory limit</label><input data-s="memory" value="${esc(s.memory || "")}" placeholder="default from settings" spellcheck="false"></div>
        <label class="row stk-check" style="gap:6px"><input type="checkbox" data-s="envrepo" ${s.env_from_repo !== false ? "checked" : ""}>Pass the repository's environment variables</label>
      </div>
      <div class="row between"><span class="field-label" style="margin:0">Environment</span><button class="btn xs" data-add-env>${icon("plus", "sm")}Add</button></div>
      <div class="stack" data-envs style="gap:6px">${(s.env || []).map(envRow).join("")}</div>
    </div>`;
  };
  const svcNames = () => $$("[data-svc]", m.body).map((b) => $('[data-s="name"]', b).value.trim().toLowerCase()).filter(Boolean);
  const svcSelect = (sel) => `<select data-k="service">${svcNames().map((n) => `<option ${n === sel ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`;
  const checkRow = (c = { kind: "http", required: true, path: "/", expect_status: 200 }) => `<div class="stk-row" data-check>
      <input data-k="name" value="${esc(c.name || "")}" placeholder="what it proves" spellcheck="false">
      <select data-k="kind">${["http", "command", "browser"].map((k) => `<option ${c.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select>
      <span data-kind="command"><input data-k="command" value="${esc(c.command || "")}" placeholder="./e2e.sh (STACK_*_URL are set)" spellcheck="false"></span>
      <span data-kind="target" class="stk-target">${svcSelect(c.service)}<input data-k="method" value="${esc(c.method || "GET")}" title="method" style="width:62px"><input data-k="path" value="${esc(c.path || "/")}" placeholder="/path" spellcheck="false"><input data-k="expect_status" type="number" value="${esc(c.expect_status || 200)}" title="expected status" style="width:64px"><input data-k="expect_text" value="${esc(c.expect_text || "")}" placeholder="response contains" spellcheck="false"><input data-k="body" value="${esc(c.body || "")}" placeholder="JSON body" spellcheck="false"></span>
      <label class="renv-secret"><input type="checkbox" data-k="required" ${c.required !== false ? "checked" : ""}>required</label>
      <button class="btn xs ghost" data-del title="Remove">${icon("x", "sm")}</button></div>`;
  const fixtureRow = (f = {}) => `<div class="stk-row" data-fixture>
      <input data-k="name" value="${esc(f.name || "")}" placeholder="seed data" spellcheck="false">${svcSelect(f.service)}
      <input data-k="command" value="${esc(f.command || "")}" placeholder="psql -U postgres" spellcheck="false">
      <textarea data-k="stdin" rows="2" placeholder="stdin, e.g. SQL" spellcheck="false">${esc(f.stdin || "")}</textarea>
      <button class="btn xs ghost" data-del title="Remove">${icon("x", "sm")}</button></div>`;

  const m = modal(`<h2>${icon("layers")} Integration stack</h2>
    <p class="hint">The services a task needs running together. Services with a repository are built from the task's branch of that repository, so changes across services are tested together. End-to-end checks run at verification.</p>
    ${dockerNote(docker)}
    <div class="grid2"><div class="field"><label>Name</label><input id="skName" value="${esc(d.name)}" spellcheck="false"></div>
      <div class="field"><label>Description</label><input id="skDesc" value="${esc(d.description || "")}"></div></div>
    <div class="renv-sec"><div class="row between"><h3>Services</h3><span class="row" style="gap:6px"><button class="btn xs" id="skImport">${icon("download", "sm")}Import docker compose</button><button class="btn xs" id="skAddSvc">${icon("plus", "sm")}Add service</button></span></div>
      <p class="hint">Inside the stack, services reach each other by name, e.g. <code>http://pricing:8000</code>; <code>\${STACK_PRICING_INTERNAL_URL}</code> works in env values.</p>
      <div id="skSvcs" class="stack" style="gap:10px">${d.services.map(svcBlock).join("")}</div></div>
    <div class="renv-sec"><div class="row between"><h3>Stack variables</h3><button class="btn xs" id="skAddVar">${icon("plus", "sm")}Add</button></div>
      <p class="hint">Shared values for service env (<code>\${NAME}</code>). Secrets are masked in logs and hidden here after saving.</p>
      <div id="skVars" class="stack" style="gap:6px">${(d.vars || []).map(envRow).join("")}</div></div>
    <div class="renv-sec"><div class="row between"><h3>Fixtures and seed data</h3><button class="btn xs" id="skAddFx">${icon("plus", "sm")}Add</button></div>
      <p class="hint">Commands run inside a service after it starts (seed SQL, a fake-device simulator's scenario).</p>
      <div id="skFx" class="stack" style="gap:6px"></div></div>
    <div class="renv-sec"><div class="row between"><h3>End-to-end checks</h3><button class="btn xs" id="skAddCheck">${icon("plus", "sm")}Add check</button></div>
      <p class="hint">What proves the services work together. HTTP checks call a service; command checks run in the worktree with <code>STACK_&lt;SERVICE&gt;_URL</code> set; browser checks load a page. Required checks block delivery.</p>
      <div id="skChecks" class="stack" style="gap:6px"></div></div>
    <div class="modal-actions">${d.id ? `<button class="btn danger" id="skDelete" style="margin-right:auto">${icon("trash", "sm")}Delete</button>` : ""}<button class="btn" data-close>Cancel</button><button class="btn primary" id="skSave">Save stack</button></div>`, { wide: true });

  const bind = () => {
    $$("[data-del]", m.body).forEach((b) => (b.onclick = () => b.closest("[data-svc],.renv-var,[data-check],[data-fixture]").remove()));
    $$("[data-svc]", m.body).forEach((blk) => {
      const repo = $('[data-s="repo"]', blk);
      const sync = () => $$("[data-when]", blk).forEach((f) => (f.hidden = (f.dataset.when === "repo") !== !!repo.value));
      repo.onchange = sync; sync();
      $("[data-add-env]", blk).onclick = () => { $("[data-envs]", blk).insertAdjacentHTML("beforeend", envRow()); bind(); };
    });
    $$(".renv-var", m.body).forEach((r) => {
      const n = $("[data-en]", r), v = $("[data-ev]", r), s = $("[data-es]", r);
      s.onchange = () => { v.type = s.checked ? "password" : "text"; };
      n.onblur = () => { if (!n.dataset.touched && secretName(n.value) && !s.checked) { s.checked = true; v.type = "password"; } n.dataset.touched = "1"; };
    });
    $$("[data-check]", m.body).forEach((r) => {
      const kind = $('[data-k="kind"]', r);
      const sync = () => { $('[data-kind="command"]', r).hidden = kind.value !== "command"; $('[data-kind="target"]', r).hidden = kind.value === "command"; $$('[data-k="method"],[data-k="expect_status"],[data-k="body"]', r).forEach((x) => (x.hidden = kind.value !== "http")); };
      kind.onchange = sync; sync();
    });
  };
  const add = (id, html) => { $(id, m.body).insertAdjacentHTML("beforeend", html); bind(); };
  // Checks and fixtures pick services by name, so they render once the services exist.
  $("#skFx", m.body).innerHTML = (d.fixtures || []).map(fixtureRow).join("");
  $("#skChecks", m.body).innerHTML = (d.checks || []).map(checkRow).join("");
  bind();
  $("#skAddSvc", m.body).onclick = () => add("#skSvcs", svcBlock(blankService()));
  $("#skAddVar", m.body).onclick = () => add("#skVars", envRow());
  $("#skAddFx", m.body).onclick = () => add("#skFx", fixtureRow());
  $("#skAddCheck", m.body).onclick = () => add("#skChecks", checkRow());

  const readEnv = (host) => $$(":scope > .renv-var", host).map((r) => {
    const v = { name: $("[data-en]", r).value.trim(), value: $("[data-ev]", r).value, secret: $("[data-es]", r).checked };
    return $("[data-ev]", r).dataset.had && v.secret && v.value === "" ? { ...v, value: MASK } : v;
  }).filter((v) => v.name);
  const list = (s) => s.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
  const collect = () => ({
    id: d.id, name: $("#skName", m.body).value.trim(), description: $("#skDesc", m.body).value.trim(),
    vars: readEnv($("#skVars", m.body)),
    services: $$("[data-svc]", m.body).map((b) => {
      const v = (k) => $(`[data-s="${k}"]`, b).value.trim();
      return { name: v("name"), role: v("role"), repo: v("repo"), image: v("repo") ? "" : v("image"), build: { context: v("context") || ".", dockerfile: v("dockerfile") || "Dockerfile" },
        command: v("command"), ports: list(v("ports")), healthcheck: { http: v("hhttp"), command: v("hcmd"), timeout: Number(v("htimeout")) || 120 },
        depends_on: list(v("depends")), memory: v("memory"), env_from_repo: $('[data-s="envrepo"]', b).checked, env: readEnv($("[data-envs]", b)) };
    }),
    fixtures: $$("[data-fixture]", m.body).map((r) => ({ name: $('[data-k="name"]', r).value.trim(), service: $('[data-k="service"]', r)?.value || "", command: $('[data-k="command"]', r).value.trim(), stdin: $('[data-k="stdin"]', r).value })),
    checks: $$("[data-check]", m.body).map((r) => {
      const v = (k) => ($(`[data-k="${k}"]`, r)?.value ?? "").trim();
      return { name: v("name"), kind: v("kind"), required: $('[data-k="required"]', r).checked, command: v("command"), service: v("service"), method: v("method"), path: v("path"), expect_status: Number(v("expect_status")) || 200, expect_text: v("expect_text"), body: v("body") };
    }),
  });

  $("#skImport", m.body).onclick = () => {
    const p = modal(`<h2>Import docker compose</h2><p class="hint">Services, ports, environment, health checks and dependencies are read from the file. Services with a <code>build:</code> are built from the repository you choose; pick another repository per service afterwards. Volumes are not imported.</p>
      <div class="grid2"><div class="field"><label>Repository</label><select id="icRepo">${repos.map((r) => `<option value="${esc(r.path)}" ${r.path === repoPath ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></div>
      <div class="field"><label>File in the repository</label><input id="icFile" value="docker-compose.yml" spellcheck="false"></div></div>
      <div class="field"><label>…or paste the file</label><textarea id="icText" rows="8" spellcheck="false" placeholder="services:&#10;  api:&#10;    build: ."></textarea></div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="icGo">Import</button></div>`, { wide: true });
    $("#icGo", p.body).onclick = async () => {
      try {
        const draft = await stackApi.importCompose({ repo: $("#icRepo", p.body).value, file: $("#icFile", p.body).value, text: $("#icText", p.body).value });
        $("#skSvcs", m.body).innerHTML = draft.services.map(svcBlock).join("");
        if (!$("#skName", m.body).value.trim()) $("#skName", m.body).value = draft.name;
        bind();
        toast(draft.warnings.length ? "warning" : "success", `Imported ${draft.services.length} service(s)`, draft.warnings.length ? draft.warnings.join(" · ") : "Check the repositories and health checks, then save.", { duration: 12000 });
        p.close();
      } catch (e) { toast("error", "Could not import", e.message); }
    };
  };
  if (d.id) $("#skDelete", m.body).onclick = async () => {
    if (!(await confirm("Delete this stack?", "Repositories that use it stop starting a stack for new tasks. Running stacks are not affected.", { danger: true, okLabel: "Delete" }))) return;
    try { await stackApi.remove(d.id); toast("info", "Stack deleted"); m.close(); onSaved && onSaved(null); } catch (e) { toast("error", "Could not delete", e.message); }
  };
  $("#skSave", m.body).onclick = async () => {
    const btn = $("#skSave", m.body);
    btn.disabled = true;
    try {
      const saved = await stackApi.save(collect());
      toast("success", `Stack ${saved.name} saved`);
      m.close();
      onSaved && onSaved(saved);
    } catch (e) { toast("error", "Could not save the stack", e.message); btn.disabled = false; }
  };
}

function blankService(repoPath = "") {
  return { name: "", role: "service", repo: repoPath || "", image: "", build: { context: ".", dockerfile: "Dockerfile" }, command: "", ports: [], env: [], env_from_repo: true, healthcheck: { http: "", command: "", timeout: 120 }, depends_on: [], memory: "" };
}

// ---------------------------------------------------------------------------- task card
const lastView = new Map(); // task id -> last stack view, so a re-render does not flash a loading state
const STATUS_TONE = { healthy: "green", up: "green", starting: "amber", unhealthy: "red", failed: "red", exited: "red", stopped: "", down: "" };

export function stackCardHtml(t) {
  if (!t.stack) return "";
  return `<div class="card" id="stackCard"><div class="card-head"><h3>Stack</h3><span class="muted" style="font-size:11px">${esc(t.stack.name || "")}</span></div><div class="card-body" id="stackBody">${lastView.has(t.id) ? renderCard(t, lastView.get(t.id)) : '<div class="empty small">Loading the stack…</div>'}</div></div>`;
}

export function bindStackCard(root, t, { isCurrent }) {
  const body = $("#stackBody", root);
  if (!body) return () => {};
  let timer = null, stopped = false;
  const load = async () => {
    if (stopped || !isCurrent()) return;
    let v;
    try { v = await stackApi.task(t.id); } catch (e) { body.innerHTML = `<div class="empty small">${esc(e.message)}</div>`; return; }
    if (stopped || !isCurrent()) return;
    lastView.set(t.id, v);
    body.innerHTML = renderCard(t, v);
    wire(v);
    const busy = v.job?.running || v.state?.status === "starting";
    clearTimeout(timer);
    timer = setTimeout(load, busy ? 2000 : 15000);
  };
  const wire = (v) => {
    $$("[data-stk]", body).forEach((b) => (b.onclick = async () => {
      const action = b.dataset.stk, service = b.dataset.service || "";
      if (action === "down" && !(await confirm("Stop the stack?", "All containers of this task's stack are removed. Their logs are saved to the run folder.", { danger: true, okLabel: "Stop stack" }))) return;
      try { await stackApi.action(t.id, action, { service }); toast("info", { up: "Starting the stack", down: "Stopping the stack", restart: `Rebuilding ${service}`, check: "Running end-to-end checks" }[action]); load(); }
      catch (e) { toast("error", "Stack action failed", e.message); }
    }));
    $$("[data-logs]", body).forEach((b) => (b.onclick = () => openLogs(t, b.dataset.logs)));
  };
  if (lastView.has(t.id)) wire(lastView.get(t.id));
  load();
  return () => { stopped = true; clearTimeout(timer); };
}

function renderCard(t, v) {
  const st = v.state || {}, def = v.definition, job = v.job;
  const services = def ? def.services.map((s) => ({ ...s, live: (st.services || {})[s.name] || null })) : [];
  const up = ["up", "starting", "failed"].includes(st.status);
  const running = !!job?.running;
  const status = running ? `${job.action}…` : st.status || "not started";
  const tone = running ? "amber" : STATUS_TONE[st.status] ?? "";
  const checks = st.checks?.items || [];
  const summary = t.stack?.checks || [];
  const rows = services.map((s) => {
    const l = s.live, sst = up && l ? l.status : "stopped";
    return `<li class="stk-li">
      <div class="stk-li-main"><div class="stk-li-name"><span class="dot ${sst === "healthy" ? "on" : sst === "starting" ? "warn" : ["unhealthy", "exited"].includes(sst) ? "off" : ""}"></span>
        <strong>${esc(s.name)}</strong>${s.role === "simulator" ? '<span class="badge outline">simulator</span>' : ""}</div>
        <span class="stk-src truncate" title="${esc(l?.source || s.repo || s.image)}">${esc(s.repo ? basename(s.repo) : s.image)}${up && l?.source_kind ? ` · ${esc({ "task worktree": "worktree", "repository checkout": "checkout" }[l.source_kind] || l.source_kind)}` : ""}</span></div>
      <div class="stk-li-side"><span class="badge ${STATUS_TONE[sst] ?? ""}">${esc(sst)}</span>
        ${up && l?.url ? `<a class="btn xs" href="/api/tasks/${encodeURIComponent(t.id)}/stack/open/${encodeURIComponent(s.name)}/" target="_blank" rel="noopener" title="Open through Relay (${esc(l.host_url || l.url)} on the host)">${icon("external", "sm")}Open</a>` : ""}
        <button class="btn xs" data-logs="${esc(s.name)}" ${l ? "" : "disabled"} title="Logs">${icon("terminal", "sm")}Logs</button>
        ${up && l ? `<button class="btn xs ghost" data-stk="restart" data-service="${esc(s.name)}" ${running ? "disabled" : ""} title="Rebuild from the worktree and recreate">${icon("refresh", "sm")}</button>` : ""}</div>
      ${l?.error && up ? `<div class="stk-err">${esc(l.error)}</div>` : ""}
    </li>`;
  }).join("");
  const checkRows = (checks.length ? checks : summary).map((c) => `<li class="stk-check-row"><span class="badge ${c.passed ? "green" : c.required ? "red" : "amber"}">${c.passed ? "pass" : c.cannot_run ? "blocked" : "fail"}</span><span class="truncate" title="${esc(c.output || c.name)}">${esc(c.name)}</span><span class="muted">${c.required ? "" : "optional · "}${esc(c.kind)} · ${fmtDur(c.duration)}</span></li>`).join("");
  return `
    ${v.docker && !v.docker.available ? dockerNote(v.docker) : ""}
    <div class="row between wrap" style="gap:8px;margin-bottom:10px">
      <span class="row" style="gap:8px"><span class="badge ${tone}">${esc(status)}</span>${st.project && up ? `<span class="mono muted" style="font-size:11px">${esc(st.project)}</span>` : ""}${st.ready_seconds && st.status === "up" ? `<span class="muted" style="font-size:11px">ready in ${fmtDur(st.ready_seconds)}</span>` : ""}</span>
      <span class="row" style="gap:6px">
        ${up ? `<button class="btn xs" data-stk="check" ${running ? "disabled" : ""}>${icon("shield", "sm")}Run checks</button><button class="btn xs danger" data-stk="down" ${running ? "disabled" : ""}>${icon("stop", "sm")}Stop</button>`
          : `<button class="btn xs primary" data-stk="up" ${running || !v.docker?.available || !t.worktree ? "disabled" : ""} title="${t.worktree ? "Build from the task's worktree and start" : "Available once the task has a worktree"}">${icon("play", "sm")}Start stack</button>`}
      </span></div>
    ${st.error && st.status === "failed" ? `<div class="stk-err">${esc(st.error.split("\n").slice(0, 6).join("\n"))}</div>` : ""}
    ${job && (job.running || job.error) ? `<details class="stk-job" ${job.error ? "open" : ""}><summary>${job.running ? "Working" : "Last action failed"} · ${esc(job.action)}${job.service ? ` ${esc(job.service)}` : ""}</summary><pre class="pre">${esc((job.error ? job.error + "\n\n" : "") + (job.log || []).slice(-30).join("\n"))}</pre></details>` : ""}
    <ul class="stk-list">${rows || '<li class="muted">The stack definition was removed.</li>'}</ul>
    ${checkRows ? `<div class="section-title" style="margin:14px 0 6px">End-to-end checks${st.checks?.time || t.stack?.time ? ` <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">· ${esc(new Date(Date.parse(st.checks?.time || t.stack.time)).toLocaleTimeString())}</span>` : ""}</div><ul class="stk-checks">${checkRows}</ul>` : ""}
    ${st.status === "down" && st.stop_reason ? `<p class="muted" style="margin:10px 0 0;font-size:11.5px">Stopped: ${esc(st.stop_reason)}. Logs are kept in the run folder.</p>` : ""}`;
}

async function openLogs(t, service) {
  const m = modal(`<div class="row between"><h2>${icon("terminal")} ${esc(service)} logs</h2><span class="row" style="gap:6px"><button class="btn xs" id="lgRefresh">${icon("refresh", "sm")}Refresh</button></span></div>
    <p class="hint">Secret values are masked. The last 500 lines while the stack runs; the saved log after it stops.</p>
    <div class="log-view" id="lgView" style="max-height:60vh">Loading…</div>
    <div class="modal-actions"><button class="btn" data-close>Close</button></div>`, { wide: true });
  const load = async () => {
    try { const r = await stackApi.logs(t.id, service, 500); $("#lgView", m.body).textContent = r.text || "(no output)"; const v = $("#lgView", m.body); v.scrollTop = v.scrollHeight; }
    catch (e) { $("#lgView", m.body).textContent = e.message; }
  };
  $("#lgRefresh", m.body).onclick = load;
  load();
}
