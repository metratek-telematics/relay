// Agents page: health, versions, smoke tests, per-agent runtime options.
import { $, $$, esc, icon, toast, fmtDur } from "../ui.js";
import { S, agentLabel, agentInitial, navigate } from "../state.js";
import { api } from "../api.js";

export function agentHealthRow(name, h, { compact = false } = {}) {
  h = h || {};
  const meta = S.agentMeta[name] || {};
  const ok = h.ok;
  return `<div class="ah" data-agent="${esc(name)}">
    <span class="av lg ${esc(name)}">${esc(agentInitial(name))}</span>
    <div class="who"><strong>${esc(agentLabel(name))} <span class="muted" style="font-weight:500">· ${esc(meta.vendor || "")}</span></strong>
      <span>${h.installed ? esc(h.version || h.path || "installed") : "not installed"}</span>
      ${h.error ? `<span class="err">${esc(h.error)}</span>` : ""}${h.hint && !compact ? `<span class="hint">${esc(h.hint)}</span>` : ""}
      ${h.test ? `<span class="${h.test.ok ? "" : "err"}">${h.test.ok ? `✓ smoke test passed in ${h.test.seconds}s${h.test.model ? ` · ${esc(h.test.model)}` : ""}` : `✗ smoke test failed: ${esc(h.test.error || "")}`}</span>` : ""}
    </div>
    <div class="row"><span class="badge ${ok ? "green" : h.installed ? "amber" : "red"}">${ok ? "ready" : h.installed ? "check" : "missing"}</span>${compact ? "" : `<button class="btn sm" data-test="${esc(name)}" ${h.installed ? "" : "disabled"}>${icon("zap")}Test</button>`}</div>
  </div>`;
}

export function mountAgents(main) {
  main.innerHTML = `<div class="page" id="agentsPage"><div class="empty small">Checking agents…</div></div>`;
  let alive = true;
  const tests = {};
  async function render(force = false) {
    let h;
    try { h = (await api.agents(force)).health; S.agents = h; } catch (e) { $("#agentsPage", main).innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (!alive) return;
    for (const k of Object.keys(tests)) if (h[k]) h[k].test = tests[k];
    const cfg = S.config || {};
    $("#agentsPage", main).innerHTML = `
      <div class="page-head"><div><h1>Agents</h1><p>Relay drives the CLIs already installed and signed in on this machine. Nothing is proxied through a custom API.</p></div>
        <div class="page-actions"><button class="btn" id="recheck">${icon("refresh")}Re-check</button><button class="btn" id="testAll">${icon("zap")}Test all</button><a class="btn primary" href="#/settings/agents">${icon("settings")}Agent settings</a></div></div>
      <div class="dash-grid">
        <div class="stack" style="gap:16px">
          <div class="card"><div class="card-head"><h3>Coding agents</h3></div><div class="card-body agent-health">${["codex", "claude", "gemini"].map((a) => agentHealthRow(a, h[a])).join("")}</div></div>
          <div class="card"><div class="card-head"><h3>Tooling</h3></div><div class="card-body agent-health">
            <div class="ah"><span class="av lg git">G</span><div class="who"><strong>Git</strong><span>${esc(h.git?.path || "not found")}</span></div><span class="badge ${h.git?.ok ? "green" : "red"}">${h.git?.ok ? "ready" : "missing"}</span></div>
            <div class="ah"><span class="av lg git">GH</span><div class="who"><strong>GitHub CLI</strong><span>${esc(h.gh?.path || "not found")}${S.github?.login ? ` · signed in as @${esc(S.github.login)}` : ""}</span>${S.github?.error ? `<span class="err">${esc(S.github.error)}</span>` : ""}</div><span class="badge ${h.gh?.ok ? "green" : "amber"}">${h.gh?.ok ? "ready" : "optional"}</span></div>
          </div></div>
        </div>
        <div class="stack" style="gap:16px">
          <div class="card"><div class="card-head"><h3>How sessions work</h3></div><div class="card-body hint stack">
            <div><strong>Persistent memory.</strong> Each role gets its own CLI session (Claude <code>--session-id/--resume</code>, Codex <code>exec resume</code>, Gemini <code>--resume</code>). Every message between agents is a new turn on that session, so each agent remembers the entire task.</div>
            <div><strong>Full tool access.</strong> Agents run with their native tools inside the isolated worktree: file reads/edits, shell, search, web, MCP servers you have configured for that CLI.</div>
            <div><strong>Structured streaming.</strong> Codex <code>--json</code>, Claude <code>stream-json</code> and Gemini <code>stream-json</code> events are parsed live into the conversation view: text, tool calls, results, usage and cost.</div>
            <div><strong>Unattended mode.</strong> Codex: <code>${esc(cfg.codex_sandbox || "workspace-write")}</code> sandbox · Claude: <code>${esc(cfg.claude_permission === "acceptEdits" ? "--permission-mode acceptEdits" : "--dangerously-skip-permissions")}</code> · Gemini: <code>${esc(cfg.gemini_approval === "auto_edit" ? "--approval-mode auto_edit" : "--yolo")}</code>. Change these under Settings → Agents.</div>
          </div></div>
          <div class="card"><div class="card-head"><h3>Sign-in cheatsheet</h3></div><div class="card-body hint">
            <div><code>codex</code> → follow the ChatGPT sign-in · <code>claude</code> → <code>/login</code> · <code>gemini</code> → choose Google login (Workspace accounts also need <code>GOOGLE_CLOUD_PROJECT</code>) · <code>gh auth login</code> for GitHub.</div>
          </div></div>
        </div>
      </div>`;
    $("#recheck", main).onclick = () => render(true);
    $$("[data-test]", main).forEach((b) => (b.onclick = () => runTest(b.dataset.test)));
    $("#testAll", main).onclick = async () => { for (const a of ["codex", "claude", "gemini"]) if (h[a]?.installed) await runTest(a); };
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
  return { update(reason) { if (reason === "agents") render(); }, destroy() { alive = false; } };
}
