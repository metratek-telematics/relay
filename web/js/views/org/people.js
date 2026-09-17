// People & roles: everyone who has access, their role and where it came from; sign-in trust and group mapping (owners); the permission matrix.
import { $, $$, esc, icon, toast, confirm, modal, timeAgo } from "../../ui.js";
import { ORG, loadMe, orgApi, can, xicon, avatar, roleBadge, money, readOnlyNote } from "./org.js";

const ROLE_HELP = {
  viewer: "Reads everything; changes only their own profile, notifications and read-only tokens.",
  member: "Creates, answers, approves and stops tasks; runs the queue; proposes lessons.",
  admin: "Also manages repositories, connectors, stacks, agents, projects, integrations and settings.",
  owner: "Also manages people, roles, sign-in and budgets.",
};
const SOURCE = { manual: "assigned", groups: "from groups", bootstrap: "first sign-in", env: "server", default: "default", local: "local" };

export function mountPeople(body) {
  let alive = true, data = null, org = null, matrix = null, showMatrix = false;

  async function load() {
    if (!ORG.me) await loadMe();
    try {
      data = await orgApi.users();
      org = can("admin") ? await orgApi.settings() : null;
      matrix = matrix || (await orgApi.matrix());
    } catch (e) { body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (alive) draw();
  }

  function draw() {
    const owner = can("owner"), me = ORG.me.user.username;
    const users = data.users || [];
    const counts = Object.fromEntries(data.roles.map((r) => [r, users.filter((u) => u.role === r && !u.disabled).length]));
    body.innerHTML = `
      <div class="page-head"><div><h1>People &amp; roles</h1><p>Everyone who has signed in to Relay (or was added ahead of time), with the role that decides what they can change.</p></div>
        <div class="page-actions">${owner ? `<button class="btn primary" id="uInvite">${icon("plus")}Add person</button>` : ""}</div></div>
      ${!data.identity_mode ? `<div class="org-note warn">${icon("alert", "sm")}<span><b>Local mode.</b> Relay is not told who people are, so every visitor acts as the owner. ${owner ? "Set the trusted proxy below to use your identity provider." : ""}</span></div>` : ""}
      <div class="org-kpis">${data.roles.slice().reverse().map((r) => `<div class="org-kpi"><span>${roleBadge(r)}</span><b>${counts[r] || 0}</b><small>${esc(ROLE_HELP[r])}</small></div>`).join("")}</div>
      <div class="card"><div class="card-head"><h3>People</h3><span class="badge">${users.length}</span></div><div class="card-body org-table-wrap" style="padding:4px 8px">
        <table class="org-table"><thead><tr><th>Person</th><th>Role</th><th class="org-hide-sm">Groups</th><th class="org-hide-sm">Last seen</th>${can("admin") ? '<th class="num org-hide-sm">Budget / month</th>' : ""}${owner ? "<th class=\"org-hide-sm\"></th>" : ""}</tr></thead><tbody>
        ${users.map((u) => `<tr style="${u.disabled ? "opacity:.55" : ""}">
          <td><div class="org-person">${avatar(u, 32)}<div class="min0"><strong class="truncate">${esc(u.name || u.username)}${u.username === me ? ' <span class="badge outline">you</span>' : ""}${u.disabled ? ' <span class="badge red">disabled</span>' : ""}</strong><span class="muted truncate">${esc(u.email || u.username)}<span class="org-show-sm"> · ${u.last_seen ? esc(timeAgo(u.last_seen)) : "never"}</span></span></div></div></td>
          <td>${owner && !u.local ? `<select class="org-select" data-role="${esc(u.username)}">${data.roles.map((r) => `<option ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}</select>` : roleBadge(u.role)}
            ${u.role_source ? `<div class="muted" style="font-size:11px;margin-top:2px">${esc(SOURCE[u.role_source] || u.role_source)}${owner && u.role_source === "manual" && (u.groups || []).length ? ` · <a href="#" data-regroup="${esc(u.username)}">use groups</a>` : ""}</div>` : ""}</td>
          <td class="org-hide-sm"><div class="org-chips">${(u.groups || []).slice(0, 4).map((g) => `<span class="org-chip">${esc(g)}</span>`).join("")}${(u.groups || []).length > 4 ? `<span class="org-chip">+${u.groups.length - 4}</span>` : ""}</div></td>
          <td class="org-hide-sm">${u.last_seen ? esc(timeAgo(u.last_seen)) : '<span class="muted">never</span>'}</td>
          ${can("admin") ? `<td class="num org-hide-sm">${owner ? `<input class="input" style="width:90px;height:28px;text-align:right" type="number" min="0" step="10" data-budget="${esc(u.username)}" value="${u.budget_monthly_usd || ""}" placeholder="none">` : u.budget_monthly_usd ? money(u.budget_monthly_usd) : "—"}</td>` : ""}
          ${owner ? `<td class="num org-hide-sm">${u.username !== me && !u.local ? `<button class="btn xs ghost" data-toggle="${esc(u.username)}" data-disabled="${u.disabled ? 1 : 0}">${u.disabled ? "Enable" : "Disable"}</button><button class="btn xs ghost" data-remove="${esc(u.username)}" title="Remove">${icon("trash")}</button>` : ""}</td>` : ""}
        </tr>`).join("")}</tbody></table></div></div>
      ${owner && org ? authCard(org) : can("owner") ? "" : readOnlyNote("owner", "Changing roles and sign-in")}
      <div class="card"><div class="card-head"><div><h3>What each role can do</h3><p class="card-sub">Enforced by the server on every request; tokens are further limited by their scopes.</p></div><button class="btn sm" id="uMatrix">${showMatrix ? "Hide" : "Show"} rules</button></div>
        ${showMatrix ? `<div class="card-body org-table-wrap"><table class="org-table"><thead><tr><th>Methods</th><th>Endpoint</th><th>Needs</th><th>What</th></tr></thead><tbody>
          ${matrix.rules.map((r) => `<tr><td class="mono" style="font-size:11px">${esc(r.methods.join(" "))}</td><td class="mono" style="font-size:11px;overflow-wrap:anywhere">${esc(r.path)}</td><td>${r.role === "self" ? '<span class="badge outline">yourself</span>' : r.role === "task_owner" ? '<span class="badge outline">task creator or admin</span>' : roleBadge(r.role)}</td><td>${esc(r.label)}</td></tr>`).join("")}</tbody></table></div>` : ""}</div>`;
    bind();
  }

  function authCard(o) {
    const a = o.auth;
    const groups = Object.entries(a.group_roles || {});
    return `<div class="card" id="authCard"><div class="card-head"><div><h3>Sign-in &amp; role mapping</h3><p class="card-sub">Relay believes identity headers (X-Authentik-Username, -Email, -Name, -Groups) only from these proxies.</p></div>
        <span class="org-chain ${o.identity_mode ? "ok" : "bad"}">${o.identity_mode ? xicon("lock", "sm") + " identity on" : "local mode"}</span></div>
      <div class="card-body">
        <div class="org-note ${o.your_ip_trusted ? "good" : ""}">${icon(o.your_ip_trusted ? "check" : "info", "sm")}<span>This request came from <code class="org-inline-code">${esc(o.your_ip)}</code>, which is ${o.your_ip_trusted ? "" : "<b>not</b> "}a trusted proxy.${o.identity_mode && !o.your_ip_trusted ? " Saving a list that excludes your proxy locks everyone out until the server's RELAY_TRUSTED_PROXIES is set." : ""}</span></div>
        <div class="grid2" style="margin-top:6px">
          <div class="field"><label>Trusted proxies (IPs or networks, one per line)</label><textarea id="aProx" rows="3" placeholder="172.18.0.0/16" ${a._env_trusted_proxies ? "disabled" : ""}>${esc((a.trusted_proxies || []).join("\n"))}</textarea>
            <div class="help">${a._env_trusted_proxies ? "Set by RELAY_TRUSTED_PROXIES on the server." : "Your reverse proxy's address as Relay sees it, for example the Docker network of Traefik."}</div></div>
          <div class="stack" style="gap:2px">
            <div class="field inline"><label>Create accounts on first sign-in</label><span class="switch ${a.auto_provision ? "on" : ""}" data-sw="auto_provision"></span></div>
            <div class="field inline"><label>First person to sign in becomes owner</label><span class="switch ${a.first_user_owner ? "on" : ""}" data-sw="first_user_owner"></span></div>
            <div class="field inline"><label>Trust identity headers from any address<div class="help">Only when nothing but the proxy can reach Relay.</div></label><span class="switch ${a.header_auth ? "on" : ""}" data-sw="header_auth" ${a._env_header_auth ? 'style="opacity:.5;pointer-events:none"' : ""}></span></div>
            <div class="field"><label>Role for people no group maps</label><select id="aDefault">${["viewer", "member", "admin"].map((r) => `<option ${a.default_role === r ? "selected" : ""}>${r}</option>`).join("")}</select></div>
          </div>
        </div>
        <div class="field"><label>Group → role</label><div class="stack" id="aGroups" style="gap:6px">${groups.map(([g, r]) => groupRow(g, r)).join("")}</div>
          <button class="btn xs" id="aAddGroup" style="margin-top:6px">${icon("plus")}Add group</button><div class="help">A person gets the highest role any of their groups maps to, unless an owner assigned a role by hand.</div></div>
        <div class="row" style="justify-content:flex-end"><button class="btn primary" id="aSave">${icon("save")}Save sign-in settings</button></div>
      </div></div>`;
  }
  const groupRow = (g = "", r = "member") => `<div class="row" style="gap:6px" data-grow><input class="input" style="flex:1" data-g value="${esc(g)}" placeholder="Authentik group name"><select class="org-select" data-r>${["viewer", "member", "admin", "owner"].map((x) => `<option ${x === r ? "selected" : ""}>${x}</option>`).join("")}</select><button class="btn xs ghost" data-grm>${icon("x")}</button></div>`;

  function bind() {
    const run = async (fn, ok) => { try { await fn(); ok && toast("success", ok); await load(); } catch (e) { toast("error", "Could not save", e.message); load(); } };
    $("#uMatrix", body).onclick = () => { showMatrix = !showMatrix; draw(); };
    $$("[data-role]", body).forEach((s) => (s.onchange = async () => {
      if (s.dataset.role === ORG.me.user.username && !(await confirm("Change your own role?", "You may lose access to this page.", { danger: true, okLabel: "Change" }))) return draw();
      run(() => orgApi.saveUser(s.dataset.role, { role: s.value }), `Role changed to ${s.value}`);
    }));
    $$("[data-regroup]", body).forEach((a) => (a.onclick = (e) => { e.preventDefault(); run(() => orgApi.saveUser(a.dataset.regroup, { role_source: "groups" }), "Role follows groups again"); }));
    $$("[data-budget]", body).forEach((i) => (i.onchange = () => run(() => orgApi.saveUser(i.dataset.budget, { budget_monthly_usd: Number(i.value) || 0 }), "Budget saved")));
    $$("[data-toggle]", body).forEach((b) => (b.onclick = () => run(() => orgApi.saveUser(b.dataset.toggle, { disabled: b.dataset.disabled !== "1" }), b.dataset.disabled === "1" ? "Enabled" : "Disabled")));
    $$("[data-remove]", body).forEach((b) => (b.onclick = async () => {
      if (!(await confirm(`Remove ${b.dataset.remove}?`, "Their tokens stop working. If they can still sign in through the identity provider, Relay creates the account again with the default role; disable instead to keep them out.", { danger: true, okLabel: "Remove" }))) return;
      run(() => orgApi.removeUser(b.dataset.remove), "Removed");
    }));
    $("#uInvite", body) && ($("#uInvite", body).onclick = () => {
      const m = modal(`<h2>Add a person</h2><p class="hint">Use their identity-provider username. The role applies from their first sign-in.</p>
        <div class="grid2"><div class="field"><label>Username</label><input id="iUser" placeholder="maria"></div><div class="field"><label>Role</label><select id="iRole">${["viewer", "member", "admin", "owner"].map((r) => `<option ${r === "member" ? "selected" : ""}>${r}</option>`).join("")}</select></div></div>
        <div class="grid2"><div class="field"><label>Name</label><input id="iName" placeholder="Maria K."></div><div class="field"><label>Email</label><input id="iEmail" placeholder="maria@company.com"></div></div>
        <p class="hint" id="iHelp">${esc(ROLE_HELP.member)}</p>
        <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="iGo">${icon("plus")}Add</button></div>`);
      $("#iRole", m.body).onchange = (e) => ($("#iHelp", m.body).textContent = ROLE_HELP[e.target.value]);
      $("#iGo", m.body).onclick = async () => {
        try { await orgApi.invite({ username: $("#iUser", m.body).value, role: $("#iRole", m.body).value, name: $("#iName", m.body).value, email: $("#iEmail", m.body).value }); m.close(); toast("success", "Person added"); load(); }
        catch (e) { toast("error", "Could not add", e.message); }
      };
    });
    const card = $("#authCard", body);
    if (!card) return;
    const flags = {};
    $$("[data-sw]", card).forEach((s) => (s.onclick = () => { s.classList.toggle("on"); flags[s.dataset.sw] = s.classList.contains("on"); }));
    $("#aAddGroup", card).onclick = () => { $("#aGroups", card).insertAdjacentHTML("beforeend", groupRow()); bindGroupRows(); };
    const bindGroupRows = () => $$("[data-grm]", card).forEach((b) => (b.onclick = () => b.closest("[data-grow]").remove()));
    bindGroupRows();
    $("#aSave", card).onclick = async () => {
      const group_roles = {};
      $$("[data-grow]", card).forEach((r) => { const g = $("[data-g]", r).value.trim(); if (g) group_roles[g] = $("[data-r]", r).value; });
      const b = { group_roles, default_role: $("#aDefault", card).value, ...flags };
      if (!$("#aProx", card).disabled) b.trusted_proxies = $("#aProx", card).value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
      run(() => orgApi.saveAuth(b), "Sign-in settings saved");
    };
  }

  load();
  return { destroy() { alive = false; } };
}
