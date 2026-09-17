// Learning advice for the New task wizard: a recommended team and a pre-flight risk check.
// Self-contained (markup + behaviour) so a redesigned wizard can place it anywhere; styles in /learning.css.
// Numbers come from orchestrator/recommend.py and orchestrator/risk.py (formulas in their docstrings).
import { $, $$, esc, icon, toast } from "../ui.js";
import { agentLabel } from "../state.js";
import { api } from "../api.js";

const MODES = [["best", "Best"], ["balanced", "Balanced"], ["cheapest_good_enough", "Cheapest good enough"]];
const pct = (x) => (x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`);
const money = (x) => (x === null || x === undefined ? "—" : `$${Number(x).toFixed(2)}`);

export function teamLabel(roles) {
  return ["supervisor", "worker", "reviewer"].filter((r) => roles?.[r]?.agent).map((r) => {
    const x = roles[r]; const extra = [x.model, x.effort].filter(Boolean).join(" ");
    return `${agentLabel(x.agent)}${extra ? ` (${extra})` : ""}`;
  }).join(" → ");
}

// Draft payload the preflight endpoint expects.
const draftBody = (d, mode) => ({ repo: d.repo, template: d.template, requirements: d.requirements, issue: d.issue,
  repos: (d.repos || []).map((r) => ({ repo: r.repo })), workflow: d.workflow, mode });

// ---------------------------------------------------------------------------- team recommendation
// host: element · getDraft(): current wizard data · onUse(roles): apply a team to the draft
export function mountTeamAdvice(host, getDraft, { onUse } = {}) {
  let mode = null, data = null, alive = true;
  host.classList.add("lx-advice");
  const load = async () => {
    host.innerHTML = `<div class="lx-advice-head">${icon("sparkles", "sm")}<strong>Recommended team</strong><span class="muted">reading past outcomes…</span></div>`;
    try { data = await api.learningPreflight(draftBody(getDraft(), mode)); } catch (e) { if (alive) host.innerHTML = `<div class="lx-advice-head">${icon("sparkles", "sm")}<strong>Recommended team</strong><span class="muted">unavailable: ${esc(e.message)}</span></div>`; return; }
    if (!alive) return;
    mode = mode || data.recommendation.mode;
    draw();
  };
  const draw = () => {
    const rec = data.recommendation, pick = rec.pick;
    if (!pick) { host.innerHTML = ""; return; }
    const cur = data.current;
    const same = cur && cur.team_key === pick.team_key;
    const rows = rec.candidates.filter((c) => c.available).slice(0, 5);
    host.innerHTML = `<div class="lx-advice-head">${icon("sparkles", "sm")}<strong>Recommended team</strong>
        <span class="lx-modes" role="tablist">${MODES.map(([k, l]) => `<button type="button" class="chip ${mode === k ? "active" : ""}" data-lx-mode="${k}" role="tab" aria-selected="${mode === k}">${l}</button>`).join("")}</span></div>
      <div class="lx-pick">
        <div class="lx-pick-main"><div class="lx-team">${esc(teamLabel(pick.roles))}${pick.pinned ? ' <span class="badge purple">pinned for this repository</span>' : ""}${same ? ' <span class="badge green">selected</span>' : ""}</div>
          <div class="lx-why">${esc(pick.why)}</div></div>
        <div class="lx-nums"><span><b>${Math.round(pick.expected_score)}</b>expected</span><span><b>${pct(pick.success_chance)}</b>success</span><span><b>${money(pick.median_cost)}</b>${pick.cost_estimated ? "est. cost" : "median cost"}</span></div>
        ${same ? "" : `<button type="button" class="btn sm primary" data-lx-use="${esc(pick.team_key)}">${icon("check")}Use this team</button>`}
      </div>
      ${cur && !same ? `<div class="lx-current muted">Your current team: ${esc(cur.why)}</div>` : ""}
      <details class="lx-cands"><summary>Compare ${rows.length} teams · ${data.records} past run${data.records === 1 ? "" : "s"} · prior ${Math.round(rec.prior.score)} avg</summary>
        <table class="lx-table"><thead><tr><th>Team</th><th>Expected</th><th>Success</th><th>Cost</th><th>Runs here</th><th></th></tr></thead><tbody>
        ${rows.map((c) => `<tr><td>${esc(teamLabel(c.roles))}</td><td>${Math.round(c.expected_score)}</td><td>${pct(c.success_chance)}</td><td>${money(c.median_cost)}${c.cost_estimated ? "*" : ""}</td><td>${c.runs_on_repo}</td>
          <td><button type="button" class="btn xs" data-lx-use="${esc(c.team_key)}">Use</button></td></tr>`).join("")}</tbody></table>
        <p class="help">Bayesian average of similar runs (same repository weighs most, then task type and size), shrunk towards the global average; * cost estimated from similar teams.</p></details>`;
    $$("[data-lx-mode]", host).forEach((b) => (b.onclick = () => { mode = b.dataset.lxMode; load(); }));
    $$("[data-lx-use]", host).forEach((b) => (b.onclick = () => {
      const c = rec.candidates.find((x) => x.team_key === b.dataset.lxUse); if (!c) return;
      const roles = {}; for (const r of ["supervisor", "worker", "reviewer"]) roles[r] = { agent: c.roles[r]?.agent || "", model: c.roles[r]?.model || "", effort: c.roles[r]?.effort || "" };
      onUse && onUse(roles, c);
    }));
  };
  load();
  return { refresh: load, destroy() { alive = false; } };
}

// ---------------------------------------------------------------------------- pre-flight risk
// onChange(patch): {requirements?, workflow?} to merge into the wizard data, then the check re-runs.
export function mountRiskCheck(host, getDraft, { onChange } = {}) {
  let alive = true, data = null;
  host.classList.add("lx-risk");
  const answers = new Map();
  const load = async () => {
    try { data = await api.learningPreflight(draftBody(getDraft())); } catch (e) { if (alive) host.innerHTML = ""; return; }
    if (!alive) return;
    draw();
  };
  const draw = () => {
    const r = data.risk;
    if (!r) { host.innerHTML = ""; return; }
    const tone = { low: "green", medium: "amber", high: "red" }[r.level];
    const clar = r.mitigations.find((m) => m.id === "clarify");
    host.innerHTML = `<div class="lx-risk-head"><span class="badge ${tone}">${esc(r.level)} risk</span><strong>Pre-flight check</strong>
        <span class="muted">${pct(r.p_fail)} chance of failing or scoring under 60 · base ${pct(r.base)} (${esc(r.base_text)})</span></div>
      ${r.factors.length ? `<ul class="lx-factors">${r.factors.map((f) => `<li><span>${esc(f.text)}</span><span class="muted mono">+${f.weight}</span></li>`).join("")}</ul>` : '<p class="muted lx-none">Nothing in the request or the history suggests extra risk.</p>'}
      ${r.mitigations.length ? `<div class="lx-mitigations">${r.mitigations.filter((m) => m.id !== "clarify").map((m) => mitigation(m)).join("")}</div>` : ""}
      ${clar ? `<div class="lx-clarify"><div class="field-label">${icon("question", "sm")}Clarify before the team starts <span class="muted">(answers are added to the request)</span></div>
        ${clar.questions.map((q, i) => `<label class="lx-q"><span>${esc(q)}</span><textarea rows="2" data-lx-q="${i}" placeholder="Your answer (optional)">${esc(answers.get(i) || "")}</textarea></label>`).join("")}
        <button type="button" class="btn sm" id="lxAddAnswers">${icon("plus")}Add answers to the request</button></div>` : ""}`;
    $$("[data-lx-q]", host).forEach((t) => t.addEventListener("input", () => answers.set(Number(t.dataset.lxQ), t.value)));
    const add = $("#lxAddAnswers", host);
    if (add) add.onclick = () => {
      const lines = clar.questions.map((q, i) => [q, (answers.get(i) || "").trim()]).filter(([, a]) => a);
      if (!lines.length) { toast("warning", "Answer at least one question"); return; }
      const d = getDraft();
      const block = `\n\nClarifications:\n${lines.map(([q, a]) => `- ${q}\n  ${a}`).join("\n")}`;
      answers.clear();
      onChange && onChange({ requirements: (d.requirements || "").trimEnd() + block });
      toast("success", "Added to the request"); load();
    };
    $$("[data-lx-act]", host).forEach((b) => (b.onclick = async () => {
      const d = getDraft(), act = b.dataset.lxAct;
      if (act === "add_reviewer" || act === "use_recommended") {
        const pf = await api.learningPreflight(draftBody(d, act === "add_reviewer" ? "best" : null));
        const pick = pf.recommendation.pick;
        const wf = JSON.parse(JSON.stringify(d.workflow));
        if (act === "use_recommended" && pick) for (const r of ["supervisor", "worker", "reviewer"]) wf.roles[r] = { agent: pick.roles[r]?.agent || "", model: pick.roles[r]?.model || "", effort: pick.roles[r]?.effort || "" };
        if (act === "add_reviewer") {
          const rev = pick?.roles?.reviewer?.agent || (["codex", "claude", "gemini"].find((a) => a !== wf.roles.worker.agent) || "codex");
          wf.roles.reviewer = { agent: rev, model: "", effort: "" }; wf.preset = "custom";
        }
        onChange && onChange({ workflow: wf });
        toast("success", act === "add_reviewer" ? `Added ${agentLabel(wf.roles.reviewer.agent)} as independent reviewer` : "Recommended team applied");
        load();
      }
    }));
  };
  const mitigation = (m) => {
    const action = ["add_reviewer", "use_recommended"].includes(m.id) ? `<button type="button" class="btn xs" data-lx-act="${m.id}">Apply</button>`
      : m.href ? `<a class="btn xs" href="${esc(m.href)}" target="_blank" rel="noopener">Open</a>` : "";
    return `<div class="lx-mit">${icon(m.id === "split" ? "layers" : m.id === "design" ? "wand" : m.id === "stack" ? "cpu" : m.id === "fix_environment" ? "settings" : "shield", "sm")}<span>${esc(m.label)}</span>${action}</div>`;
  };
  load();
  return { refresh: load, destroy() { alive = false; } };
}
