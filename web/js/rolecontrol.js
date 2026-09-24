// One control for a role — agent · account · model · effort, in that order, with the same words
// wherever it appears: Settings → Team and workflow, the New task wizard and a task's own team
// editor all draw it through workflowEditor (views/newtask.js), and the task page draws the
// read-only half of it (roleNowHtml) in its roster.
//
// Two rules hold the whole thing together:
//
//   1. No value is ever shown as a bare "default". Every picker's fallback option carries the real
//      content and whose it is — "CLI default — gpt-5.6-luna" — so a person can read what will run
//      without opening another page.
//   2. The resolution is not re-derived here. A running task publishes what it resolved to
//      (role_plan, with `sources`) and each finished turn records what it really used (sessions);
//      before a task exists the same chain is read off the settings the server already resolved
//      (orchestrator/personal.py → /api/state `config` and `personal`).
import { esc, icon } from "./ui.js";
import { S, agentLabel, agentInitial, ROLE_LABEL, roleAgent } from "./state.js";
import { AUTO_FREE, orLabel, orSupported } from "./views/openrouter.js";

export const ROLES = ["supervisor", "worker", "reviewer"];

const health = (agent) => (S.agents || {})[agent] || {};
const meta = (agent) => (S.agentMeta || {})[agent] || {};
const effortsOf = (agent) => meta(agent).efforts || [];
const globalRole = (role) => ((S.config || {}).roles || {})[role] || {};
const agentDefault = (agent) => ((S.config || {}).agent_defaults || {})[agent] || {};
export const accountsOf = (agent) => ((S.accounts || {})[agent] || []);
// Whose role default a value is: this person's own, or the organisation's (orchestrator/personal.py).
// The server sends both, so "yours" is only ever said of a value this person really changed.
function roleOwner(role, kind, value) {
  const p = S.personal || {};
  if ((p.sources || {}).roles !== "yours") return "organisation";
  const org = ((p.organisation || {}).roles || {})[role] || {};
  return (org[kind] || "") === (value || "") ? "organisation" : "yours";
}

// The one wording table. Everything that names a source reads it, so the three pages cannot drift.
const OWNER_WORD = {
  task: "This task's own choice",
  yours: "Your default",
  organisation: "The organisation default",
  agent: (a) => `The saved default for ${agentLabel(a)}`,
  cli: (a) => `The ${agentLabel(a)} CLI's own default`,
};
const OWNER_SHORT = {
  task: "This task",
  yours: "Your default",
  organisation: "Organisation default",
  agent: (a) => `${agentLabel(a)} default`,
  cli: () => "CLI default",
};
// The same layers inside a sentence, so a button reads "Use the CLI default \u2014 high".
const OWNER_PHRASE = {
  task: "this task's own choice",
  yours: "your default",
  organisation: "the organisation default",
  agent: (a) => `the ${agentLabel(a)} default`,
  cli: () => "the CLI default",
};
const word = (table, owner, agent) => (typeof table[owner] === "function" ? table[owner](agent) : table[owner]);

/** The layer that decides a role's model or effort when the layer being edited leaves it blank.
 *  scope "task" falls back to the role default first; in Settings the role default *is* the layer
 *  being edited, so it falls back to the agent default and then to the CLI itself. */
export function fallbackFor(kind, role, agent, scope) {
  if (scope === "task") {
    const g = globalRole(role);
    if ((g.agent || "") === agent && (g[kind] || "")) return { value: g[kind], owner: roleOwner(role, kind, g[kind]), agent };
  }
  const d = agentDefault(agent);
  if (d[kind]) return { value: d[kind], owner: "agent", agent };
  const cli = kind === "model" ? health(agent).default_model : health(agent).default_effort;
  return { value: cli || "", owner: "cli", agent, unknown: !cli };
}

/** "CLI default — gpt-5.6-luna", or what to say when even the CLI has not been read. */
export function fallbackLabel(kind, role, agent, scope) {
  if (kind === "effort" && !effortsOf(agent).length) return `${agentLabel(agent)} has no effort setting`;
  const f = fallbackFor(kind, role, agent, scope);
  const head = word(OWNER_SHORT, f.owner, agent);
  if (f.value) return `${head} — ${f.value}`;
  return kind === "model" ? `${head} — not read from ${agentLabel(agent)} yet` : `${head} — no effort passed`;
}

/** What a role will actually use for one value, with whose value it is — for pages that only report
 *  (Settings \u2192 Agents and models, the task rail) rather than offering the picker. */
export function resolvedLabel(kind, role, agent) {
  if (kind === "effort" && !effortsOf(agent).length) return "no effort setting";
  const f = fallbackFor(kind, role, agent, "task");
  const w = word(OWNER_SHORT, f.owner, agent);
  return f.value ? `${f.value} (${w.toLowerCase()})` : `set by the ${agentLabel(agent)} CLI`;
}

/** The sentence under a picker: whose value is in effect, and what it is. */
function sourceLine(kind, role, cur, scope, ctx) {
  const agent = cur.agent;
  const own = (cur[kind] || "").trim();
  const f = fallbackFor(kind, role, agent, scope);
  const key = `${role}.${kind}`;
  if (own) {
    // Whose value this is, and the one click back to the layer underneath.
    const fallbackPhrase = kind === "effort" && !effortsOf(agent).length
      ? `no effort (${agentLabel(agent)} has no setting)`
      : `${word(OWNER_PHRASE, f.owner, agent)}${f.value ? ` \u2014 ${f.value}` : ""}`;
    let owner = "task", back = { to: "", label: `Use ${fallbackPhrase}` };
    const g = globalRole(role);
    if (scope === "task" && ctx.fresh && (g.agent || "") === agent && (g[kind] || "") === own) {
      // A draft the wizard filled from the defaults has not been decided by anybody yet: it is still
      // the default it came from, and saying otherwise would invent a choice the person never made.
      owner = roleOwner(role, kind, own); back = null;
    } else if (scope === "org") owner = "organisation";
    else if (scope === "mine") {
      const org = ((ctx.orgRoles || {})[role] || {})[kind] || "";
      if (own === org) { owner = "organisation"; back = null; }   // already the organisation's: nothing to fall back to
      else { owner = "yours"; back = { to: org, label: org ? `Use the organisation default \u2014 ${org}` : `Use the organisation default (${fallbackPhrase})` }; }
    }
    return `<p class="rc-src"><span class="rc-owner">${esc(word(OWNER_WORD, owner, agent))}</span>
      ${back ? `<button type="button" class="rc-back" data-fallback="${esc(key)}" data-to="${esc(back.to)}">${icon("retry", "sm")}${esc(back.label)}</button>` : ""}</p>`;
  }
  const inEffect = f.value ? `<b>${esc(f.value)}</b>` : esc(kind === "model" ? "no model is passed" : "no effort is passed");
  return `<p class="rc-src"><span class="rc-owner">${esc(word(OWNER_WORD, f.owner, agent))}</span><span class="rc-eff">in effect: ${inEffect}</span></p>`;
}

// ---------------------------------------------------------------- the four slots
function accountSlot(role, cur, scope) {
  const agent = cur.agent;
  const rows = accountsOf(agent);
  if (cur.provider === "openrouter") {
    return `<div class="rc-slot"><label class="rl" for="rc-acct-${role}">Account</label>
      <select id="rc-acct-${role}" disabled><option>OpenRouter key, not a ${esc(agentLabel(agent))} sign-in</option></select>
      <p class="rc-src"><span class="rc-owner">Relay's OpenRouter key</span><span class="rc-eff">this role does not use a ${esc(agentLabel(agent))} sign-in</span></p></div>`;
  }
  if (rows.length < 2) {
    const only = rows[0];
    return `<div class="rc-slot"><label class="rl" for="rc-acct-${role}">Account</label>
      <select id="rc-acct-${role}" disabled><option>${esc(only ? only.name : `The ${agentLabel(agent)} sign-in on this server`)}</option></select>
      <p class="rc-src"><span class="rc-owner">The only sign-in</span><span class="rc-eff">add a second one on the Agents page to choose here</span></p></div>`;
  }
  const pinned = (cur.account || "").trim();
  const state = (a) => (!a.enabled ? " · paused" : !a.signed_in ? " · not signed in" : "");
  const auto = rows.filter((a) => a.enabled && a.signed_in).map((a) => a.name);
  const opts = rows.map((a) => `<option value="${esc(a.id)}" ${pinned === a.id ? "selected" : ""}>${esc(a.name)}${a.is_default ? " (default)" : ""}${esc(state(a))}</option>`).join("");
  const line = pinned
    ? `<p class="rc-src"><span class="rc-owner">${esc(scope === "task" ? "This task's own choice" : scope === "org" ? "The organisation default" : word(OWNER_WORD, roleOwner(role, "account", pinned)))}</span>
        <button type="button" class="rc-back" data-fallback="${esc(role)}.account" data-to="">${icon("retry", "sm")}Use whichever has capacity</button></p>`
    : `<p class="rc-src"><span class="rc-owner">Whichever has capacity</span><span class="rc-eff">${esc(auto.length ? auto.join(", then ") : "none is signed in")}</span></p>`;
  return `<div class="rc-slot"><label class="rl" for="rc-acct-${role}">Account</label>
    <select id="rc-acct-${role}" data-account="${esc(role)}"><option value="">Whichever has capacity${auto.length ? ` — ${esc(auto[0])} first` : ""}</option>${opts}</select>
    ${line}</div>`;
}

function modelSlot(role, cur, scope, ctx) {
  const agent = cur.agent, onOR = cur.provider === "openrouter";
  const catalog = onOR
    ? [...new Set([AUTO_FREE, ...(((S.config || {}).models || {}).openrouter || []), ...(((S.config || {}).model_recent || {}).openrouter || [])])]
    : [...new Set([...(((S.config || {}).models || {})[agent] || []), ...(((S.config || {}).model_recent || {})[agent] || [])])];
  const custom = cur.model && !catalog.includes(cur.model);
  const def = ((S.providers || {}).openrouter || {}).default_model || AUTO_FREE;
  const first = onOR ? `OpenRouter default — ${orLabel(def)}` : fallbackLabel("model", role, agent, scope);
  const line = onOR
    ? `<p class="rc-src"><span class="rc-owner">${esc(cur.model ? "This role's own choice" : "The OpenRouter default")}</span><span class="rc-eff">in effect: <b>${esc(orLabel(cur.model || def))}</b></span></p>`
    : sourceLine("model", role, cur, scope, ctx);
  return `<div class="rc-slot"><label class="rl" for="rc-model-${role}">Model</label>
    <div class="rc-row"><select id="rc-model-${role}" data-model-sel="${esc(role)}"><option value="">${esc(first)}</option>${catalog.map((m) => `<option value="${esc(m)}" ${cur.model === m ? "selected" : ""}>${esc(onOR ? orLabel(m) : m)}</option>`).join("")}<option value="__custom__" ${custom ? "selected" : ""}>Custom…</option></select>
      ${onOR ? `<button type="button" class="btn xs" data-or-browse="${esc(role)}" title="Browse OpenRouter models">${icon("layers", "sm")}Browse</button>` : ""}</div>
    <input data-model="${esc(role)}" placeholder="${onOR ? "OpenRouter model id, e.g. anthropic/claude-sonnet-4.5" : "exact model id passed to the CLI"}" value="${esc(custom ? cur.model : "")}" ${custom ? "" : "hidden"}>
    ${line}</div>`;
}

function effortSlot(role, cur, scope, ctx) {
  const agent = cur.agent, list = effortsOf(agent);
  if (!list.length) {
    return `<div class="rc-slot"><label class="rl" for="rc-effort-${role}">Effort</label>
      <select id="rc-effort-${role}" disabled><option>${esc(agentLabel(agent))} has no effort setting</option></select>
      <p class="rc-src"><span class="rc-owner">Not offered by this CLI</span><span class="rc-eff">nothing is passed, so its own default reasoning applies</span></p></div>`;
  }
  return `<div class="rc-slot"><label class="rl" for="rc-effort-${role}">Effort</label>
    <select id="rc-effort-${role}" data-effort="${esc(role)}"><option value="">${esc(fallbackLabel("effort", role, agent, scope))}</option>${list.map((e) => `<option value="${esc(e)}" ${cur.effort === e ? "selected" : ""}>${esc(e)}</option>`).join("")}</select>
    ${sourceLine("effort", role, cur, scope, ctx)}</div>`;
}

// ---------------------------------------------------------------- what is running now
/** What a role really ran on, from the turn records the runner wrote (sessions), and what it will
 *  run on next, from the plan the orchestrator published (role_plan). */
export function roleNow(t, role) {
  const sess = (t?.sessions || {})[role] || {};
  const plan = (t?.role_plan || {})[role] || {};
  const wf = ((t?.workflow || {}).roles || {})[role] || {};
  const agent = plan.agent || wf.agent || "";
  const turns = Number(sess.turns || 0);
  return {
    agent,
    ran: turns > 0,
    turns,
    account: sess.account_name || sess.account || "",
    model: (turns ? sess.model : "") || plan.model || wf.model || "",
    effort: (turns && sess.effort !== undefined ? sess.effort : "") || plan.effort || wf.effort || "",
    provider: plan.provider || wf.provider || "",
    supportsEffort: plan.supports_effort !== undefined ? plan.supports_effort : effortsOf(agent).length > 0,
    sources: plan.sources || {},
  };
}

/** The same four values as words, for a roster cell or a tooltip: what is actually in effect, kept
 *  short. Where it comes from is the editor's job to say; here the values themselves are the point. */
export function roleNowFacts(t, role) {
  const n = roleNow(t, role);
  const out = [];
  if (n.account) out.push(n.account);
  if (n.provider === "openrouter") out.push(`${orLabel(n.model)} via OpenRouter`);
  else {
    const m = n.model || fallbackFor("model", role, n.agent, "task").value;
    out.push(m || "CLI default (not read)");
  }
  const e = n.effort || fallbackFor("effort", role, n.agent, "task").value;
  out.push(!n.supportsEffort ? "no effort setting" : e || "CLI default (not read)");
  if (n.turns) out.push(`turn ${n.turns}`);
  return out;
}

export function roleSummaryText(t, roles = ROLES) {
  return roles.filter((r) => roleAgent(t, r)).map((r) => {
    const n = roleNow(t, r);
    return `${ROLE_LABEL[r]} · ${agentLabel(n.agent)}\n${n.ran ? "Running" : "Will run"}: ${roleNowFacts(t, r).join(" · ")}`;
  }).join("\n\n");
}

/** The roster cell on the task page: agent, account, model, effort and the turn it is on. */
export function roleNowHtml(t, role, { live = false } = {}) {
  const n = roleNow(t, role);
  if (!n.agent) return "";
  const facts = roleNowFacts(t, role);
  return `<span class="rc-now ${live ? "on" : ""}" title="${esc(roleSummaryText(t, [role]))}">
    <span class="av xs ${esc(n.agent)}">${esc(agentInitial(n.agent))}</span>
    <span class="rcn-text"><span class="rcn-head">${esc(ROLE_LABEL[role])} · ${esc(agentLabel(n.agent))}${live ? '<span class="live-dot sm"></span>' : ""}</span>
      <span class="rcn-facts">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</span></span></span>`;
}

// ---------------------------------------------------------------- the editable control
/** One role, head to foot: which agent, where it runs, which sign-in, which model, which effort.
 *  ctx: { scope: "task"|"mine"|"org", agents, task, orgRoles } */
export function roleControlHtml(role, cur, ctx) {
  const scope = ctx.scope || "task";
  const agents = ctx.agents || S.agentMeta || {};
  const orReady = (a) => cur.provider === "openrouter" && orSupported(a) && !!health(a).installed;
  const pick = Object.keys(agents).filter((a) => agents[a].builtin || health(a).installed || cur.agent === a)
    .map((a) => `<button type="button" data-role="${esc(role)}" data-agent="${esc(a)}" class="${cur.agent === a ? "active" : ""}" style="--agent:${esc(agents[a].color)}" title="${esc(orReady(a) ? "ready on OpenRouter" : health(a).ok ? "ready" : (health(a).error || "not ready"))}"><span class="av sm ${esc(a)}">${esc(agentInitial(a))}</span>${esc(agents[a].label)}${health(a) && health(a).ok === false && !orReady(a) ? ' <span class="muted">!</span>' : ""}</button>`).join("");
  const now = ctx.task && roleAgent(ctx.task, role) === cur.agent ? roleNow(ctx.task, role) : null;
  const body = !cur.agent
    ? '<div class="muted" style="font-size:11px">No agent in this role.</div>'
    : `${providerSlot(role, cur)}
       ${accountSlot(role, cur, scope)}
       ${modelSlot(role, cur, scope, ctx)}
       ${effortSlot(role, cur, scope, ctx)}
       ${now && now.ran ? `<p class="rc-ran">${icon("clock", "sm")}<span>Ran on ${esc(roleNowFacts(ctx.task, role).join(" · "))}</span></p>` : ""}`;
  return `<div class="role-box rc" data-rc="${esc(role)}">
    <div class="rt">${esc(ROLE_LABEL[role])}${role === "reviewer" ? ' <span class="rt-opt">(optional)</span>' : ""}</div>
    <div class="rc-slot"><label class="rl">Agent</label><div class="agent-pick">${pick}${role === "reviewer" ? `<button type="button" data-role="reviewer" data-agent="" class="${!cur.agent ? "active" : ""}">none</button>` : ""}</div></div>
    ${body}</div>`;
}

function providerSlot(role, cur) {
  const orOk = orSupported(cur.agent);
  const why = (meta(cur.agent).openrouter || {}).why || "";
  const onOR = cur.provider === "openrouter";
  return `<div class="rc-slot"><label class="rl">Runs on</label>
    <div class="role-provider" role="group" aria-label="${esc(role)} runs on">
      <button type="button" data-prov-role="${esc(role)}" data-prov="" class="${onOR ? "" : "active"}" aria-pressed="${!onOR}">${esc(agentLabel(cur.agent))} sign-in</button>
      <button type="button" data-prov-role="${esc(role)}" data-prov="openrouter" class="${onOR ? "active" : ""}" aria-pressed="${onOR}" ${orOk ? "" : `disabled title="${esc(why)}"`}>OpenRouter</button></div></div>`;
}

/** Bind every role control inside `host`. `apply(role, patch)` is called with what changed; the
 *  caller redraws, so one control behaves the same wherever it is drawn. */
export function bindRoleControls(host, { roles, onPatch, onAgent, onProvider, onBrowse }) {
  const $$ = (sel) => [...host.querySelectorAll(sel)];
  $$("[data-role][data-agent]").forEach((b) => (b.onclick = () => onAgent(b.dataset.role, b.dataset.agent)));
  $$("[data-prov-role]").forEach((b) => (b.onclick = () => onProvider(b.dataset.provRole, b.dataset.prov)));
  $$("[data-or-browse]").forEach((b) => (b.onclick = () => onBrowse(b.dataset.orBrowse)));
  $$("[data-account]").forEach((s) => s.addEventListener("change", () => onPatch(s.dataset.account, { account: s.value })));
  $$("[data-effort]").forEach((s) => s.addEventListener("change", () => onPatch(s.dataset.effort, { effort: s.value })));
  $$("[data-model-sel]").forEach((s) => s.addEventListener("change", () => {
    const r = s.dataset.modelSel, input = host.querySelector(`[data-model="${r}"]`);
    if (s.value === "__custom__") { input.hidden = false; input.focus(); onPatch(r, { model: input.value.trim() }, { redraw: false }); }
    else onPatch(r, { model: s.value });
  }));
  $$("[data-model]").forEach((i) => i.addEventListener("input", () => onPatch(i.dataset.model, { model: i.value.trim() }, { redraw: false })));
  $$("[data-fallback]").forEach((b) => (b.onclick = () => {
    const [r, kind] = b.dataset.fallback.split(".");
    onPatch(r, { [kind]: b.dataset.to || "" });
  }));
  void roles;
}
