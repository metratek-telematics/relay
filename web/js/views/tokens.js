// Token efficiency: where prompt and output tokens go, cache hits, trend and the settings that send less
// (orchestrator/tokens.py, docs/TOKEN_EFFICIENCY.md). Self-contained: GET /api/tokens.
import { $, $$, esc, icon, toast } from "../ui.js";
import { get, api } from "../api.js";
import { agentLabel } from "../state.js";

const SECTION_LABEL = {
  rules: "Rules", protocol: "Session protocol", tools: "Tools", task: "Task", lessons: "Lessons", context: "Repositories & system map",
  design: "Design", environment: "Environment", packet: "Context packet / plan", judge: "Acceptance contract", report: "Worker report",
  verification: "Verification", diff: "Diff", guidance: "Guidance", instruction: "Instructions", transcript: "Replayed transcript", other: "Other",
};
const n = (v) => { v = Number(v) || 0; return v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e4 ? `${Math.round(v / 1e3)}k` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(v); };
const pct = (v) => `${Math.round((Number(v) || 0) * 100)}%`;
const usd = (v) => `$${(Number(v) || 0).toFixed((Number(v) || 0) < 1 ? 3 : 2)}`;

function bars(rows, { value, label, title, max }) {
  const top = max || Math.max(1, ...rows.map(value));
  return `<div class="tk-bars">${rows.map((r) => {
    const v = value(r);
    return `<div class="tk-bar" title="${esc(title(r))}"><span class="tk-bar-label truncate">${esc(label(r))}</span><span class="tk-track"><span class="tk-fill" style="width:${Math.max(0.5, (v / top) * 100).toFixed(1)}%"></span></span><span class="tk-bar-val">${esc(n(v))}</span></div>`;
  }).join("")}</div>`;
}

function table(rows, head) {
  return `<div class="tk-table-wrap"><table class="tk-table"><thead><tr>${head.map(([h, cls]) => `<th class="${cls || ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

export function mountTokens(body) {
  let days = 30;
  try { days = Number(localStorage.getItem("relay.tokenDays")) || 30; } catch {}
  const load = async () => {
    let d;
    try { d = await get(`/api/tokens?days=${days}`); } catch (e) { body.innerHTML = `<div class="card"><div class="card-body"><div class="empty small">${icon("alert")} ${esc(e.message)}</div></div></div>`; return; }
    draw(d);
  };
  const draw = (d) => {
    const t = d.total, s = d.settings || {};
    const secs = Object.entries(d.sections || {});
    const secTotal = secs.reduce((a, [, v]) => a + v.tokens, 0);
    const roleRows = Object.entries(d.roles || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v.turns}</td><td class="num">${n(v.prompt)}</td><td class="num">${pct(v.cache_ratio)}</td><td class="num">${n(v.output)}</td><td class="num">${n(v.turns ? v.prompt / v.turns : 0)}</td><td class="num">${usd(v.cost_usd)}</td></tr>`);
    const agentRows = Object.entries(d.agents || {}).map(([k, v]) => `<tr><td>${esc(agentLabel(k))}</td><td class="num">${v.turns}</td><td class="num">${n(v.prompt)}</td><td class="num">${pct(v.cache_ratio)}</td><td class="num">${n(v.output)}</td><td class="num">${n(v.turns ? v.prompt / v.turns : 0)}</td><td class="num">${usd(v.cost_usd)}</td></tr>`);
    const head = [["", ""], ["Turns", "num"], ["Prompt tokens", "num"], ["Cached", "num"], ["Output", "num"], ["Prompt / turn", "num"], ["Cost", "num"]];
    body.innerHTML = `
      <div class="card"><div class="card-head"><div><h3>Token efficiency</h3><p class="card-sub">Where the tokens of agent turns go, from what each CLI reported. Prompt tokens include cache reads; cached tokens cost about a tenth.</p></div>
        <select id="tkDays" aria-label="Period">${[7, 30, 90].map((x) => `<option value="${x}" ${x === days ? "selected" : ""}>Last ${x} days</option>`).join("")}</select></div>
        <div class="card-body">
          <div class="tk-kpis">
            <div class="tk-kpi"><div class="lbl">Prompt tokens</div><div class="val">${n(t.prompt)}</div><div class="sub">${t.turns} turns</div></div>
            <div class="tk-kpi"><div class="lbl">Cache hits</div><div class="val">${pct(t.cache_ratio)}</div><div class="sub">${n(t.cached)} cached</div></div>
            <div class="tk-kpi"><div class="lbl">Output tokens</div><div class="val">${n(t.output)}</div><div class="sub">${pct(t.output_share)} of all tokens</div></div>
            <div class="tk-kpi"><div class="lbl">Cost</div><div class="val">${usd(t.cost_usd)}</div><div class="sub">${t.turns ? usd(t.cost_usd / t.turns) : "$0"} per turn</div></div>
          </div>
        </div></div>
      <div class="card"><div class="card-head"><div><h3>What the prompts are made of</h3><p class="card-sub">Relay's own part of each prompt by section (characters ÷ 4), over ${d.measured_turns} measured turn${d.measured_turns === 1 ? "" : "s"}. The CLI's system prompt, tool definitions and the tool results inside a turn come on top.</p></div></div>
        <div class="card-body">${secs.length ? bars(secs, { value: ([, v]) => v.tokens, label: ([k]) => SECTION_LABEL[k] || k, title: ([k, v]) => `${SECTION_LABEL[k] || k}: ≈${v.tokens.toLocaleString()} tokens (${secTotal ? Math.round((v.tokens / secTotal) * 100) : 0}%)` })
          : `<div class="empty small">${icon("gauge", "lg")}<p>No measured turns yet. Every agent turn from now on records its prompt sections.</p></div>`}</div></div>
      <div class="card"><div class="card-head"><h3>By role</h3></div><div class="card-body">${roleRows.length ? table(roleRows, head) : '<div class="muted">No turns in this period.</div>'}</div></div>
      <div class="card"><div class="card-head"><h3>By agent</h3></div><div class="card-body">${agentRows.length ? table(agentRows, head) : '<div class="muted">No turns in this period.</div>'}</div></div>
      <div class="card"><div class="card-head"><div><h3>Trend</h3><p class="card-sub">Prompt tokens per day; hover a bar for cached, output and cost.</p></div></div><div class="card-body">${d.trend.length ? bars(d.trend, { value: (r) => r.prompt, label: (r) => r.day, title: (r) => `${r.day}: ${r.prompt.toLocaleString()} prompt · ${pct(r.cache_ratio)} cached · ${r.output.toLocaleString()} output · ${usd(r.cost_usd)} · ${r.turns} turns` }) : '<div class="muted">No turns in this period.</div>'}</div></div>
      <div class="card"><div class="card-head"><h3>Most expensive tasks</h3></div><div class="card-body">${d.tasks.length ? table(d.tasks.map((r) => `<tr><td class="tk-name"><a href="#/task/${esc(r.id)}">${r.number ? `#${esc(r.number)} ` : ""}${esc(r.name || r.id)}</a></td><td class="num">${r.turns}</td><td class="num">${n(r.prompt)}</td><td class="num">${pct(r.cache_ratio)}</td><td class="num">${n(r.output)}</td><td class="num">${n(r.turns ? r.prompt / r.turns : 0)}</td><td class="num">${usd(r.cost_usd)}</td></tr>`), [["Task", ""], ...head.slice(1)]) : '<div class="muted">No tasks in this period.</div>'}</div></div>
      <div class="card"><div class="card-head"><div><h3>Send less</h3><p class="card-sub">Applied to every new agent turn. See docs/TOKEN_EFFICIENCY.md for what each one does and what it saved.</p></div></div><div class="card-body">
        <div class="field inline"><label>Lean agent context (Claude)<div class="help">Agents run without your personal plugins, skills, slash commands and claude.ai connectors; the task's tools come from Relay. Measured: about 6k fewer tokens on every API call.</div></label><span class="switch ${s.token_lean_agent_context !== false ? "on" : ""}" data-tk="token_lean_agent_context" role="switch" tabindex="0"></span></div>
        <div class="field inline"><label>Do not re-send what a live session already has<div class="help">Unchanged acceptance contract and verification results become one line; the reply format is explained once.</div></label><span class="switch ${s.token_prompt_deltas !== false ? "on" : ""}" data-tk="token_prompt_deltas" role="switch" tabindex="0"></span></div>
        <div class="field inline"><label>Failing checks: send the lines that explain the failure<div class="help">The runner's failure summary and error lines, deduplicated, instead of the tail of the log.</div></label><span class="switch ${s.token_failure_excerpts !== false ? "on" : ""}" data-tk="token_failure_excerpts" role="switch" tabindex="0"></span></div>
        <div class="grid3">
          <div class="field"><label for="tkDiff">Diff for the reviewer</label><select id="tkDiff" data-tkv="token_diff_mode"><option value="targeted" ${s.token_diff_mode !== "full" ? "selected" : ""}>Targeted: stat, small files, git command for the rest</option><option value="full" ${s.token_diff_mode === "full" ? "selected" : ""}>Full diff up to the budget</option></select></div>
          <div class="field"><label for="tkCompact">Compact a session above (tokens)</label><input id="tkCompact" type="number" min="0" step="10000" data-tkv="token_compact_threshold" value="${esc(s.token_compact_threshold ?? 120000)}"><div class="help">A fresh session starts from Relay's handoff. 0 turns it off.</div></div>
          <div class="field"><label for="tkVerb">Codex verbosity</label><select id="tkVerb" data-tkv="token_codex_verbosity">${["", "low", "medium", "high"].map((v) => `<option value="${v}" ${(s.token_codex_verbosity || "") === v ? "selected" : ""}>${v || "CLI default"}</option>`).join("")}</select></div>
          <div class="field"><label for="tkClaudeOut">Claude max output per response</label><input id="tkClaudeOut" type="number" min="0" step="1000" data-tkv="token_claude_max_output_tokens" value="${esc(s.token_claude_max_output_tokens || 0)}"><div class="help">0 keeps Claude Code's default. Too low truncates large file writes.</div></div>
        </div>
      </div></div>`;
    $("#tkDays", body).onchange = (e) => { days = Number(e.target.value); try { localStorage.setItem("relay.tokenDays", String(days)); } catch {} load(); };
    const save = async (patch) => { try { await api.saveSettings(patch); toast("success", "Saved"); } catch (e) { toast("error", "Could not save", e.message); } };
    $$("[data-tk]", body).forEach((sw) => {
      const flip = () => { sw.classList.toggle("on"); save({ [sw.dataset.tk]: sw.classList.contains("on") }); };
      sw.onclick = flip; sw.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } };
    });
    $$("[data-tkv]", body).forEach((i) => (i.onchange = () => save({ [i.dataset.tkv]: i.type === "number" ? Number(i.value) || 0 : i.value })));
  };
  body.innerHTML = `<div class="card"><div class="card-body"><div class="empty small">${icon("spinner", "spin")} Loading…</div></div></div>`;
  load();
}
