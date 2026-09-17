// Personal access tokens: create (shown once), list with last use, revoke. Owners also see everyone's tokens.
import { $, $$, esc, icon, toast, confirm, modal, timeAgo, fmtDateTime } from "../../ui.js";
import { ORG, loadMe, orgApi, can, xicon, copyButton, bindCopy } from "./org.js";
import { request } from "../../api.js";

const SCOPE_TONE = { read: "", "tasks:write": "green", admin: "purple" };

export function mountTokens(body) {
  let alive = true, mine = [], scopes = [], all = null;

  async function load() {
    if (!ORG.me) await loadMe();
    try {
      const r = await orgApi.tokens();
      mine = r.tokens || []; scopes = r.scopes || [];
      all = can("owner") ? (await request("/api/org/tokens")).tokens || [] : null;
    } catch (e) { body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (alive) draw();
  }

  const state = (t) => t.revoked ? '<span class="badge red">revoked</span>' : t.expired ? '<span class="badge amber">expired</span>' : '<span class="badge green">active</span>';
  const rows = (list, showOwner) => list.length ? `<div class="org-table-wrap"><table class="org-table"><thead><tr><th>Name</th>${showOwner ? "<th>Owner</th>" : ""}<th>Scopes</th><th>Created</th><th>Expires</th><th>Last used</th><th class="num">Calls</th><th>Status</th><th></th></tr></thead><tbody>
    ${list.map((t) => `<tr><td><strong>${esc(t.name)}</strong><div class="muted mono" style="font-size:11px">${esc(t.prefix)}…</div></td>${showOwner ? `<td>${esc(t.username)}</td>` : ""}
      <td><div class="org-chips">${t.scopes.map((s) => `<span class="badge ${SCOPE_TONE[s] || ""}">${esc(s)}</span>`).join("")}</div></td>
      <td title="${esc(t.created_at)}">${esc(timeAgo(t.created_at))}</td><td>${t.expires_at ? esc(fmtDateTime(t.expires_at)) : '<span class="muted">never</span>'}</td>
      <td>${t.last_used_at ? `${esc(timeAgo(t.last_used_at))}<div class="muted" style="font-size:11px">${esc(t.last_used_ip || "")}</div>` : '<span class="muted">not yet</span>'}</td>
      <td class="num">${t.uses || 0}</td><td>${state(t)}</td>
      <td>${t.active ? `<button class="btn xs danger" data-revoke="${esc(t.id)}" data-name="${esc(t.name)}">${icon("x")}Revoke</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="org-empty" style="border:0;padding:26px"><span class="org-empty-art">${xicon("key")}</span><h2>No tokens yet</h2><p>Create one for a script, a CI job or another tool. It acts as you, limited to the scopes you choose.</p></div>`;

  function draw() {
    body.innerHTML = `
      <div class="page-head"><div><h1>API tokens</h1><p>Personal access tokens for the <a href="#/org/api">Relay REST API</a>. Relay stores only a hash; you see each token once.</p></div>
        <div class="page-actions"><a class="btn" href="#/org/api">${xicon("api")}API docs</a><button class="btn primary" id="tNew">${icon("plus")}New token</button></div></div>
      <div class="card"><div class="card-head"><h3>Your tokens</h3><span class="badge">${mine.filter((t) => t.active).length} active</span></div><div class="card-body" style="padding:6px 8px">${rows(mine, false)}</div></div>
      ${all ? `<div class="card"><div class="card-head"><div><h3>Everyone's tokens</h3><p class="card-sub">Owners can revoke any token, for example when someone leaves.</p></div></div><div class="card-body" style="padding:6px 8px">${rows(all.filter((t) => t.username !== ORG.me.user.username), true)}</div></div>` : ""}
      <div class="card"><div class="card-head"><h3>Good practice</h3></div><div class="card-body hint">
        One token per tool, named after it · the narrowest scope that works · an expiry for anything that is not a long-lived integration ·
        store it as a CI secret, never in a repository · revoke it the moment it might have leaked (the audit log shows every call it made that changed something).</div></div>`;
    $("#tNew", body).onclick = create;
    $$("[data-revoke]", body).forEach((b) => (b.onclick = async () => {
      if (!(await confirm(`Revoke “${b.dataset.name}”?`, "Anything using it stops working immediately. This cannot be undone.", { danger: true, okLabel: "Revoke" }))) return;
      try { await orgApi.revokeToken(b.dataset.revoke); toast("success", "Token revoked"); load(); } catch (e) { toast("error", "Could not revoke", e.message); }
    }));
  }

  function create() {
    const LEVEL = { viewer: 10, member: 20, admin: 30, owner: 40 };
    const mine = LEVEL[ORG.me.role] || 0;
    const m = modal(`<h2>New access token</h2><p class="hint">It acts as you (${esc(ORG.me.user.username)}), limited to these scopes.</p>
      <div class="field"><label>Name</label><input id="tkName" placeholder="GitHub Actions · acme/web"></div>
      <div class="field"><label>Scopes</label><div class="stack" style="gap:6px">${scopes.map((s) => `<label class="org-scope"><input type="checkbox" value="${esc(s.id)}" ${s.id === "read" ? "checked" : ""} ${(LEVEL[s.role] || 0) > mine ? "disabled" : ""}><b>${esc(s.id)}</b><span>${esc(s.help)}${(LEVEL[s.role] || 0) > mine ? ` Needs the ${esc(s.role)} role.` : ""}</span></label>`).join("")}</div></div>
      <div class="field"><label>Expires</label><select id="tkExp"><option value="7">in 7 days</option><option value="30">in 30 days</option><option value="90" selected>in 90 days</option><option value="365">in a year</option><option value="0">never</option></select></div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="tkGo">${icon("key")}Create token</button></div>`);
    $("#tkGo", m.body).onclick = async () => {
      const name = $("#tkName", m.body).value.trim();
      const sc = $$("input[type=checkbox]:checked", m.body).map((x) => x.value);
      try {
        const r = await orgApi.createToken({ name, scopes: sc, expires_days: Number($("#tkExp", m.body).value) || null });
        m.close();
        showOnce(r);
        load();
      } catch (e) { toast("error", "Could not create the token", e.message); }
    };
  }

  function showOnce(r) {
    const base = location.origin;
    const curl = `curl -s ${base}/api/v1/tasks?limit=5 \\\n  -H "Authorization: Bearer ${r.secret}"`;
    const m = modal(`<h2>${xicon("checkCircle")} Token created</h2><p class="hint">Copy it now. Relay keeps only a hash and cannot show it again.</p>
      <div class="org-secret"><code>${esc(r.secret)}</code>${copyButton(r.secret)}</div>
      <div class="field"><label>Try it</label><pre class="org-code">${esc(curl)}</pre></div>
      <div class="row wrap" style="gap:6px">${r.token.scopes.map((s) => `<span class="badge ${SCOPE_TONE[s] || ""}">${esc(s)}</span>`).join("")}<span class="muted" style="font-size:12px">${r.token.expires_at ? `expires ${esc(fmtDateTime(r.token.expires_at))}` : "never expires"}</span></div>
      <div class="modal-actions"><a class="btn" href="#/org/api" data-close>API docs</a><button class="btn primary" data-close>I copied it</button></div>`, { wide: true });
    bindCopy(m.body);
  }

  load();
  return { destroy() { alive = false; } };
}
