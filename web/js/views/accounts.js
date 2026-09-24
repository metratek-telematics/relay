// Agents page: one card per CLI sign-in — plan, how much of the limit is used, when it resets, why
// it cannot run, and what Relay does when it runs out. Adding, pausing and removing accounts too.
import { $, $$, esc, icon, toast, modal, confirm, copyText, fmtCost, timeAgo, prompt as askText } from "../ui.js";
import { S, agentLabel, agentInitial } from "../state.js";
import { api } from "../api.js";

const UNKNOWN = "not known";

function fmtReset(ts) {
  if (!ts) return "";
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "reset due";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const when = new Date(ts * 1000).toLocaleString([], d >= 1 ? { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
  return `resets in ${d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`} (${when})`;
}

// A plan subscription and pay-as-you-go spend are different things, so they never share a chip.
function kindOf(info) {
  const rows = [...(info.plan || []), ...(info.usage || [])];
  const has = (re) => rows.some((r) => re.test(String(r.label || "")));
  if (has(/^plan$/i) || rows.some((r) => /plan|subscription|max|pro|team/i.test(String(r.value || "")) && /plan|signed in with/i.test(String(r.label || "")))) return "plan";
  if (has(/credit|balance/i)) return "payg";
  return "";
}

function stateBadge(a) {
  if (!a.enabled) return `<span class="badge">paused</span>`;   // your own choice outranks anything measured
  if (!a.signed_in) return `<span class="badge amber">not signed in</span>`;
  const st = a.state || {};
  if (st.ok) return `<span class="badge green">ready</span>`;
  if (st.ok === false) return `<span class="badge ${st.kind === "limit" ? "amber" : "red"}">${esc(st.kind === "limit" ? "at its limit" : "cannot run")}</span>`;
  return `<span class="badge outline">${UNKNOWN}</span>`;
}

function reasonLine(a, unread) {
  if (!a.enabled) return `Paused by you: Relay skips this account and uses the next one.`;
  if (!a.signed_in) return `Not signed in, so Relay never sends it a turn. Sign in with the command below; Relay notices by itself when the folder holds the sign-in.`;
  const st = a.state || {};
  if (st.ok === false) return `Cannot run now — ${esc(st.reason || UNKNOWN)}${st.resets_at ? ` · ${esc(fmtReset(st.resets_at))}` : ""}.`;
  if (st.ok) return unread ? `Nothing known is blocking it, so Relay would send it a turn — but how much of its limit is used is ${UNKNOWN} until it is read.`
    : `Ready: Relay can send it a turn now.`;
  return `Whether it can run is ${UNKNOWN} until the limits are read.`;
}

function quotas(info) {
  const ws = info.windows || [];
  if (!ws.length) {
    return `<div class="acct-row"><span class="acct-h">Limit used</span><span class="acct-item"><strong>${UNKNOWN}</strong><span class="muted">${esc(info.unread ? "Relay has not read this account yet — use Check limits." : "this CLI reports its usage only after a turn has run")}</span></span></div>`;
  }
  return `<div class="acct-row acct-top"><span class="acct-h">Limit used <span class="badge outline">from the CLI</span></span><div class="quota-list">${ws.map((w) => {
    const pct = w.used == null ? null : Math.max(0, Math.min(100, Number(w.used)));
    const tone = pct == null ? "" : pct >= 90 ? "red" : pct >= 70 ? "amber" : "green";
    const bits = [pct == null ? UNKNOWN : `${+pct.toFixed(1)}% used`, w.detail || "", fmtReset(w.resets_at) || `reset time ${UNKNOWN}`].filter(Boolean).join(" · ");
    return `<div class="quota"><div class="quota-head"><strong>${esc(w.label)}</strong><span class="muted">${esc(bits)}</span></div>
      ${pct == null ? "" : `<div class="quota-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="${esc(w.label)}"><span class="${tone}" style="width:${pct}%"></span></div>`}
      ${w.as_of ? `<span class="muted quota-asof">as of ${esc(timeAgo(w.as_of))}</span>` : ""}</div>`;
  }).join("")}</div></div>`;
}

function accountBlock(agent, a) {
  const info = a.info || {};
  const kind = kindOf(info);
  const plan = (info.plan || []).find((r) => /^plan$/i.test(r.label || ""));
  const relay = ((info.relay || {}).periods || []).find((p) => p.label === "30 days");
  const chips = (info.plan || []).map((r) => `<span class="acct-item"><span class="muted">${esc(r.label)}</span><strong>${esc(r.value)}</strong></span>`).join("");
  return `<div class="ah ah-pack" data-acc="${esc(agent)}:${esc(a.id)}">
    <span class="av lg ${esc(agent)}">${esc(agentInitial(agent))}</span>
    <div class="who">
      <strong>${esc(agentLabel(agent))} · ${esc(a.name)}
        ${a.is_default ? '<span class="badge outline">default</span>' : ""}
        ${kind === "plan" ? `<span class="badge blue" title="A subscription: turns are included until the window limit">plan${plan ? ` · ${esc(plan.value)}` : ""}</span>`
          : kind === "payg" ? '<span class="badge purple" title="Billed per token from a balance">pay as you go</span>'
          : `<span class="badge outline">plan ${UNKNOWN}</span>`}
        ${a.busy ? '<span class="badge blue">a turn is using it</span>' : ""}</strong>
      <span>${reasonLine(a, !!info.unread)}</span>
    </div>
    <div class="row ah-actions">${stateBadge(a)}
      <button class="btn sm" data-acc-check="${esc(agent)}:${esc(a.id)}" title="Ask the CLI for this account's plan and limits">${icon("refresh")}Check limits</button>
      <button class="btn sm" data-acc-signin="${esc(agent)}:${esc(a.id)}">${icon("shield")}Sign-in command</button>
      <button class="btn sm ghost" data-acc-pause="${esc(agent)}:${esc(a.id)}">${a.enabled ? "Pause" : "Resume"}</button>
      ${a.is_default ? "" : `<button class="btn sm ghost" data-acc-default="${esc(agent)}:${esc(a.id)}" title="Try this account first">Make default</button>
      <button class="btn sm ghost" data-acc-remove="${esc(agent)}:${esc(a.id)}" title="Remove this account">${icon("trash")}</button>`}</div>
    <div class="acct" style="grid-column:2">
      ${chips ? `<div class="acct-row"><span class="acct-h">Account <span class="badge outline">from the CLI</span></span>${chips}</div>` : ""}
      ${quotas(info)}
      <div class="acct-row"><span class="acct-h">Relay measured</span><span class="acct-item"><span class="muted">turns Relay ran on it, last 30 days</span><strong>${relay && relay.turns ? `${relay.turns} turns · ${fmtCost(relay.cost_usd, relay.estimated)}` : (info.relay ? "no turns yet" : UNKNOWN)}</strong></span></div>
      ${(info.notes || []).length ? `<div class="acct-notes">${info.notes.map((n) => `<span class="muted">${esc(n)}</span>`).join("")}</div>` : ""}
    </div>
  </div>`;
}

function policyText(p) {
  const steps = (p.steps || []).map((s) => s.text);
  return steps.length ? steps.join(", then ") : "wait for the window to reset";
}

export function accountsCard(data) {
  if (!data) return `<div class="card" id="accountsCard"><div class="card-head"><h3>Accounts, plans and limits</h3></div><div class="card-body hint">Loading accounts…</div></div>`;
  const groups = (data.agents || []).filter((g) => (S.agents || {})[g.agent]?.installed !== false || g.multiple);
  return `<div class="card" id="accountsCard"><div class="card-head"><h3>Accounts, plans and limits</h3>
      <button class="btn sm" id="accRefresh" title="Ask every CLI for its plan and limits again">${icon("refresh")}Check all limits</button></div>
    <div class="card-body stack">
      <div class="hint">One row per sign-in. A CLI is signed in per configuration folder, so a second subscription is a second account: Relay prefers the default and moves to the next one that still has capacity.</div>
      ${groups.map((g) => `<div class="stack">
        <div class="row between"><strong>${esc(g.label)}</strong>
          <button class="btn sm" data-acc-add="${esc(g.agent)}">${icon("plus", "sm")}Add account</button></div>
        ${g.accounts.map((a) => accountBlock(g.agent, a)).join("")}
        <div class="hint">When ${esc(g.label)} hits its limit, Relay will ${esc(policyText(g.policy))}. An account counts as out of capacity at ${esc(String(g.policy.threshold))}% of a window.</div>
      </div>`).join("") || '<div class="hint">No CLI here signs in per account.</div>'}
      <div class="hint">Numbers marked <span class="badge outline">from the CLI</span> are what the CLI itself reported; “Relay measured” is Relay's own record of the turns it ran. Anything Relay has not read says “${UNKNOWN}” rather than 0.</div>
    </div></div>`;
}

function signInModal(agent, a) {
  const ide = (S.config || {}).ide_url;
  const cmd = a.login_command;
  modal(`<h2><span class="av sm ${esc(agent)}">${esc(agentInitial(agent))}</span> Sign in: ${esc(agentLabel(agent))} · ${esc(a.name)}</h2>
    <p class="hint">These CLIs only sign in interactively (a browser and a code to paste), so Relay cannot do it for you. Run this once${a.is_default ? "" : ", exactly as written — the variables put the sign-in in this account's own folder"}:</p>
    <pre class="ho-cmds"><span class="ho-line"><span>${esc(ide ? cmd : `docker exec -it relay sh -lc '${cmd}'`)}</span><button class="btn xs ghost" data-copy="${esc(ide ? cmd : `docker exec -it relay sh -lc '${cmd}'`)}" title="Copy">${icon("copy", "sm")}</button></span></pre>
    ${ide ? `<p class="hint">Open <a href="${esc(ide)}" target="_blank" rel="noopener">VS Code</a>, Terminal → New Terminal, and paste it. On the server instead: <code>docker exec -it relay sh -lc '${esc(cmd)}'</code></p>` : ""}
    ${a.home ? `<p class="hint">Its sign-in is kept in <code>${esc(a.home)}</code>. Relay watches that folder and shows the account as signed in as soon as the login lands there — press Re-check.</p>` : ""}
    <div class="modal-actions"><button class="btn primary" data-close>Done</button></div>`);
  $$("[data-copy]").forEach((b) => (b.onclick = () => copyText(b.dataset.copy)));
}

export function bindAccountsCard(root, data, reload) {
  const find = (key) => {
    const [agent, id] = key.split(":");
    const g = (data.agents || []).find((x) => x.agent === agent) || { accounts: [] };
    return [agent, id, g.accounts.find((a) => a.id === id) || {}];
  };
  const act = async (fn, ok) => {
    try { await fn(); if (ok) toast("success", ok); } catch (e) { toast("error", "Could not save", e.message); }
    reload();
  };
  $$("[data-acc-add]", root).forEach((b) => (b.onclick = async () => {
    const agent = b.dataset.accAdd;
    const name = await askText(`Add a ${agentLabel(agent)} account`, "A name you will recognise, such as “Team plan” or “Personal”.",
      { placeholder: "Team plan", okLabel: "Add" });
    if (name) act(() => api.addAgentAccount(agent, name), `${name} added — sign it in to start using it`);
  }));
  $$("[data-acc-signin]", root).forEach((b) => (b.onclick = () => { const [agent, , a] = find(b.dataset.accSignin); signInModal(agent, a); }));
  $$("[data-acc-pause]", root).forEach((b) => (b.onclick = () => {
    const [agent, id, a] = find(b.dataset.accPause);
    act(() => api.updateAgentAccount(agent, id, { enabled: !a.enabled }), a.enabled ? `${a.name} paused — Relay skips it` : `${a.name} resumed`);
  }));
  $$("[data-acc-default]", root).forEach((b) => (b.onclick = () => {
    const [agent, id, a] = find(b.dataset.accDefault);
    act(() => api.updateAgentAccount(agent, id, { default: true }), `${a.name} is now tried first`);
  }));
  $$("[data-acc-remove]", root).forEach((b) => (b.onclick = async () => {
    const [agent, id, a] = find(b.dataset.accRemove);
    if (await confirm(`Remove ${a.name}?`, "Deletes this account's sign-in folder on the server. The other accounts are untouched.", { danger: true, okLabel: "Remove" }))
      act(() => api.removeAgentAccount(agent, id), `${a.name} removed`);
  }));
  $$("[data-acc-check]", root).forEach((b) => (b.onclick = async () => {
    const [agent] = find(b.dataset.accCheck);
    b.disabled = true; b.innerHTML = `${icon("spinner", "spin")}Checking…`;
    try { await api.agentAccounts(true, agent); } catch (e) { toast("error", "Could not read the account", e.message); }
    reload();
  }));
  const all = $("#accRefresh", root);
  if (all) all.onclick = async () => {
    all.disabled = true; all.innerHTML = `${icon("spinner", "spin")}Checking…`;
    try { await api.agentAccounts(true); } catch (e) { toast("error", "Could not read the accounts", e.message); }
    reload();
  };
}
