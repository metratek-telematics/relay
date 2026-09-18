// OpenRouter: the model browser (Models & usage), the provider settings (Settings → Model providers) and the helpers the
// team picker uses. The server side is orchestrator/openrouter.py; agents reach OpenRouter through Relay's gateway.
import { $, $$, esc, icon, toast, modal, fmtCost, timeAgo, debounce } from "../ui.js";
import { S, agentLabel } from "../state.js";
import { api } from "../api.js";

export const AUTO_FREE = "openrouter:auto-free";
export const orLabel = (id) => (id === AUTO_FREE ? "Auto · best free model" : id || "OpenRouter default");
export const orSupported = (agent) => !!(((S.agentMeta || {})[agent] || {}).openrouter || {}).ok;
export const orConfigured = () => !!((S.providers || {}).openrouter || {}).configured;

const money = (v, digits) => (v == null ? "—" : `$${Number(v).toFixed(digits ?? (Math.abs(v) < 1 ? 4 : 2))}`);
const spent = (b) => (Number(b.cost_usd) ? fmtCost(b.cost_usd, b.estimated) : "$0");
const perM = (v) => (v == null ? "?" : v === 0 ? "$0" : `$${v < 1 ? v.toFixed(2) : v.toFixed(v % 1 ? 2 : 0)}`);
const fmtCtx = (n) => (!n ? "" : n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}k`);
const CTX_STEPS = [[0, "Any context"], [32000, "≥ 32k"], [64000, "≥ 64k"], [128000, "≥ 128k"], [200000, "≥ 200k"], [1000000, "≥ 1M"]];
const SORTS = [["recommended", "Recommended first"], ["price", "Price: low to high"], ["price_desc", "Price: high to low"], ["context", "Largest context"], ["newest", "Newest"]];

function badges(r) {
  const out = [];
  if (r.free) out.push('<span class="badge green">free</span>');
  else if (r.router) out.push('<span class="badge outline" title="Priced per request by the router">router</span>');
  else out.push(`<span class="price" title="USD per 1M tokens, input / output">${perM(r.input)} / ${perM(r.output)}</span>`);
  if (r.context) out.push(`<span class="muted">${fmtCtx(r.context)} ctx</span>`);
  out.push(r.tools ? '<span class="badge outline" title="Supports tool calling; coding agents need it">tools</span>' : '<span class="badge amber" title="No tool calling: coding agents cannot use it">no tools</span>');
  if (r.reasoning) out.push('<span class="muted">reasoning</span>');
  if (r.structured) out.push('<span class="muted" title="Structured outputs / JSON schema">structured</span>');
  if ((r.modalities_in || []).includes("image")) out.push('<span class="muted">images in</span>');
  return out.join("");
}

// Account strip: what OpenRouter says about the key, and what Relay spent there.
export function accountHtml(d, { relay = true } = {}) {
  const a = (d || {}).account || {};
  const rel = (d || {}).relay || {};
  const chips = [];
  if (!d || d.configured === false) return `<div class="acct-notes"><span class="muted">No OpenRouter key yet. An admin adds one in <a href="#/settings/providers">Settings → Model providers</a>; the model list below works without one.</span></div>`;
  if (!a.ok) return `<div class="acct-notes"><span class="err">${esc(a.error || "OpenRouter did not answer.")}</span></div>`;
  const left = a.credits_remaining;
  chips.push(["Credits left", left == null ? "unknown" : money(left, 2)]);
  if (a.limit != null) chips.push(["Key limit", `${money(a.limit_remaining, 2)} of ${money(a.limit, 2)}${a.limit_reset ? ` (${a.limit_reset})` : ""}`]);
  chips.push(["Tier", a.is_free_tier ? "free tier" : "paid"]);
  if (a.free_requests) chips.push(["Free requests today", `${a.free_requests.used ?? "?"} of ${a.free_requests.limit ?? "?"}`]);
  chips.push(["OpenRouter usage today", money(a.usage_daily, 4)], ["this month", money(a.usage_monthly, 4)]);
  const byModel = (rel.by_model || []).slice(0, 6);
  const low = d.low_credit_usd && left != null && left < d.low_credit_usd && a.total_credits;
  const meters = (d.meters || []).map((b) => `<div class="quota"><div class="quota-head"><strong>${esc(b.label)}</strong><span class="muted">${money(b.spent_usd, 2)} of ${money(b.budget_usd, 2)} this month · forecast ${money(b.forecast_usd, 2)}</span></div>
      <div class="quota-bar" role="progressbar" aria-valuenow="${Math.min(100, b.pct)}" aria-valuemin="0" aria-valuemax="100" aria-label="${esc(b.label)}"><span class="${b.state === "over" ? "red" : b.state === "warn" ? "amber" : "green"}" style="width:${Math.min(100, b.pct)}%"></span></div></div>`).join("");
  return `<div class="acct-row"><span class="acct-h">OpenRouter account</span>${chips.map(([l, v]) => `<span class="acct-item"><span class="muted">${esc(l)}</span><strong>${esc(v)}</strong></span>`).join("")}</div>
    ${low ? `<div class="acct-notes"><span class="err">Credits are below the ${money(d.low_credit_usd, 2)} alert: paid models stop at zero, free models keep working.</span></div>` : ""}
    ${relay ? `<div class="acct-row"><span class="acct-h">Relay spend on OpenRouter</span><span class="acct-item"><span class="muted">today</span><strong>${spent((rel.today || {}))}</strong></span><span class="acct-item"><span class="muted">this month</span><strong>${spent((rel.month || {}))}</strong></span>
      ${byModel.map((m) => `<span class="acct-item"><span class="muted">${esc(m.key)}</span><strong>${spent(m)}</strong></span>`).join("")}</div>
    ${meters ? `<div class="acct-row acct-top"><span class="acct-h">Spend caps</span><div class="quota-list">${meters}</div></div>` : ""}
    <div class="acct-notes"><span class="muted">Checked ${esc(timeAgo(a.checked_at))} · spend by role, agent and task is on each task's Sessions tab and in Settings → Usage.</span></div>` : ""}`;
}

function autoPicksHtml(list, pick) {
  if (!(list || []).length) return '<span class="muted">No free model qualifies right now (free, tool calling, enough context). Relay falls back to OpenRouter\'s free router.</span>';
  const more = list.length - 3;
  return `<ol class="or-picks">${list.map((c, i) => `<li class="${c.cooldown_until ? "is-cool" : ""}" ${i >= 3 ? "data-more-pick hidden" : ""}>
      <div class="or-pick-head"><span class="or-rank">${i + 1}</span><code>${esc(c.id)}</code>${c.score != null ? `<span class="badge outline" title="Relay's score for coding agents">${esc(c.score)}</span>` : ""}
        ${c.cooldown_until ? `<span class="badge amber" title="${esc(c.cooldown_reason || "")}">cooling down until ${esc(new Date(c.cooldown_until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span>` : ""}
        ${pick ? `<button class="btn xs" data-use="${esc(c.id)}">Use this one</button>` : ""}</div>
      <div class="or-why muted">${(c.why || []).map(esc).join(" · ")}</div></li>`).join("")}</ol>
    ${more > 0 ? `<button type="button" class="btn xs ghost" data-show-picks aria-expanded="false">Show the whole ranking (${more} more)</button>` : ""}`;
}

// The OpenRouter model browser. With `pick`, choosing a model (or the automatic free pick) calls onPick(id).
export function openOpenRouterBrowser({ pick = false, current = "", onPick } = {}) {
  let data = null, query = "", sort = "recommended";
  const f = { free: false, tools: true, reasoning: false, ctx: 0, rec: false, starred: false };
  const m = modal(`<h2>${icon("layers")} OpenRouter models${pick ? " · pick one" : ""}</h2>
    <div id="orAcct" class="acct"><span class="muted">Loading the account…</span></div>
    <div class="or-auto card-lite"><div class="row between wrap" style="gap:8px"><strong>${icon("sparkles", "sm")} Auto · best free model</strong>
        ${pick ? `<button class="btn sm ${current === AUTO_FREE ? "primary" : ""}" data-use="${AUTO_FREE}">${current === AUTO_FREE ? "Selected" : "Use automatic free pick"}</button>` : ""}</div>
      <p class="hint" style="margin:4px 0 8px">Relay picks the best free model when a task starts, keeps it for the session and rotates to the next one on a rate limit or an outage. The ranking uses the catalog (tools, context, reasoning, age, size) and Relay's own record with each model.</p>
      <div id="orAuto"><span class="muted">Ranking…</span></div></div>
    <div class="or-filters row wrap">
      <label class="search-field sm">${icon("search")}<input id="orQ" type="search" placeholder="Search id or name" aria-label="Search models"></label>
      <label class="row"><input type="checkbox" data-f="free"> Free only</label>
      <label class="row" title="Coding agents call tools; models without tool calling cannot run a turn"><input type="checkbox" data-f="tools" checked> Tool calling</label>
      <label class="row"><input type="checkbox" data-f="reasoning"> Reasoning</label>
      <label class="row"><input type="checkbox" data-f="rec"> Recommended for coding</label>
      <label class="row"><input type="checkbox" data-f="starred"> Starred</label>
      <select id="orCtx" aria-label="Minimum context">${CTX_STEPS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
      <select id="orSort" aria-label="Sort">${SORTS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
      <button class="btn sm" id="orRefresh" title="Fetch the catalog and the account again">${icon("refresh")}Refresh</button></div>
    <p class="hint" id="orHint">Loading the catalog…</p>
    <div class="model-list" id="orList"></div>
    <p class="hint">★ adds a model to the OpenRouter pickers in every team. Prices are USD per million tokens (input / output), as OpenRouter lists them.</p>
    <div class="modal-actions"><button class="btn primary" data-close>Done</button></div>`, { wide: true });
  const list = $("#orList", m.body);
  const starred = () => new Set(((S.config || {}).models || {}).openrouter || []);
  const use = (id) => { if (onPick) onPick(id); m.close(); };
  const draw = () => {
    if (!data) return;
    const recIds = new Map((data.recommended || []).map((r, i) => [r.id, { ...r, rank: i }]));
    const st = starred();
    const q = query.toLowerCase();
    let rows = (data.models || []).filter((r) => (!f.free || r.free) && (!f.tools || r.tools) && (!f.reasoning || r.reasoning) && (!f.ctx || (r.context || 0) >= f.ctx)
      && (!f.rec || recIds.has(r.id)) && (!f.starred || st.has(r.id)) && (!q || r.id.toLowerCase().includes(q) || (r.name || "").toLowerCase().includes(q)));
    const price = (r) => (r.free ? 0 : r.input == null ? 1e9 : (r.input * 3 + (r.output || 0)) / 4);
    const cmp = {
      recommended: (a, b) => (recIds.has(b.id) - recIds.has(a.id)) || ((recIds.get(a.id)?.rank ?? 0) - (recIds.get(b.id)?.rank ?? 0)) || (st.has(b.id) - st.has(a.id)) || (b.free - a.free) || ((b.created || 0) - (a.created || 0)),
      price: (a, b) => price(a) - price(b), price_desc: (a, b) => price(b) - price(a),
      context: (a, b) => (b.context || 0) - (a.context || 0), newest: (a, b) => (b.created || 0) - (a.created || 0),
    }[sort];
    rows = rows.sort(cmp);
    const total = (data.models || []).length;
    $("#orHint", m.body).textContent = data.error && !total ? data.error
      : `${rows.length} of ${total} models · ${(data.models || []).filter((r) => r.free).length} free · ${(data.models || []).filter((r) => r.tools).length} with tool calling${data.fetched ? ` · catalog from ${timeAgo(data.fetched)}` : ""}${data.stale ? " (could not refresh; showing the saved copy)" : ""}`;
    list.innerHTML = rows.slice(0, 300).map((r) => {
      const rec = recIds.get(r.id);
      return `<div class="model-row ${current === r.id ? "is-default" : ""} ${r.tools ? "" : "is-off"}">
        <button class="btn xs ghost star ${st.has(r.id) ? "on" : ""}" data-star="${esc(r.id)}" aria-label="${st.has(r.id) ? "Remove from pickers" : "Add to pickers"}" title="${st.has(r.id) ? "Remove from pickers" : "Add to pickers"}">${st.has(r.id) ? "★" : "☆"}</button>
        <div class="mr-name"><code>${esc(r.id)}</code><span class="muted" title="${esc(r.description || "")}">${esc(r.name || "")}${rec ? ` · recommended: ${esc(rec.why.slice(0, 3).join(", "))}` : ""}</span></div>
        <div class="mr-meta">${rec ? '<span class="badge blue" title="Computed from tools, context, reasoning, age and price">coding pick</span>' : ""}${badges(r)}</div>
        ${pick ? `<button class="btn xs ${current === r.id ? "primary" : ""}" data-use="${esc(r.id)}" ${r.tools ? "" : 'title="No tool calling: an agent turn will fail"'}>${current === r.id ? "Selected" : "Use"}</button>` : '<span></span>'}</div>`;
    }).join("") + (rows.length > 300 ? `<div class="empty small">${rows.length - 300} more; search or filter to narrow the list.</div>` : "") || '<div class="empty small">No models match these filters.</div>';
    $$("[data-star]", list).forEach((b) => (b.onclick = async () => {
      const id = b.dataset.star, s2 = starred();
      if (s2.has(id)) s2.delete(id); else s2.add(id);
      try { S.config = await api.saveSettings({ models: { openrouter: [...s2] } }); } catch (e) { toast("error", "Could not save", e.message); }
      draw();
    }));
    $$("[data-use]", m.body).forEach((b) => (b.onclick = () => use(b.dataset.use)));
  };
  const load = async (refresh) => {
    $("#orHint", m.body).textContent = "Loading the catalog…";
    try {
      data = await api.openrouterModels(refresh);
      const box = $("#orAuto", m.body);
      box.innerHTML = autoPicksHtml(data.auto_free, pick);
      const tgl = $("[data-show-picks]", box);
      if (tgl) tgl.onclick = () => { const open = tgl.getAttribute("aria-expanded") !== "true"; $$("[data-more-pick]", box).forEach((li) => (li.hidden = !open)); tgl.setAttribute("aria-expanded", open); tgl.textContent = open ? "Show the top three" : `Show the whole ranking (${data.auto_free.length - 3} more)`; };
      draw();
    }
    catch (e) { $("#orHint", m.body).textContent = e.message; }
    api.openrouterAccount(refresh).then((d) => { $("#orAcct", m.body).innerHTML = accountHtml(d); }).catch((e) => { $("#orAcct", m.body).innerHTML = `<span class="muted">${esc(e.message)}</span>`; });
  };
  $("#orQ", m.body).addEventListener("input", debounce((e) => { query = e.target.value.trim(); draw(); }, 120));
  $$("[data-f]", m.body).forEach((c) => c.addEventListener("change", () => { f[c.dataset.f] = c.checked; draw(); }));
  $("#orCtx", m.body).onchange = (e) => { f.ctx = Number(e.target.value); draw(); };
  $("#orSort", m.body).onchange = (e) => { sort = e.target.value; draw(); };
  $("#orRefresh", m.body).onclick = () => load(true);
  load(false);
  return m;
}

// The Agents page card: is OpenRouter set up, and which agents can run on it.
export function openRouterCard() {
  const meta = S.agentMeta || {};
  const ok = Object.keys(meta).filter((a) => (meta[a].openrouter || {}).ok);
  const no = Object.keys(meta).filter((a) => meta[a].openrouter && !meta[a].openrouter.ok);
  const p = (S.providers || {}).openrouter || {};
  return `<div class="card" id="orCard"><div class="card-head"><h3>${icon("layers", "sm")} OpenRouter</h3><span class="badge ${p.configured ? "green" : "amber"}">${p.configured ? "ready" : "no key"}</span></div><div class="card-body hint stack">
    <div>Any role can run its agent on OpenRouter instead of the agent's own sign-in: pick <strong>Runs on · OpenRouter</strong> in the team. ${p.configured ? `Default model: <code>${esc(orLabel(p.default_model))}</code>.` : 'An admin adds the key in <a href="#/settings/providers">Settings → Model providers</a>.'}</div>
    <div><strong>Runs on OpenRouter:</strong> ${ok.map((a) => esc(agentLabel(a))).join(", ")}.</div>
    ${no.length ? `<div><strong>Cannot:</strong> ${no.map((a) => `<span title="${esc(meta[a].openrouter.why || "")}">${esc(agentLabel(a))}</span>`).join(", ")} (hover for why).</div>` : ""}
    <div class="row wrap"><button class="btn sm" id="orBrowse">${icon("layers")}Models &amp; usage</button>${p.configured ? `<button class="btn sm" id="orTestAll" title="One short turn per installed agent, through OpenRouter">${icon("zap")}Test on OpenRouter</button>` : ""}<a class="btn sm" href="#/settings/providers">${icon("settings")}Provider settings</a></div>
  </div></div>`;
}
export function bindOpenRouterCard(root) {
  const b = $("#orBrowse", root);
  if (b) b.onclick = () => openOpenRouterBrowser();
  const t = $("#orTestAll", root);
  if (t) t.onclick = () => openOpenRouterTests();
}

// One "pong" turn per installed agent on OpenRouter's default model: proves the gateway, the recipe and the cost path.
export function openOpenRouterTests() {
  const ids = Object.keys(S.agentMeta || {}).filter((a) => orSupported(a) && (S.agents?.[a] || {}).installed);
  const def = ((S.providers || {}).openrouter || {}).default_model || AUTO_FREE;
  const m = modal(`<h2>${icon("zap")} Test agents on OpenRouter</h2>
    <p class="hint">Each agent gets one short turn on <code>${esc(orLabel(def))}</code> through Relay's gateway. Free models cost nothing but count against the daily free requests.</p>
    <div class="field"><label for="orTestModel">Model</label><input id="orTestModel" value="${esc(def)}" spellcheck="false"></div>
    <div class="md-table"><table class="or-tests"><thead><tr><th>Agent</th><th>Result</th><th>Model that answered</th><th class="num">Cost</th></tr></thead><tbody>
      ${ids.map((a) => `<tr data-row="${esc(a)}"><td>${esc(agentLabel(a))}</td><td class="muted">not run</td><td></td><td class="num"></td></tr>`).join("") || '<tr><td colspan="4" class="muted">No installed agent can run on OpenRouter.</td></tr>'}
    </tbody></table></div>
    <div class="modal-actions"><button class="btn" data-close>Close</button><button class="btn primary" id="orRun">${icon("play")}Run tests</button></div>`, { wide: true });
  $("#orRun", m.body).onclick = async (e) => {
    e.target.disabled = true;
    const model = $("#orTestModel", m.body).value.trim();
    for (const a of ids) {
      const row = $(`[data-row="${a}"]`, m.body);
      row.children[1].innerHTML = `${icon("spinner", "spin")} running`;
      try {
        const r = await api.testAgent(a, model, "openrouter");
        row.children[1].innerHTML = r.ok ? `<span class="ok-text">${icon("check", "sm")} replied in ${esc(r.seconds)}s</span>` : `<span class="err">${esc((r.error || "failed").slice(0, 300))}</span>`;
        row.children[2].innerHTML = r.models ? r.models.map((x) => `<code>${esc(x)}</code>`).join(" ") : "";
        row.children[3].textContent = r.cost_usd != null ? (Number(r.cost_usd) ? fmtCost(r.cost_usd) : "$0") : "";
      } catch (err) { row.children[1].innerHTML = `<span class="err">${esc(err.message)}</span>`; }
    }
    e.target.disabled = false;
  };
}

// ---------------------------------------------------------------------------- Settings → Model providers
export async function mountProviders(body) {
  body.innerHTML = '<div class="card"><div class="card-body"><span class="muted">Loading provider settings…</span></div></div>';
  let d;
  try { d = await api.openrouterSettings(); }
  catch (e) {
    body.innerHTML = `<div class="card"><div class="card-head"><h3>${icon("layers", "sm")} OpenRouter</h3></div><div class="card-body"><p class="hint">${e.status === 403 ? "Only admins can see and change provider settings, because they hold the API key. Ask an admin to set up OpenRouter." : esc(e.message)}</p></div></div>`;
    return;
  }
  const s = d.settings;
  const r = s.routing || {};
  const sw = (k, on, label) => `<span class="switch ${on ? "on" : ""}" data-sw="${k}" role="switch" aria-checked="${!!on}" tabindex="0" aria-label="${esc(label)}"></span>`;
  const supported = Object.entries(d.support || {}).filter(([, v]) => v.ok);
  const unsupported = Object.entries(d.support || {}).filter(([, v]) => !v.ok);
  body.innerHTML = `
    <div class="card" id="orKeyCard"><div class="card-head"><div><h3>${icon("layers", "sm")} OpenRouter</h3><p class="card-sub">One key, hundreds of models: any agent that supports it can run a role on OpenRouter. Only admins see this page.</p></div><span class="badge ${d.configured ? "green" : "amber"}" id="orState">${d.configured ? "ready" : "no key"}</span></div><div class="card-body">
      ${d.base_url_override ? `<div class="notice-inline warn">${icon("alert", "sm")} Development override: requests go to <code>${esc(d.base_url_override)}</code> (RELAY_OPENROUTER_BASE_URL), not openrouter.ai.</div>` : ""}
      <div class="field inline"><label>Use OpenRouter</label>${sw("enabled", s.enabled !== false, "Use OpenRouter")}</div>
      <div class="field"><label for="orKey">API key</label>
        <div class="row" style="gap:8px"><input id="orKey" type="password" autocomplete="off" spellcheck="false" placeholder="${s.has_api_key ? (s.key_source === "settings" ? `saved (${esc(s.key_hint)}) · type a new key to replace it` : `from the ${esc(s.key_source)} (${esc(s.key_hint)})`) : "sk-or-v1-…"}" style="flex:1;min-width:0">
          <button class="btn" id="orTest">${icon("zap")}Test</button>${s.key_source === "settings" ? `<button class="btn ghost" id="orClear" title="Remove the saved key">${icon("trash")}</button>` : ""}</div>
        <div class="help">Stored on this server only, never shown again and never given to an agent: agents talk to Relay's local gateway with a token that works for one turn. Create a key at openrouter.ai/settings/keys; a per-key credit limit there is a second safety net.</div>
        <div id="orTestOut" class="acct" style="margin-top:8px"></div></div>
      <div class="grid2">
        <div class="field"><label for="orDefault">Default model</label><div class="row" style="gap:8px"><input id="orDefault" value="${esc(s.default_model || "")}" placeholder="${AUTO_FREE}" style="flex:1;min-width:0" spellcheck="false"><button class="btn" id="orPickDefault">${icon("layers")}Browse</button></div>
          <div class="help">For roles on OpenRouter that leave the model blank. <code>${AUTO_FREE}</code> is the automatic best free model.</div></div>
        <div class="field"><label for="orConn">How agents connect</label><select id="orConn"><option value="proxy" ${s.connection !== "direct" ? "selected" : ""}>Through Relay's gateway (recommended)</option><option value="direct" ${s.connection === "direct" ? "selected" : ""}>Directly, with the key in the agent's environment</option></select>
          <div class="help">The gateway keeps the key away from agents, applies routing to every CLI, paces free models, waits out rate limits, rotates models and records the real cost of each request. Direct mode only gives the CLI the key.</div></div>
      </div>
    </div></div>
    <div class="card"><div class="card-head"><h3>Routing and privacy</h3></div><div class="card-body">
      <div class="grid3">
        <div class="field"><label for="orSortSel">Prefer providers by</label><select id="orSortSel">${[["", "OpenRouter's balancing (default)"], ["price", "Lowest price"], ["throughput", "Highest throughput"], ["latency", "Lowest latency"]].map(([v, l]) => `<option value="${v}" ${(r.sort || "") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="field"><label for="orOrder">Provider order</label><input id="orOrder" value="${esc((r.order || []).join(", "))}" placeholder="e.g. anthropic, openai" spellcheck="false"><div class="help">Try these provider slugs first.</div></div>
        <div class="field"><label for="orIgnore">Never use providers</label><input id="orIgnore" value="${esc((r.ignore || []).join(", "))}" placeholder="provider slugs" spellcheck="false"></div>
      </div>
      <div class="field inline"><label>Fall back to other providers of the same model when one is down</label>${sw("allow_fallbacks", r.allow_fallbacks !== false, "Allow provider fallbacks")}</div>
      <div class="field inline"><label>Only providers that support every parameter sent (tool calling included)</label>${sw("require_parameters", !!r.require_parameters, "Require parameters")}</div>
      <div class="field inline"><label>Deny data collection: only providers that do not store or train on prompts</label>${sw("data_collection", r.data_collection === "deny", "Deny data collection")}</div>
      <div class="field inline"><label>Zero data retention endpoints only</label>${sw("zdr", !!r.zdr, "Zero data retention")}</div>
      <p class="hint" style="margin:8px 0 0">Sent as OpenRouter's <code>provider</code> preferences with every request that goes through the gateway (and natively by OpenCode, Kilo, Crush, Aider and Continue in direct mode). Privacy settings always win over what a CLI asks for. Strict privacy narrows the providers a model has; a 503 then says so.</p>
    </div></div>
    <div class="card"><div class="card-head"><h3>Reliability</h3></div><div class="card-body">
      <div class="field"><label for="orFallbacks">Fallback models (in order)</label><input id="orFallbacks" value="${esc((s.fallback_models || []).join(", "))}" placeholder="e.g. qwen/qwen3-coder:free, openrouter/free" spellcheck="false">
        <div class="help">When a role's model is rate limited past the wait below, down (502/503) or gone (404), the gateway retries the same request on these, in order. The automatic free pick rotates through its own ranking instead.</div></div>
      <div class="grid3">
        <div class="field"><label for="orWait">Wait out rate limits for (seconds)</label><input id="orWait" type="number" min="0" max="600" value="${esc(s.rate_limit_wait_seconds ?? 60)}"><div class="help">Per request, honouring Retry-After, before moving to the next model.</div></div>
        <div class="field"><label for="orPace">Free model requests per minute</label><input id="orPace" type="number" min="1" max="600" value="${esc(s.free_rate_per_minute ?? 20)}"><div class="help">OpenRouter allows 20; Relay paces below it instead of collecting 429s.</div></div>
        <div class="field"><label for="orMinCtx">Automatic free pick: minimum context</label><input id="orMinCtx" type="number" min="8000" step="1000" value="${esc((s.auto_free || {}).min_context ?? 64000)}"></div>
      </div>
    </div></div>
    <div class="card"><div class="card-head"><h3>Spend caps and alerts</h3></div><div class="card-body">
      <div class="grid3">
        <div class="field"><label for="orCap">Monthly cap, all projects (USD)</label><input id="orCap" type="number" min="0" step="1" value="${esc(s.monthly_cap_usd || 0)}"><div class="help">0 = no cap. At the cap, paid OpenRouter models stop (free ones keep working) and queued tasks wait for next month or switch to a fallback agent.</div></div>
        <div class="field"><label for="orLow">Alert when credits fall below (USD)</label><input id="orLow" type="number" min="0" step="0.5" value="${esc(s.low_credit_usd ?? 1)}"><div class="help">One notification a day. 0 turns it off.</div></div>
      </div>
      <div class="field"><label>Monthly cap per project (USD)</label><div class="md-table"><table class="or-caps"><thead><tr><th>Project</th><th>Cap</th></tr></thead><tbody>
        ${(d.projects || []).map((p) => `<tr><td>${esc(p.name)}</td><td><input type="number" min="0" step="1" data-pcap="${esc(p.id)}" value="${esc((s.project_caps || {})[p.id] || "")}" placeholder="no cap" aria-label="Cap for ${esc(p.name)}"></td></tr>`).join("") || '<tr><td colspan="2" class="muted">No projects yet.</td></tr>'}
      </tbody></table></div><div class="help">Relay counts the real cost of every OpenRouter turn per project; crossing 50/80/100 % raises the usual budget alerts.</div></div>
    </div></div>
    <div class="card"><div class="card-head"><h3>Attribution</h3></div><div class="card-body">
      <div class="field inline"><label>Name Relay on the OpenRouter activity page (HTTP-Referer and X-Title headers)</label>${sw("attribution", s.attribution !== false, "Send attribution headers")}</div>
      <div class="grid2"><div class="field"><label for="orTitle">App title</label><input id="orTitle" value="${esc(s.app_title || "Relay")}"></div>
      <div class="field"><label for="orUrl">App URL</label><input id="orUrl" value="${esc(s.app_url || "")}" placeholder="Relay's public URL"><div class="help">Blank uses Relay's public URL; without one only the title is sent.</div></div></div>
    </div></div>
    <div class="card"><div class="card-head"><h3>Which agents can run on OpenRouter</h3></div><div class="card-body stack">
      <div class="md-table"><table><thead><tr><th>Agent</th><th>How</th></tr></thead><tbody>
        ${supported.map(([a, v]) => `<tr><td>${esc(agentLabel(a))}</td><td>${esc(v.how)}${v.note ? ` <span class="muted">· ${esc(v.note)}</span>` : ""}</td></tr>`).join("")}
        ${unsupported.map(([a, v]) => `<tr><td>${esc(agentLabel(a))}</td><td class="muted">Not supported: ${esc(v.why)}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="hint" style="margin:0">The owner's own sign-ins are never used or changed for an OpenRouter turn: every setting is per run. See docs/OPENROUTER.md.</p>
    </div></div>`;

  const state = { routing: { ...r }, flags: { enabled: s.enabled !== false, attribution: s.attribution !== false } };
  const status = (ok, text) => { const b = $("#orState", body); if (b && text) { b.textContent = text; b.className = `badge ${ok ? "green" : "amber"}`; } };
  const commit = async (patch, quiet) => {
    try {
      const res = await api.saveOpenrouterSettings(patch);
      S.providers = { ...(S.providers || {}), openrouter: { ...((S.providers || {}).openrouter || {}), configured: res.configured, default_model: res.settings.default_model, connection: res.settings.connection } };
      status(res.configured, res.configured ? "ready" : "no key");
      if (!quiet) toast("success", "OpenRouter settings saved");
      return res;
    } catch (e) { toast("error", "Could not save", e.message); return null; }
  };
  const list = (v) => v.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  const saveRouting = debounce(() => commit({ routing: { ...state.routing, order: list($("#orOrder", body).value), ignore: list($("#orIgnore", body).value), sort: $("#orSortSel", body).value } }, true), 400);
  $$("[data-sw]", body).forEach((el) => {
    const flip = () => {
      const on = !el.classList.contains("on");
      el.classList.toggle("on", on); el.setAttribute("aria-checked", on);
      const k = el.dataset.sw;
      if (k === "enabled" || k === "attribution") { state.flags[k] = on; commit({ [k]: on }, true); }
      else { state.routing[k] = k === "data_collection" ? (on ? "deny" : "allow") : on; saveRouting(); }
    };
    el.onclick = flip;
    el.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } };
  });
  ["#orSortSel", "#orOrder", "#orIgnore"].forEach((id) => $(id, body).addEventListener("change", saveRouting));
  $("#orConn", body).onchange = (e) => commit({ connection: e.target.value });
  $("#orDefault", body).onchange = (e) => commit({ default_model: e.target.value.trim() });
  $("#orPickDefault", body).onclick = () => openOpenRouterBrowser({ pick: true, current: $("#orDefault", body).value.trim() || AUTO_FREE, onPick: (id) => { $("#orDefault", body).value = id; commit({ default_model: id }); } });
  $("#orFallbacks", body).onchange = (e) => commit({ fallback_models: list(e.target.value) });
  $("#orWait", body).onchange = (e) => commit({ rate_limit_wait_seconds: Number(e.target.value) });
  $("#orPace", body).onchange = (e) => commit({ free_rate_per_minute: Number(e.target.value) });
  $("#orMinCtx", body).onchange = (e) => commit({ auto_free: { min_context: Number(e.target.value) } });
  $("#orCap", body).onchange = (e) => commit({ monthly_cap_usd: Number(e.target.value) || 0 });
  $("#orLow", body).onchange = (e) => commit({ low_credit_usd: Number(e.target.value) || 0 });
  $("#orTitle", body).onchange = (e) => commit({ app_title: e.target.value.trim() });
  $("#orUrl", body).onchange = (e) => commit({ app_url: e.target.value.trim() });
  $$("[data-pcap]", body).forEach((i) => i.addEventListener("change", () => {
    const caps = {};
    $$("[data-pcap]", body).forEach((x) => { if (Number(x.value) > 0) caps[x.dataset.pcap] = Number(x.value); });
    commit({ project_caps: caps });
  }));
  const keyInput = $("#orKey", body);
  keyInput.addEventListener("change", async () => {
    const v = keyInput.value.trim();
    if (!v) return;
    const res = await commit({ api_key: v });
    if (res) { keyInput.value = ""; keyInput.placeholder = `saved (${res.settings.key_hint}) · type a new key to replace it`; runTest(); }
  });
  const clear = $("#orClear", body);
  if (clear) clear.onclick = async () => { const res = await commit({ api_key: "" }); if (res) mountProviders(body); };
  async function runTest() {
    const out = $("#orTestOut", body);
    const btn = $("#orTest", body);
    btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Testing`;
    try {
      const typed = keyInput.value.trim();
      const a = await api.testOpenrouter(typed || "");
      out.innerHTML = a.ok ? accountHtml({ configured: true, account: a }, { relay: false }) + `<div class="acct-notes"><span class="ok-text">${icon("check", "sm")} The key works${typed ? " (not saved yet: press Enter or leave the field to save it)" : ""}.</span></div>`
        : `<div class="acct-notes"><span class="err">${esc(a.error || "The key did not work.")}</span></div>`;
    } catch (e) { out.innerHTML = `<div class="acct-notes"><span class="err">${esc(e.message)}</span></div>`; }
    btn.disabled = false; btn.innerHTML = `${icon("zap")}Test`;
  }
  $("#orTest", body).onclick = runTest;
}
