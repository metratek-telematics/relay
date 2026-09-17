// Agents page: health, versions, smoke tests, and the agent pack (install, sign in, configure).
import { $, $$, esc, icon, toast, modal, confirm, copyText, menu } from "../ui.js";
import { S, agentLabel, agentInitial, agentIds } from "../state.js";
import { api } from "../api.js";

const isPack = (a) => !!(S.agentMeta[a] || {}).install;

function statusBadge(h, job) {
  if (job && job.state === "running") return `<span class="badge blue">${icon("spinner", "spin")}${job.action === "remove" ? "removing" : job.action === "update" ? "updating" : "installing"}</span>`;
  const signedOut = h.installed && h.signed_in === false;
  if (h.ok) return `<span class="badge green">ready</span>`;
  if (signedOut) return `<span class="badge amber" ${h.hint ? `title="${esc(h.hint)}"` : ""}>not signed in</span>`;
  if (h.installed) return `<span class="badge amber">check</span>`;
  return `<span class="badge ${isPack(h.agent) ? "" : "red"}">${isPack(h.agent) ? "not installed" : "missing"}</span>`;
}

export function agentHealthRow(name, h, { compact = false, job = null } = {}) {
  h = { agent: name, ...(h || {}) };
  const meta = S.agentMeta[name] || {};
  const signedOut = h.installed && h.signed_in === false;
  const pack = isPack(name);
  const busy = job && job.state === "running";
  const failed = job && job.state === "failed";
  const version = h.installed ? esc(h.version || h.path || "installed") : pack ? "Not installed. Install it here; it stays off until you sign in." : "not installed";
  const actions = compact ? "" : pack
    ? (h.installed
      ? `${signedOut ? `<button class="btn sm primary" data-signin="${esc(name)}" ${busy ? "disabled" : ""}>${icon("shield")}Sign in</button>` : ""}
         <button class="btn sm" data-configure="${esc(name)}">${icon("settings")}Configure</button>
         <button class="btn sm" data-test="${esc(name)}" ${busy ? "disabled" : ""}>${icon("zap")}Test</button>
         <button class="btn sm ghost" data-more="${esc(name)}" title="Update or remove" ${busy ? "disabled" : ""}>${icon("more")}</button>`
      : `<button class="btn sm primary" data-install="${esc(name)}" ${busy ? "disabled" : ""}>${icon("download")}Install</button>`)
    : `<button class="btn sm" data-test="${esc(name)}" ${h.installed ? "" : "disabled"}>${icon("zap")}Test</button>`;
  return `<div class="ah ${pack && !compact ? "ah-pack" : ""} ${pack && !h.installed ? "ah-off" : ""}" data-agent="${esc(name)}">
    <span class="av lg ${esc(name)}">${esc(agentInitial(name))}</span>
    <div class="who"><strong>${esc(agentLabel(name))} <span class="muted" style="font-weight:500">· ${esc(meta.vendor || "")}</span>${meta.edit_only ? ` <span class="badge" title="Edits files but cannot run commands or tests">edits only</span>` : ""}</strong>
      <span>${version}</span>
      ${h.error && !signedOut && h.installed ? `<span class="err">${esc(h.error)}</span>` : ""}${h.hint && !compact && h.installed ? `<span class="hint">${esc(h.hint)}</span>` : ""}
      ${failed ? `<span class="err">${esc(job.action)} failed: ${esc(job.error || "")} · <a href="#" data-log="${esc(name)}">show log</a></span>` : ""}
      ${h.test ? `<span class="${h.test.ok ? "" : "err"}">${h.test.ok ? `✓ smoke test passed in ${h.test.seconds}s${h.test.model ? ` · ${esc(h.test.model)}` : ""}` : `✗ smoke test failed: ${esc(h.test.error || "")}`}</span>` : ""}
    </div>
    <div class="row ah-actions">${statusBadge(h, job)}${actions}</div>
  </div>`;
}

function commandBlock(cmds) {
  return `<pre class="ho-cmds">${cmds.map((c) => `<span class="ho-line"><span>${esc(c)}</span><button class="btn xs ghost" data-copy="${esc(c)}" title="Copy">${icon("copy", "sm")}</button></span>`).join("")}</pre>`;
}
function bindCopy(root) { $$("[data-copy]", root).forEach((b) => (b.onclick = () => copyText(b.dataset.copy))); }

function openSignIn(name) {
  const meta = S.agentMeta[name] || {};
  const ide = (S.config || {}).ide_url;
  const keys = (meta.auth || {}).env || [];
  const m = modal(`<h2><span class="av sm ${esc(name)}">${esc(agentInitial(name))}</span> Sign in to ${esc(meta.label || name)}</h2>
    <p class="hint">Sign-ins are saved in Relay's home folder, which the browser VS Code shares, so a login done there is used by every task.</p>
    <h3>Option 1: log in from a terminal</h3>
    <p class="hint">${ide ? `Open <a href="${esc(ide)}" target="_blank" rel="noopener">VS Code</a>, then Terminal → New Terminal, and run:` : "On the server, run:"}</p>
    ${commandBlock(ide ? [meta.login] : [`docker exec -it relay ${meta.login}`])}
    ${ide ? `<p class="hint">Or on the server: <code>docker exec -it relay ${esc(meta.login)}</code></p>` : ""}
    ${keys.length ? `<h3>Option 2: use an API key</h3><p class="hint">Add one of <code>${keys.map(esc).join("</code>, <code>")}</code> under Configure. Keys are kept in Relay's settings on this server.</p>` : ""}
    ${meta.docs ? `<p class="hint"><a href="${esc(meta.docs)}" target="_blank" rel="noopener">${esc(meta.label)} documentation</a></p>` : ""}
    <div class="modal-actions">${keys.length ? `<button class="btn" id="goConfigure">${icon("settings")}Configure</button>` : ""}<button class="btn primary" data-close>Done</button></div>`);
  bindCopy(m.body);
  const go = $("#goConfigure", m.body);
  if (go) go.onclick = () => { m.close(); openConfigure(name); };
}

function openConfigure(name, onSaved) {
  const meta = S.agentMeta[name] || {};
  const cfg = S.config || {};
  const env = { ...((cfg.agent_env || {})[name] || {}) };
  const suggested = (meta.auth || {}).env || [];
  const rows = [...new Set([...suggested, ...Object.keys(env)])];
  const secret = (k) => /KEY|TOKEN|SECRET|PASSWORD/i.test(k);
  const row = (k, v = "") => `<div class="env-row"><input data-k value="${esc(k)}" placeholder="NAME" spellcheck="false"><input data-v type="${secret(k) ? "password" : "text"}" value="${esc(v)}" placeholder="${suggested.includes(k) ? "not set" : "value"}" spellcheck="false" autocomplete="off"><button class="btn xs ghost" data-del title="Remove">${icon("x", "sm")}</button></div>`;
  const models = (cfg.models || {})[name] || [];
  const def = ((cfg.agent_defaults || {})[name] || {}).model || "";
  const m = modal(`<h2><span class="av sm ${esc(name)}">${esc(agentInitial(name))}</span> Configure ${esc(meta.label || name)}</h2>
    <div class="field"><label>Default model</label><input id="cfgModel" list="cfgModels" value="${esc(def)}" placeholder="${esc(meta.models_hint || "CLI default")}"><datalist id="cfgModels">${models.map((x) => `<option value="${esc(x)}">`).join("")}</datalist>
      <span class="hint">Used when a team role leaves the model blank. ${esc(meta.models_hint || "")}</span></div>
    <div class="field"><label>Environment variables</label><div class="env-table" id="cfgEnv">${rows.map((k) => row(k, env[k] || "")).join("")}</div>
      <button class="btn xs" id="cfgAdd">${icon("plus", "sm")}Add variable</button>
      <span class="hint">Only set what you use; empty rows are ignored. Keys stay on this server.</span></div>
    <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="cfgSave">Save</button></div>`, { wide: true });
  const table = $("#cfgEnv", m.body);
  const bindRows = () => $$("[data-del]", table).forEach((b) => (b.onclick = () => b.closest(".env-row").remove()));
  bindRows();
  $("#cfgAdd", m.body).onclick = () => { table.insertAdjacentHTML("beforeend", row("")); bindRows(); $$("[data-k]", table).pop().focus(); };
  $("#cfgSave", m.body).onclick = async () => {
    const next = {};
    $$(".env-row", table).forEach((r) => { const k = $("[data-k]", r).value.trim(); const v = $("[data-v]", r).value; if (k && v !== "") next[k] = v; });
    // Settings merge per key, so a removed variable is saved as empty, which the agent treats as unset.
    for (const k of Object.keys(env)) if (!(k in next)) next[k] = "";
    try {
      S.config = await api.saveSettings({ agent_env: { [name]: next }, agent_defaults: { [name]: { model: $("#cfgModel", m.body).value.trim() } } });
      toast("success", `${meta.label || name} saved`);
      m.close();
      onSaved && onSaved();
    } catch (e) { toast("error", "Could not save", e.message); }
  };
}

export function mountAgents(main) {
  main.innerHTML = `<div class="page" id="agentsPage"><div class="empty small">Checking agents…</div></div>`;
  let alive = true;
  const tests = {};
  let jobs = {};
  let poll = null;
  async function render(force = false) {
    let h;
    try {
      const [a, j] = await Promise.all([api.agents(force), api.agentJobs().catch(() => ({}))]);
      h = a.health; S.agents = h; jobs = j || {};
    } catch (e) { $("#agentsPage", main).innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (!alive) return;
    for (const k of Object.keys(tests)) if (h[k]) h[k].test = tests[k];
    const ids = agentIds();
    const builtin = ids.filter((a) => !isPack(a));
    const pack = ids.filter(isPack);
    const installed = pack.filter((a) => h[a]?.installed).length;
    const missing = pack.filter((a) => !h[a]?.installed && jobs[a]?.state !== "running");
    const anyRunning = Object.values(jobs).some((j) => j.state === "running");
    const cfg = S.config || {};
    const logins = ids.filter((a) => (S.agentMeta[a] || {}).login).map((a) => `<code>${esc(S.agentMeta[a].login.split("   ")[0])}</code> (${esc(agentLabel(a))})`);
    $("#agentsPage", main).innerHTML = `
      <div class="page-head"><div><h1>Agents</h1><p>Relay runs these command-line agents on this server with their own sign-ins. Pack agents install from here and stay idle until you sign in and pick them for a team.</p></div>
        <div class="page-actions"><button class="btn" id="recheck">${icon("refresh")}Re-check</button><button class="btn" id="testAll">${icon("zap")}Test ready agents</button><a class="btn primary" href="#/settings/agents">${icon("settings")}Agent settings</a></div></div>
      <div class="dash-grid">
        <div class="stack" style="gap:16px">
          <div class="card"><div class="card-head"><h3>Core agents</h3></div><div class="card-body agent-health">${builtin.map((a) => agentHealthRow(a, h[a], { job: jobs[a] })).join("")}</div></div>
          <div class="card"><div class="card-head"><h3>Agent pack <span class="muted" style="font-weight:500">· ${installed} of ${pack.length} installed</span></h3>
            <button class="btn sm" id="installAll" ${missing.length ? "" : "disabled"}>${icon(anyRunning ? "spinner" : "download", anyRunning ? "spin" : "")}${missing.length ? `Install all (${missing.length})` : "All installed"}</button></div>
            <div class="card-body agent-health">${pack.map((a) => agentHealthRow(a, h[a], { job: jobs[a] })).join("")}</div></div>
          <div class="card"><div class="card-head"><h3>Tooling</h3></div><div class="card-body agent-health">
            <div class="ah"><span class="av lg git">G</span><div class="who"><strong>Git</strong><span>${esc(h.git?.path || "not found")}</span></div><span class="badge ${h.git?.ok ? "green" : "red"}">${h.git?.ok ? "ready" : "missing"}</span></div>
            <div class="ah"><span class="av lg github">GH</span><div class="who"><strong>GitHub CLI</strong><span>${esc(h.gh?.path || "not found")}${S.github?.login ? ` · signed in as @${esc(S.github.login)}` : ""}</span>${S.github?.error ? `<span class="err">${esc(S.github.error)}</span>` : ""}</div><span class="badge ${h.gh?.ok ? "green" : "amber"}">${h.gh?.ok ? "ready" : "optional"}</span></div>
          </div></div>
        </div>
        <div class="stack" style="gap:16px">
          <div class="card"><div class="card-head"><h3>Adding an agent</h3></div><div class="card-body hint stack">
            <div><strong>1. Install.</strong> Downloads the official CLI into Relay's data folder, so it survives restarts and image rebuilds.</div>
            <div><strong>2. Sign in.</strong> Run its login in the browser VS Code terminal, or add an API key under Configure.</div>
            <div><strong>3. Test.</strong> Sends a one-word prompt to prove the sign-in and the output parsing work.</div>
            <div><strong>4. Use it.</strong> Pick it for any role in a team preset or when creating a task.</div>
          </div></div>
          <div class="card"><div class="card-head"><h3>How sessions work</h3></div><div class="card-body hint stack">
            <div><strong>Persistent memory.</strong> Each role keeps its own CLI session and every message is a new turn on it, so the agent remembers the whole task. Agents without resume (${ids.filter((a) => (S.agentMeta[a] || {}).resume === false).map((a) => esc(agentLabel(a))).join(", ") || "none"}) get the context again each turn.</div>
            <div><strong>Full tool access.</strong> Agents run unattended with their own tools inside the task's worktree. Codex: <code>${esc(cfg.codex_sandbox || "workspace-write")}</code> sandbox · Claude: <code>${esc(cfg.claude_permission === "acceptEdits" ? "acceptEdits" : "skip permissions")}</code> · Gemini: <code>${esc(cfg.gemini_approval === "auto_edit" ? "auto_edit" : "yolo")}</code>.</div>
            <div><strong>Structured streaming.</strong> Each CLI's JSON output is parsed live into the conversation: text, tool calls, results, usage and cost.</div>
          </div></div>
          <div class="card"><div class="card-head"><h3>Sign-in commands</h3></div><div class="card-body hint">
            <div><code>codex</code> (ChatGPT sign-in) · <code>claude</code> then <code>/login</code> · <code>gemini</code> · ${logins.join(" · ")} · <code>gh auth login</code> for GitHub.</div>
          </div></div>
        </div>
      </div>`;
    $("#recheck", main).onclick = () => render(true);
    $$("[data-test]", main).forEach((b) => (b.onclick = () => runTest(b.dataset.test)));
    $$("[data-install]", main).forEach((b) => (b.onclick = () => runJob(b.dataset.install, "install")));
    $$("[data-signin]", main).forEach((b) => (b.onclick = () => openSignIn(b.dataset.signin)));
    $$("[data-configure]", main).forEach((b) => (b.onclick = () => openConfigure(b.dataset.configure, () => render(true))));
    $$("[data-log]", main).forEach((a) => (a.onclick = (e) => { e.preventDefault(); const j = jobs[a.dataset.log] || {}; modal(`<h2>${esc(agentLabel(a.dataset.log))}: ${esc(j.action || "")} log</h2><pre class="ho-cmds" style="max-height:60vh;overflow:auto;white-space:pre-wrap">${esc(j.log || "(empty)")}</pre><div class="modal-actions"><button class="btn primary" data-close>Close</button></div>`, { wide: true }); }));
    $$("[data-more]", main).forEach((b) => (b.onclick = () => {
      const a = b.dataset.more;
      menu(b, [
        { label: "Sign in", icon: "shield", onClick: () => openSignIn(a) },
        { label: "Update to latest", icon: "refresh", onClick: () => runJob(a, "update") },
        "-",
        { label: "Remove", icon: "trash", danger: true, onClick: async () => { if (await confirm(`Remove ${agentLabel(a)}?`, "Deletes the installed CLI. Its sign-in and settings are kept, so reinstalling picks them up again.", { danger: true, okLabel: "Remove" })) runJob(a, "remove"); } },
      ]);
    }));
    $("#installAll", main).onclick = async () => { for (const a of missing) await runJob(a, "install", { quiet: true }); };
    $("#testAll", main).onclick = async () => { for (const a of ids) if (h[a]?.ok) await runTest(a); };
    if (anyRunning && !poll) poll = setInterval(() => { if (alive) render(); }, 4000);
    if (!anyRunning && poll) { clearInterval(poll); poll = null; }
  }
  async function runJob(name, action, { quiet = false } = {}) {
    try {
      await api.installAgent(name, action);
      if (!quiet) toast("info", `${agentLabel(name)}: ${action === "remove" ? "removing" : action === "update" ? "updating" : "installing"}…`, "This runs in the background; you can leave this page.");
    } catch (e) { toast("error", `${agentLabel(name)} ${action} failed`, e.message); }
    render();
  }
  async function runTest(name) {
    const btn = $(`[data-test="${name}"]`, main);
    if (btn) { btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Testing…`; }
    try {
      const r = await api.testAgent(name);
      tests[name] = r;
      toast(r.ok ? "success" : "error", `${agentLabel(name)} ${r.ok ? "responded" : "failed"}`, r.ok ? `“${r.reply}” in ${r.seconds}s` : (r.error || "").slice(0, 200));
    } catch (e) { tests[name] = { ok: false, error: e.message }; toast("error", `${agentLabel(name)} test failed`, e.message); }
    render();
  }
  render();
  return { update(reason) { if (reason === "agents") render(true); }, destroy() { alive = false; if (poll) clearInterval(poll); } };
}
