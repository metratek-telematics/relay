// Settings: workflow defaults, agents, verification, git/github, appearance, rules, about.
import { $, $$, esc, icon, toast, confirm, debounce } from "../ui.js";
import { S, agentLabel, agentInitial, agentIds, bus } from "../state.js";
import { api } from "../api.js";
import { workflowEditor } from "./newtask.js";
import { NOTIFY_EVENTS, chime, permission, requestPermission, showDesktop } from "../notify.js";
import { openShortcuts, keysFor } from "../shortcuts.js";
import { mountConnectors } from "./connectors.js";
import { mountTools } from "./tools.js";
import { mountTokens } from "./tokens.js";
import { mountProviders } from "./openrouter.js";

const SECTIONS = [["workflow", "Team and workflow", "layers"], ["agents", "Agents and models", "bot"], ["verification", "Verification", "shield"], ["prompts", "Saved prompts", "message"], ["rules", "Rules", "docs"], ["autopilot", "Autopilot", "clock"], ["budget", "Usage and budget", "gauge"], ["notifications", "Notifications", "bell"], ["git", "Git and GitHub", "github"], ["providers", "Model providers", "layers"], ["tools", "Agent tools", "package"], ["tokens", "Token efficiency", "gauge"], ["workspace", "Workspace and access", "user"], ["appearance", "Appearance", "sun"], ["about", "About", "info"]];
const GROUPS = [["Team", ["workflow", "agents", "tools", "verification", "prompts", "rules"]], ["Automation", ["autopilot", "budget", "tokens", "notifications"]], ["Integrations", ["providers", "git", "workspace"]], ["You", ["appearance", "about"]]];
// Words each section answers to, so the search finds "quiet hours" under Autopilot.
const KEYWORDS = {
  workflow: "preset supervisor worker reviewer team max turns review rounds approval questions design step learning retrospective lessons scorecard parallel exploration mockups focus group personas directions",
  agents: "codex claude gemini model effort login account subagent timeout sandbox cli arguments",
  verification: "tests checks commands detect lint build design gate forbidden terms",
  prompts: "templates saved prompts snippets",
  rules: "engineering rules design frontend security prompt injection",
  autopilot: "schedule window quiet hours limits fallback cost cap digest watchdog park retry",
  budget: "cost pricing tokens usage spend subagent model cheap",
  notifications: "desktop sound alerts delivered failed questions",
  git: "github pull request pr draft branch push label intake watch issues commit",
  appearance: "theme dark light density compact keyboard shortcuts",
  about: "version data folder archive",
  tools: "toolbox mcp servers cli tools requests install catalog",
  tokens: "token efficiency cache prompt size context compression",
  providers: "openrouter api key provider routing models free credits spend cap fallback privacy data collection zdr",
  workspace: "people roles users projects audit log integrations slack telegram email api tokens usage budgets sso onboarding",
};

// Scorecards, retrospectives and lessons (orchestrator/learning.py).
function learningCard(c) {
  const agent = c.retro_agent || "";
  const efforts = agent ? (S.agentMeta[agent] || {}).efforts || [] : [];
  const cat = agent ? (c.models || {})[agent] || [] : [...new Set(Object.values(c.models || {}).flat())];
  return `<div class="card" id="learning"><div class="card-head"><div><h3>Learning from finished tasks</h3><p class="card-sub">Every finished task gets a scorecard. A retrospective then proposes lessons for you to review in <a href="#/knowledge/lessons">Knowledge → Lessons</a>.</p></div></div><div class="card-body">
    <div class="field inline"><label>Run a short retrospective after each task</label><span class="switch ${c.retro_enabled !== false ? "on" : ""}" data-sw-cfg="retro_enabled"></span></div>
    <div class="grid3">
      <div class="field"><label>Retrospective agent</label><select data-cfg="retro_agent"><option value="">The task's supervisor</option>${agentIds().map((a) => `<option value="${esc(a)}" ${agent === a ? "selected" : ""}>${esc(agentLabel(a))}</option>`).join("")}</select><div class="help">One turn with no tools, run in the background after the result is recorded.</div></div>
      <div class="field"><label>Model</label><input list="retroModels" data-cfg="retro_model" value="${esc(c.retro_model || "")}" placeholder="cheap subagent model"><datalist id="retroModels">${cat.map((m) => `<option value="${esc(m)}">`).join("")}</datalist><div class="help">Blank uses the agent's subagent model (Usage &amp; budget), else the supervisor's model. A small model is enough.</div></div>
      <div class="field"><label>Effort</label>${efforts.length ? `<select data-cfg="retro_effort"><option value="">Lowest (${esc(efforts[0])})</option>${efforts.map((e) => `<option value="${esc(e)}" ${c.retro_effort === e ? "selected" : ""}>${esc(e)}</option>`).join("")}</select>` : `<select disabled><option>${agent ? "not supported by this CLI" : "lowest the agent offers"}</option></select>`}</div>
    </div>
    <div class="grid3">
      <div class="field"><label>Lessons proposed per task (at most)</label><input type="number" min="0" max="5" data-cfg="retro_max_lessons" value="${esc(c.retro_max_lessons ?? 3)}"></div>
      <div class="field"><label>Lessons in a prompt (at most)</label><input type="number" min="1" max="40" data-cfg="lessons_max_in_prompt" value="${esc(c.lessons_max_in_prompt ?? 15)}"></div>
      <div class="field"><label>Re-check pull requests every (minutes)</label><input type="number" min="5" data-cfg="scorecard_refresh_minutes" value="${esc(c.scorecard_refresh_minutes ?? 30)}"><div class="help">Merged, closed and later human commits change the score.</div></div>
    </div>
    <div class="field inline"><label>Add approved lessons to supervisor and worker prompts</label><span class="switch ${c.lessons_inject !== false ? "on" : ""}" data-sw-cfg="lessons_inject"></span></div>
  </div></div>`;
}

export function mountSettings(main, section) {
  let cur = SECTIONS.some(([k]) => k === section) ? section : "workflow";
  main.innerHTML = `<div class="page settings-page"><header class="page-header"><div class="ph-title"><div class="eyebrow">Settings</div><h1>How Relay works for you</h1><p class="ph-sub">Defaults for new tasks; existing tasks keep their own workflow. Connectors and repositories live in <a href="#/knowledge/connectors">Knowledge</a>.</p></div><div class="ph-actions"><span class="save-state" id="saveState" role="status" aria-live="polite"></span></div></header>
    <div class="settings"><div class="settings-side"><label class="search-field sm">${icon("search")}<input id="sSearch" type="search" placeholder="Search settings" autocomplete="off" aria-label="Search settings"></label><nav class="settings-nav" id="sNav" aria-label="Settings sections"></nav></div><div class="settings-main" id="sMain"></div></div></div>`;
  const nav = $("#sNav", main), body = $("#sMain", main);
  const saved = (ok = true, msg) => { const s = $("#saveState", main); s.textContent = msg || (ok ? "Saved" : "Save failed"); s.style.color = ok ? "var(--green)" : "var(--red)"; setTimeout(() => { if (s.textContent === "Saved") s.textContent = ""; }, 2000); };
  const save = async (partial) => { try { S.config = await api.saveSettings(partial); saved(true); bus.emit("config"); } catch (e) { saved(false); toast("error", "Could not save", e.message); } };
  const bindAuto = () => {
    $$("[data-cfg]", body).forEach((i) => {
      const k = i.dataset.cfg;
      const handler = () => {
        let v = i.type === "checkbox" ? i.checked : i.type === "number" || i.dataset.num ? Number(i.value) : i.value;
        if (i.dataset.list) v = v.split("\n").map((x) => x.trim()).filter(Boolean);
        if (i.dataset.args) v = v.trim() ? v.trim().split(/\s+/) : [];
        save({ [k]: v });
      };
      i.addEventListener(i.tagName === "TEXTAREA" || i.type === "text" ? "change" : "change", handler);
    });
    $$("[data-sw-cfg]", body).forEach((s) => (s.onclick = () => { s.classList.toggle("on"); save({ [s.dataset.swCfg]: s.classList.contains("on") }); }));
  };

  let query = "";
  const matches = (k) => { if (!query) return true; const [, l] = SECTIONS.find(([x]) => x === k); return `${l} ${KEYWORDS[k] || ""}`.toLowerCase().includes(query); };
  function drawNav() {
    nav.innerHTML = GROUPS.map(([g, keys]) => {
      const rows = keys.filter(matches).map((k) => SECTIONS.find(([x]) => x === k)).filter(Boolean);
      return rows.length ? `<div class="sn-group"><div class="sn-label">${esc(g)}</div>${rows.map(([k, l, i]) => `<a href="#/settings/${k}" class="${k === cur ? "active" : ""}" ${k === cur ? 'aria-current="page"' : ""}>${icon(i)}${esc(l)}</a>`).join("")}</div>` : "";
    }).join("") || `<p class="muted small sn-none">No setting matches “${esc(query)}”.</p>`;
  }
  // Inside the open section, cards that do not mention the search words fold away.
  function filterBody() {
    for (const card of $$(".settings-main > .card", main)) card.hidden = !!query && !card.textContent.toLowerCase().includes(query) && !(KEYWORDS[cur] || "").includes(query);
  }
  $("#sSearch", main).addEventListener("input", (e) => { query = e.target.value.trim().toLowerCase(); drawNav(); filterBody(); });
  $("#sSearch", main).addEventListener("keydown", (e) => { if (e.key === "Enter") { const first = $("a", nav); if (first) location.hash = first.getAttribute("href"); } });
  new MutationObserver(() => query && filterBody()).observe(body, { childList: true });

  function render() {
    const c = S.config || {};
    drawNav();
    if (cur === "workflow") {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Default team</h3></div><div class="card-body"><div id="wfEd"></div></div></div>
        <div class="card"><div class="card-head"><h3>Turn behaviour</h3></div><div class="card-body">
          <div class="grid3">
            <div class="field"><label>Agent turn timeout (minutes)</label><input type="number" min="5" data-cfg="agent_turn_timeout_minutes" value="${esc(c.agent_turn_timeout_minutes)}"><div class="help">A turn that runs longer is terminated and the agent is asked to continue in smaller steps.</div></div>
            <div class="field"><label>Quiet warning (minutes)</label><input type="number" min="1" data-cfg="stall_warning_minutes" value="${esc(c.stall_warning_minutes)}"><div class="help">Flag an agent that produces no output for this long.</div></div>
            <div class="field"><label>Envelope retries</label><input type="number" min="0" max="5" data-cfg="envelope_retries" value="${esc(c.envelope_retries)}"><div class="help">How many times to ask an agent to restate a missing JSON envelope.</div></div>
            <div class="field"><label>Unanswered judge questions</label><input type="number" min="0" max="1440" data-cfg="judge_escalation_timeout_minutes" value="${esc(c.judge_escalation_timeout_minutes ?? 120)}"><div class="help">Minutes before Relay takes the safe automatic choice when nobody answers (extend the budget once, then deliver with follow-ups). 0 waits for you.</div></div>
          </div>
          <div class="grid2">
            <div class="field"><label>Parallel tasks</label><input type="number" min="1" max="8" data-cfg="max_parallel" value="${esc(c.max_parallel)}" style="width:100px"></div>
            <div class="field"><label>Express lane</label><input type="number" min="0" max="4" data-cfg="express_lane" value="${esc(c.express_lane ?? 1)}" style="width:100px"><div class="help">Extra slots for tasks triaged solo, so a small task starts beside a long one instead of waiting behind it. 0 turns it off.</div></div>
          </div>
        </div></div>
        <div class="card"><div class="card-head"><h3>Design step</h3></div><div class="card-body">
          <div class="grid2">
            <div class="field"><label>Design revisions before you decide</label><input type="number" min="0" max="5" data-cfg="design_max_revisions" value="${esc(c.design_max_revisions ?? 2)}"><div class="help">Blocking design-review findings send the design back to the supervisor this many times; then the design review asks you.</div></div>
            <div class="field"><label>Design document in pull requests</label><select data-cfg="design_doc_mode">${[["commit", "Commit docs/designs/<date>-<slug>.md to the primary repository"], ["comment", "Post it as a comment on the primary pull request"], ["off", "Keep it in Relay only"]].map(([v, l]) => `<option value="${v}" ${(c.design_doc_mode || "commit") === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select><div class="help">Every pull request links it from its Change set section, with the merge order across repositories.</div></div>
          </div>
        </div></div>
        ${explorationCard(c)}
        ${learningCard(c)}`;
      const wf = { preset: c.workflow_preset, roles: JSON.parse(JSON.stringify(c.roles || {})), max_turns: c.max_turns, max_review_rounds: c.max_review_rounds, verify_mode: c.verify_mode, approval_before_delivery: c.approval_before_delivery, allow_agent_questions: c.allow_agent_questions, redeploy_on_merge: !!c.redeploy_default_on, verification_commands: c.verification_commands || [], auto_detect_verification: c.auto_detect_verification, design_mode: c.design_mode, design_approval: c.design_approval, team_mode: c.team_mode || "auto" };
      const persist = debounce((w) => save({ workflow_preset: w.preset, roles: JSON.parse(JSON.stringify(w.roles)), max_turns: w.max_turns, max_review_rounds: w.max_review_rounds, verify_mode: w.verify_mode, approval_before_delivery: w.approval_before_delivery, allow_agent_questions: w.allow_agent_questions, redeploy_default_on: !!w.redeploy_on_merge, verification_commands: w.verification_commands, auto_detect_verification: w.auto_detect_verification, design_mode: w.design_mode, design_approval: w.design_approval, team_mode: w.team_mode || "auto" }), 400);
      workflowEditor($("#wfEd", body), wf, { agents: S.agentMeta, presets: S.presets, onChange: persist });
      bindAuto();
      // The model catalogue and efforts depend on the agent, so redraw once the new agent is saved.
      $("[data-cfg='retro_agent']", body).addEventListener("change", () => setTimeout(render, 300));
    } else if (cur === "autopilot") {
      body.innerHTML = autopilotSettings(c);
      bindAutopilot(body, c, save, render);
      bindAuto();
    } else if (cur === "agents") {
      const env = c.agent_env || {};
      const envRows = (a) => Object.entries(env[a] || {}).concat([["", ""]]).map(([k, v]) => `<div class="env-row"><input placeholder="VARIABLE" value="${esc(k)}" data-env-k="${a}"><input placeholder="value" value="${esc(v)}" data-env-v="${a}"><button type="button" class="btn xs" data-env-del="${a}" title="Remove">${icon("x")}</button></div>`).join("");
      // Model / effort / subagent model for one agent, in one place.
      const catalogField = (a) => {
        const cat = (c.models || {})[a] || [];
        const d = (c.agent_defaults || {})[a] || {};
        const efforts = (S.agentMeta[a] || {}).efforts || [];
        const cliModel = S.agents?.[a]?.default_model;
        const cliEffort = S.agents?.[a]?.default_effort;
        const custom = d.model && !cat.includes(d.model);
        const subs = (c.subagent_models || {})[a] || "";
        return `<div class="grid3">
          <div class="field"><label>Default model</label>
            <select data-adef="${a}.model"><option value="">${esc(cliModel ? `CLI default (${cliModel})` : "CLI default")}</option>${cat.map((m) => `<option value="${esc(m)}" ${d.model === m ? "selected" : ""}>${esc(m)}</option>`).join("")}<option value="__custom__" ${custom ? "selected" : ""}>Custom&hellip;</option></select>
            <input data-adef-custom="${a}" value="${esc(custom ? d.model : "")}" placeholder="exact model id" ${custom ? "" : "hidden"} style="margin-top:6px">
            <div class="help">Used whenever a role leaves its model blank.</div></div>
          <div class="field"><label>Default reasoning effort</label>
            ${efforts.length
              ? `<select data-adef="${a}.effort"><option value="">${esc(cliEffort ? `CLI default (${cliEffort})` : "CLI default")}</option>${efforts.map((e) => `<option value="${e}" ${d.effort === e ? "selected" : ""}>${e}</option>`).join("")}</select><div class="help">Lower effort spends fewer output tokens.</div>`
              : `<select disabled><option>not supported by this CLI</option></select>`}</div>
          <div class="field"><label>Subagent model <span class="badge green">saves tokens</span></label>
            ${a !== "claude"
              ? `<input disabled placeholder="not supported"><div class="help">${a === "codex" ? "Codex has no setting for its helpers' model." : "Gemini has no subagent model setting."}</div>`
              : `<input list="subcat-${a}" data-sub="${a}" value="${esc(subs)}" placeholder="${a === "codex" ? "gpt-5.1-codex-mini" : "haiku"}"><datalist id="subcat-${a}">${cat.map((m) => `<option value="${esc(m)}">`).join("")}</datalist><div class="help">Helper agents spawned mid-turn use this cheaper model.</div>`}</div>
        </div>
        <div class="field"><label>Model catalog (one per line)</label><textarea data-models="${a}" rows="3" style="font-family:var(--mono);font-size:12px">${esc(cat.join("\n"))}</textarea><div class="help">Options offered in every model picker.</div></div>`;
      };
      body.innerHTML = `
        <div class="card"><div class="card-head"><h3><span class="av sm codex">Cx</span> Codex</h3><span class="badge ${S.agents?.codex?.ok ? "green" : "red"}">${esc(S.agents?.codex?.version || "missing")}</span></div><div class="card-body">
          <div class="grid2"><div class="field"><label>Sandbox</label><select data-cfg="codex_sandbox"><option value="workspace-write" ${c.codex_sandbox === "workspace-write" ? "selected" : ""}>workspace-write (recommended)</option><option value="danger-full-access" ${c.codex_sandbox === "danger-full-access" ? "selected" : ""}>danger-full-access (no sandbox)</option></select><div class="help">workspace-write lets Codex edit the worktree and run commands there without approvals.</div></div>
          <div class="field"><label>Reasoning effort</label><select data-cfg="codex_reasoning_effort"><option value="" ${!c.codex_reasoning_effort ? "selected" : ""}>CLI default</option>${["low", "medium", "high", "xhigh"].map((x) => `<option ${c.codex_reasoning_effort === x ? "selected" : ""}>${x}</option>`).join("")}</select></div></div>
          <div class="field"><label>Extra CLI arguments</label><input data-cfg="codex_extra_args" data-args="1" value="${esc((c.codex_extra_args || []).join(" "))}" placeholder="-c key=value"></div>
          ${catalogField("codex")}
          <div class="field"><label>Environment variables for Codex</label><div class="env-table" id="env-codex">${envRows("codex")}</div></div>
        </div></div>
        <div class="card"><div class="card-head"><h3><span class="av sm claude">Cl</span> Claude Code</h3><span class="badge ${S.agents?.claude?.ok ? "green" : "red"}">${esc(S.agents?.claude?.version || "missing")}</span></div><div class="card-body">
          <div class="grid2"><div class="field"><label>Permissions</label><select data-cfg="claude_permission"><option value="bypass" ${c.claude_permission === "bypass" ? "selected" : ""}>bypass all permission prompts (unattended)</option><option value="acceptEdits" ${c.claude_permission === "acceptEdits" ? "selected" : ""}>acceptEdits (some tools may be denied)</option></select></div>
          <div class="field"><label>Max internal turns per call</label><input type="number" min="10" max="1000" data-cfg="claude_max_turns_per_call" value="${esc(c.claude_max_turns_per_call)}"><div class="help">Safety cap on tool loops inside one work package.</div></div></div>
          <div class="field inline"><label>Prefer Claude subscription login (ignore ANTHROPIC_API_KEY)</label><span class="switch ${c.prefer_claude_subscription_auth ? "on" : ""}" data-sw-cfg="prefer_claude_subscription_auth"></span></div>
          <div class="grid2"><div class="field"><label>Setting sources</label><select data-cfg="claude_setting_sources"><option value="" ${!c.claude_setting_sources ? "selected" : ""}>All (user, project, local)</option><option value="project,local" ${c.claude_setting_sources === "project,local" ? "selected" : ""}>Project + local only (isolate from personal hooks/MCP)</option><option value="user" ${c.claude_setting_sources === "user" ? "selected" : ""}>User only</option></select><div class="help">Personal hooks that echo prompts through a shell can leave artifacts in worktrees; isolating avoids that.</div></div>
          <div class="field"><label>Extra CLI arguments</label><input data-cfg="claude_extra_args" data-args="1" value="${esc((c.claude_extra_args || []).join(" "))}" placeholder="--mcp-config path.json"></div></div>
          ${catalogField("claude")}
          <div class="field"><label>Environment variables for Claude</label><div class="env-table" id="env-claude">${envRows("claude")}</div></div>
        </div></div>
        <div class="card"><div class="card-head"><h3><span class="av sm gemini">Ge</span> Gemini CLI</h3><span class="badge ${S.agents?.gemini?.ok ? "green" : S.agents?.gemini?.signed_in === false ? "amber" : "red"}">${esc(S.agents?.gemini?.signed_in === false ? "not signed in" : S.agents?.gemini?.version || "missing")}</span></div><div class="card-body">
          <div class="field"><label>Approval mode</label><select data-cfg="gemini_approval"><option value="yolo" ${c.gemini_approval === "yolo" ? "selected" : ""}>yolo (auto-approve all tools)</option><option value="auto_edit" ${c.gemini_approval === "auto_edit" ? "selected" : ""}>auto_edit (auto-approve edits only)</option></select></div>
          <div class="field"><label>Extra CLI arguments</label><input data-cfg="gemini_extra_args" data-args="1" value="${esc((c.gemini_extra_args || []).join(" "))}"></div>
          ${catalogField("gemini")}
          <div class="field"><label>Environment variables for Gemini</label><div class="env-table" id="env-gemini">${envRows("gemini")}</div><div class="help">Google Workspace accounts need <code>GOOGLE_CLOUD_PROJECT</code>; alternatively set <code>GEMINI_API_KEY</code>.</div></div>
        </div></div>
        <div class="card"><div class="card-head"><h3>Spend estimation</h3><span class="badge outline">USD per 1M tokens</span></div><div class="card-body">
          <div class="hint" style="margin-bottom:10px">Claude Code reports real cost per turn. Codex and Gemini report tokens only, so Relay estimates an API-equivalent cost from this table (shown with a <b>~</b>). With subscription plans nothing is billed per token; the estimate helps you compare agents and budget work.</div>
          <div class="field inline"><label>Show estimated cost for agents that report tokens only</label><span class="switch ${c.show_estimated_cost !== false ? "on" : ""}" data-sw-cfg="show_estimated_cost"></span></div>
          <div class="md-table"><table style="width:100%"><thead><tr><th>Agent</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead><tbody>
            ${agentIds().map((a) => { const p = (c.pricing || {})[a] || {}; return `<tr><td><span class="row"><span class="av sm ${a}">${esc(agentInitial(a))}</span>${esc(agentLabel(a))}</span></td>${["input", "cached", "output"].map((k) => `<td><input class="input" type="number" step="0.01" min="0" style="width:110px;padding:5px 8px" data-price="${a}.${k}" value="${esc(p[k] ?? 0)}"></td>`).join("")}</tr>`; }).join("")}
          </tbody></table></div>
        </div></div>`;
      bindAuto();
      const saveEnv = debounce(() => {
        const out = {};
        for (const a of agentIds()) {
          out[a] = {};
          const ks = $$(`[data-env-k="${a}"]`, body), vs = $$(`[data-env-v="${a}"]`, body);
          ks.forEach((k, i) => { if (k.value.trim()) out[a][k.value.trim()] = vs[i].value; });
        }
        save({ agent_env: out });
      }, 500);
      $$("[data-env-k],[data-env-v]", body).forEach((i) => i.addEventListener("input", saveEnv));
      $$("[data-env-del]", body).forEach((b) => (b.onclick = () => { b.closest(".env-row").remove(); saveEnv(); }));
      const savePricing = debounce(() => {
        const pricing = JSON.parse(JSON.stringify(S.config.pricing || {}));
        $$("[data-price]", body).forEach((i) => { const [a, k] = i.dataset.price.split("."); pricing[a] = pricing[a] || {}; pricing[a][k] = Number(i.value) || 0; });
        save({ pricing });
      }, 500);
      $$("[data-price]", body).forEach((i) => i.addEventListener("input", savePricing));
      const saveModels = debounce(() => {
        const models = {};
        $$("[data-models]", body).forEach((t) => { models[t.dataset.models] = t.value.split("\n").map((x) => x.trim()).filter(Boolean); });
        save({ models });
      }, 500);
      $$("[data-models]", body).forEach((t) => t.addEventListener("input", saveModels));
      const saveDefaults = debounce(() => {
        const agent_defaults = JSON.parse(JSON.stringify(S.config.agent_defaults || {}));
        $$("[data-adef]", body).forEach((el) => {
          const [a, key] = el.dataset.adef.split(".");
          agent_defaults[a] = agent_defaults[a] || { model: "", effort: "" };
          agent_defaults[a][key] = el.value === "__custom__"
            ? (body.querySelector(`[data-adef-custom="${a}"]`)?.value || "").trim()
            : el.value;
        });
        save({ agent_defaults });
      }, 400);
      $$("[data-adef]", body).forEach((el) => el.addEventListener("change", () => {
        if (el.dataset.adef.endsWith(".model")) {
          const a = el.dataset.adef.split(".")[0];
          const ci = body.querySelector(`[data-adef-custom="${a}"]`);
          if (ci) { ci.hidden = el.value !== "__custom__"; if (!ci.hidden) ci.focus(); }
        }
        saveDefaults();
      }));
      $$("[data-adef-custom]", body).forEach((i) => i.addEventListener("input", saveDefaults));
      const saveSubsAgents = debounce(() => {
        const subagent_models = { ...(S.config.subagent_models || {}) };
        $$("[data-sub]", body).forEach((i) => { subagent_models[i.dataset.sub] = i.value.trim(); });
        save({ subagent_models });
      }, 500);
      $$("[data-sub]", body).forEach((i) => i.addEventListener("input", saveSubsAgents));
    } else if (cur === "budget") {
      const est = (c.lean_prompts !== false);
      body.innerHTML = `
        <div class="card"><div class="card-head"><h3>Cheaper models for subagents</h3></div><div class="card-body">
          <div class="hint" style="margin-bottom:10px">When the supervisor or worker spawns helper agents (Claude's Task tool, Codex multi-agent), those helpers do bulk searching and reading. Running them on a small model cuts usage sharply without touching the quality of the main reasoning.</div>
          <div class="field inline"><label>Use a cheaper model for subagents</label><span class="switch ${c.subagent_cheap_enabled !== false ? "on" : ""}" data-sw-cfg="subagent_cheap_enabled"></span></div>
          <div class="grid3">${["codex", "claude", "gemini"].map((a) => { const cat = (c.models || {})[a] || []; const v = (c.subagent_models || {})[a] || ""; return `<div class="field"><label><span class="av sm ${a}">${esc(agentInitial(a))}</span> ${esc(agentLabel(a))}</label><input list="sub-${a}" data-sub="${a}" value="${esc(v)}" placeholder="${a === "claude" ? "e.g. haiku" : "not supported"}" ${a === "claude" ? "" : "disabled"}><datalist id="sub-${a}">${cat.map((m) => `<option value="${esc(m)}">`).join("")}</datalist></div>`; }).join("")}</div>
          <div class="help">Only Claude Code supports this, through <code>CLAUDE_CODE_SUBAGENT_MODEL</code>. Codex and Gemini CLI have no setting for the model their helpers use.</div>
        </div></div>
        <div class="card"><div class="card-head"><h3>Prompt budget</h3><span class="badge ${est ? "green" : ""}">${est ? "lean" : "full"}</span></div><div class="card-body">
          <div class="hint" style="margin-bottom:10px">Each agent turn re-sends the conversation so far, so anything the orchestrator echoes back is paid for on every later turn. These caps trim the repeated parts. Lower is cheaper; too low and agents lose context they need.</div>
          <div class="field inline"><label>Lean prompts — send each role only the rules it needs, plus rules matching the task</label><span class="switch ${est ? "on" : ""}" data-sw-cfg="lean_prompts"></span></div>
          <div class="field inline"><label>On a passing check, send only the verdict (not the output)</label><span class="switch ${c.budget_verify_pass_quiet !== false ? "on" : ""}" data-sw-cfg="budget_verify_pass_quiet"></span></div>
          <div class="grid3">
            <div class="field"><label>Diff sent to reviewer (chars)</label><input type="number" min="2000" step="1000" data-cfg="budget_diff_chars" value="${esc(c.budget_diff_chars)}"></div>
            <div class="field"><label>Worker report echoed (chars)</label><input type="number" min="1000" step="500" data-cfg="budget_report_chars" value="${esc(c.budget_report_chars)}"></div>
            <div class="field"><label>Failed-check output (chars)</label><input type="number" min="200" step="200" data-cfg="budget_verify_chars" value="${esc(c.budget_verify_chars)}"></div>
            <div class="field"><label>Changed files listed</label><input type="number" min="5" step="5" data-cfg="budget_file_list" value="${esc(c.budget_file_list)}"></div>
            <div class="field"><label>Tool output stored (chars)</label><input type="number" min="1000" step="1000" data-cfg="budget_tool_output_chars" value="${esc(c.budget_tool_output_chars)}"></div>
            <div class="field"><label>Claude max tool turns per call</label><input type="number" min="10" data-cfg="claude_max_turns_per_call" value="${esc(c.claude_max_turns_per_call)}"></div>
          </div>
        </div></div>
        <div class="card"><div class="card-head"><h3>Cheapest settings that still work</h3></div><div class="card-body">
          <div class="hint stack">
            <div><b>1. Cap the loop.</b> Work packages and review rounds are the biggest multiplier: every extra round re-sends the whole session. 4–6 packages and 1–2 review rounds suit most tasks.</div>
            <div><b>2. Skip the separate reviewer</b> for small changes — the supervisor already inspects the diff. Add a reviewer only for risky work.</div>
            <div><b>3. Verify before review, not after every report</b> when checks are slow or noisy.</div>
            <div><b>4. Match effort to the role.</b> High effort on the supervisor (it decides); low or medium on the worker (it executes a written plan).</div>
            <div><b>5. Keep the task focused.</b> One clear outcome per task costs far less than a vague one the supervisor must explore.</div>
          </div>
          <div class="row wrap" style="margin-top:12px"><button type="button" class="btn sm" id="applyThrifty">${icon("zap")}Apply thrifty preset</button><button type="button" class="btn sm" id="applyThorough">${icon("shield")}Apply thorough preset</button></div>
        </div></div>`;
      bindAuto();
      const saveSubs = debounce(() => {
        const subagent_models = { ...(S.config.subagent_models || {}) };
        $$("[data-sub]", body).forEach((i) => { subagent_models[i.dataset.sub] = i.value.trim(); });
        save({ subagent_models });
      }, 500);
      $$("[data-sub]", body).forEach((i) => i.addEventListener("input", saveSubs));
      $("#applyThrifty", body).onclick = async () => {
        await save({ lean_prompts: true, max_turns: 5, max_review_rounds: 1, verify_mode: "before_review", subagent_cheap_enabled: true,
          budget_diff_chars: 16000, budget_report_chars: 4000, budget_verify_chars: 800, budget_verify_pass_quiet: true, budget_file_list: 25, budget_tool_output_chars: 6000 });
        toast("success", "Thrifty preset applied", "Fewer rounds, leaner prompts, cheap subagents.");
        render();
      };
      $("#applyThorough", body).onclick = async () => {
        await save({ lean_prompts: false, max_turns: 12, max_review_rounds: 3, verify_mode: "each_report",
          budget_diff_chars: 50000, budget_report_chars: 14000, budget_verify_chars: 3500, budget_verify_pass_quiet: false, budget_file_list: 80, budget_tool_output_chars: 12000 });
        toast("success", "Thorough preset applied", "Full rules and full evidence; higher usage.");
        render();
      };
    } else if (cur === "verification") {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Verification</h3></div><div class="card-body">
        <div class="field"><label>Default commands (one per line)</label><textarea data-cfg="verification_commands" data-list="1" rows="4" placeholder="npm test&#10;python -m pytest -q">${esc((c.verification_commands || []).join("\n"))}</textarea><div class="help">Run by the orchestrator inside the worktree; results go to the supervisor and reviewer.</div></div>
        <div class="field inline"><label>Auto-detect commands from the repository (npm scripts, pytest, gradle, cargo, go, dotnet)</label><span class="switch ${c.auto_detect_verification ? "on" : ""}" data-sw-cfg="auto_detect_verification"></span></div>
        <div class="grid2"><div class="field"><label>Default mode</label><select data-cfg="verify_mode"><option value="each_report" ${c.verify_mode === "each_report" ? "selected" : ""}>After every worker report</option><option value="before_review" ${c.verify_mode === "before_review" ? "selected" : ""}>Only before review / delivery</option><option value="off" ${c.verify_mode === "off" ? "selected" : ""}>Off</option></select></div>
        <div class="field"><label>Command timeout (minutes)</label><input type="number" min="1" data-cfg="verification_timeout_minutes" value="${esc(c.verification_timeout_minutes)}"></div></div>
      </div></div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h3>Design gate</h3><span class="badge ${c.design_gate !== false ? "green" : ""}">${c.design_gate !== false ? "enforced" : "off"}</span></div><div class="card-body">
        <div class="field inline"><label>Block delivery when agents add hard-coded colours outside token files, fonts outside the design system, gradient text or a forbidden term</label><span class="switch ${c.design_gate !== false ? "on" : ""}" data-sw-cfg="design_gate"></span></div>
        <div class="field"><label>Forbidden terms (one per line)</label><textarea data-cfg="design_forbidden_terms" data-list="1" rows="3" placeholder="a brand or client name that must never appear">${esc((c.design_forbidden_terms || []).join("\n"))}</textarea><div class="help">Checked in every added line of code, tests and docs, case-insensitive, with words joined by any separator. Stored in Relay's settings only, never written to a repository. A repository can add its own rules in <code>.relay/design-checks.json</code> (token files, fonts).</div></div>
      
      </div></div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h3>Environment</h3></div><div class="card-body">
        <div class="field inline"><label>Install dependencies before agents start (npm ci, pnpm, yarn from the repository's lockfile)</label><span class="switch ${c.env_prepare !== false ? "on" : ""}" data-sw-cfg="env_prepare"></span></div>
        <div class="grid2"><div class="field"><label>Setup timeout (minutes)</label><input type="number" min="1" data-cfg="env_prepare_timeout_minutes" value="${esc(c.env_prepare_timeout_minutes || 20)}"></div>
        <div class="field"><label>Browser VS Code URL</label><input data-cfg="ide_url" value="${esc(c.ide_url || "")}" placeholder="https://relay.example.com/code"><div class="help">code-server base URL. Enables Open in VS Code on tasks and repositories, and preview links in Try it.</div></div></div>
        <div class="help">Registry credentials come from the files mounted into Relay (for example <code>~/.npmrc</code>). A failed install is recorded as a blocked check; agents never install dependencies themselves.</div>
      </div></div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h3>Integration stacks</h3></div><div class="card-body">
        <div class="help" style="margin-top:0">Per-task containers built from the task's branches (Repositories → Environment → Integration stack). Needs the Docker socket; see docker-compose.yml.</div>
        <div class="grid2"><div class="field"><label>Start the stack</label><select data-cfg="stack_start"><option value="prepare" ${(c.stack_start || "prepare") === "prepare" ? "selected" : ""}>Before agents work (they get service URLs)</option><option value="verify" ${c.stack_start === "verify" ? "selected" : ""}>Only at verification</option></select></div>
        <div class="field"><label>Stacks running at once</label><input type="number" min="1" max="20" data-cfg="stack_max_concurrent" value="${esc(c.stack_max_concurrent || 2)}"></div>
        <div class="field"><label>Memory per container</label><input data-cfg="stack_memory_limit" value="${esc(c.stack_memory_limit || "1g")}"></div>
        <div class="field"><label>Host port range (127.0.0.1 only)</label><input data-cfg="stack_port_range" value="${esc(c.stack_port_range || "20000-29999")}"></div>
        <div class="field"><label>Build timeout (minutes)</label><input type="number" min="1" data-cfg="stack_build_timeout_minutes" value="${esc(c.stack_build_timeout_minutes || 15)}"></div></div>
        <div class="field inline"><label>Keep the stack running after the task ends (otherwise removed, logs saved to the run folder)</label><span class="switch ${c.stack_keep_after_task ? "on" : ""}" data-sw-cfg="stack_keep_after_task"></span></div>
      </div></div>`;
      bindAuto();
    } else if (cur === "git") {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Git</h3></div><div class="card-body">
        <div class="grid2"><div class="field"><label>Branch names</label><select data-cfg="branch_naming"><option value="type" ${(c.branch_naming || "type") === "type" ? "selected" : ""}>By task type: feat/berth-status-page, fix/123-login-timeout</option><option value="prefix" ${c.branch_naming === "prefix" ? "selected" : ""}>With the branch prefix: ${esc(c.branch_prefix || "agent")}/berth-status-page</option></select><div class="help">Suggested from the task name; you can edit it in the New task wizard.</div></div><div class="field"><label>Branch prefix</label><input data-cfg="branch_prefix" value="${esc(c.branch_prefix)}"></div></div>
        <div class="grid2"><div class="field"><label>Commit message prefix</label><input data-cfg="commit_message_prefix" value="${esc(c.commit_message_prefix)}"></div></div>
        <div class="field inline"><label>Carry uncommitted changes from the source checkout into the worktree</label><span class="switch ${c.snapshot_working_tree ? "on" : ""}" data-sw-cfg="snapshot_working_tree"></span></div>
        <div class="field inline"><label>Copy untracked files too (up to ${esc(c.max_untracked_copy_mb)} MB)</label><span class="switch ${c.copy_untracked_files ? "on" : ""}" data-sw-cfg="copy_untracked_files"></span></div>
        <div class="field inline"><label>Keep worktrees after delivery</label><span class="switch ${c.keep_worktrees ? "on" : ""}" data-sw-cfg="keep_worktrees"></span></div>
      </div></div>
      <div class="card"><div class="card-head"><h3>GitHub delivery</h3></div><div class="card-body">
        <div class="field inline"><label>Push the task branch when delivered</label><span class="switch ${c.github_auto_push_on_pass ? "on" : ""}" data-sw-cfg="github_auto_push_on_pass"></span></div>
        <div class="field inline"><label>Open a pull request</label><span class="switch ${c.github_auto_create_pr ? "on" : ""}" data-sw-cfg="github_auto_create_pr"></span></div>
        <div class="field inline"><label>Create PRs as drafts</label><span class="switch ${c.github_pr_draft ? "on" : ""}" data-sw-cfg="github_pr_draft"></span></div>
        <div class="grid2"><div class="field"><label>Base branch (blank = repository default)</label><input data-cfg="github_pr_base" value="${esc(c.github_pr_base)}"></div><div class="field"><label>Default agent label</label><input data-cfg="github_default_label" value="${esc(c.github_default_label)}"></div></div>
        <div class="field"><label>PR body template</label><textarea data-cfg="github_pr_body_template" rows="5">${esc(c.github_pr_body_template)}</textarea><div class="help">Placeholders: <code>{summary}</code> <code>{details}</code> <code>{issue_close}</code></div></div>
      </div></div>
      <div class="card"><div class="card-head"><div><h3>Redeploy after merge</h3><p class="card-sub">For delivered tasks whose workflow opted in. Nothing runs while this is off.</p></div><span class="badge outline">owner configured</span></div><div class="card-body">
        <div class="field inline"><label>Run the redeploy command for opted-in tasks</label><span class="switch ${c.redeploy_enabled ? "on" : ""}" data-sw-cfg="redeploy_enabled"></span></div>
        <div class="field"><label>Redeploy command</label><input data-cfg="redeploy_command" value="${esc(c.redeploy_command || "")}" placeholder="./deploy.sh"><div class="help">Owner configuration only: agents, task text and API callers cannot set or change it.</div></div>
        <div class="field"><label>Working folder</label><input data-cfg="redeploy_working_dir" value="${esc(c.redeploy_working_dir || "")}" placeholder="Blank = the delivered task worktree"></div>
        <div class="grid3">
          <div class="field"><label>Trigger</label><select data-cfg="redeploy_trigger"><option value="pr_merged" ${c.redeploy_trigger !== "delivered" ? "selected" : ""}>When the pull request is merged</option><option value="delivered" ${c.redeploy_trigger === "delivered" ? "selected" : ""}>As soon as the task is delivered</option></select></div>
          <div class="field"><label>Timeout (minutes)</label><input type="number" min="1" data-cfg="redeploy_timeout_minutes" value="${esc(c.redeploy_timeout_minutes)}"></div>
          <div class="field"><label>Poll interval (seconds)</label><input type="number" min="15" data-cfg="redeploy_poll_seconds" value="${esc(c.redeploy_poll_seconds)}"></div>
        </div>
        <div class="help">Each task opts in separately. <a href="#/settings/workflow">Settings → Workflow</a> sets what new tasks start with (currently <strong>${c.redeploy_default_on ? "on" : "off"}</strong>); this switch stays in charge of whether anything runs at all.</div>
      </div></div>
      <div class="card"><div class="card-head"><h3>GitHub intake</h3></div><div class="card-body">
        <div class="field inline"><label>Watch repositories for eligible issues</label><span class="switch ${c.github_intake_enabled ? "on" : ""}" data-sw-cfg="github_intake_enabled"></span></div>
        <div class="field"><label>Poll interval (seconds)</label><input type="number" min="15" data-cfg="github_poll_seconds" value="${esc(c.github_poll_seconds)}" style="width:120px"></div>
        <a class="btn sm" href="#/knowledge/github">${icon("github")}Manage watched repositories</a>
        <div class="field" style="margin-bottom:0"><label>Repository for "Report an issue"</label><input data-cfg="github_issue_repo" value="${esc(c.github_issue_repo || "")}" placeholder="metratek-telematics/relay"><div class="help">Where the Report an issue dialog files bug reports and feature requests. Point a fork at its own repository.</div></div>
      </div></div>
      <div class="card"><div class="card-head"><h3>Issues board</h3></div><div class="card-body">
        <div class="field inline"><label>Comment “Relay picked this up” on an issue when a task is created from it</label><span class="switch ${c.issues_comment_on_pickup ? "on" : ""}" data-sw-cfg="issues_comment_on_pickup"></span></div>
        <div class="grid2"><div class="field"><label>Label to add to picked-up issues (blank = none)</label><input data-cfg="issues_pickup_label" value="${esc(c.issues_pickup_label || "")}" placeholder="in-progress"></div></div>
        <div class="field inline"><label>Stop a one-after-the-other chain when one of its tasks fails</label><span class="switch ${c.queue_stop_chain_on_failure ? "on" : ""}" data-sw-cfg="queue_stop_chain_on_failure"></span></div>
        <div class="help">Used when you queue GitHub issues from the <a href="#/work/issues">Work board</a>.</div>
      </div></div>`;
      bindAuto();
    } else if (cur === "appearance") {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Appearance</h3></div><div class="card-body">
        <div class="grid2"><div class="field"><label>Theme</label><select data-cfg="ui_theme">${["system", "light", "dark"].map((x) => `<option ${c.ui_theme === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
        <div class="field"><label>Density</label><select data-cfg="ui_density">${["comfortable", "compact"].map((x) => `<option ${c.ui_density === x ? "selected" : ""}>${x}</option>`).join("")}</select></div></div>
      </div></div>
      <div class="card"><div class="card-head"><h3>Keyboard</h3><button type="button" class="btn sm" id="showKeys">${icon("keyboard")}All shortcuts <kbd>?</kbd></button></div><div class="card-body hint">
        <kbd>${esc(keysFor("palette"))}</kbd> command palette · <kbd>N</kbd> new task · <kbd>/</kbd> search · <kbd>J</kbd> <kbd>K</kbd> next and previous task · <kbd>G</kbd> then <kbd>H</kbd> <kbd>W</kbd> <kbd>K</kbd> <kbd>A</kbd> <kbd>S</kbd> go to Mission Control, Work, Knowledge, Agents, Settings · <kbd>T</kbd> Try it · <kbd>.</kbd> guidance · <kbd>Esc</kbd> close.
      </div></div>`;
      bindAuto();
      $$("[data-cfg='ui_theme'],[data-cfg='ui_density']", body).forEach((s) => s.addEventListener("change", () => bus.emit("theme", { theme: $("[data-cfg='ui_theme']", body).value, density: $("[data-cfg='ui_density']", body).value })));
      $("#showKeys", body).onclick = () => openShortcuts();
    } else if (cur === "workspace") {
      import("./org/settings-entry.js").then((m) => m.renderWorkspaceSettings(body));
    } else if (cur === "notifications") {
      renderNotifications(c);
    } else if (cur === "prompts") {
      renderPrompts(c);
    } else if (cur === "connectors") {
      mountConnectors(body);
    } else if (cur === "providers") {
      mountProviders(body);
    } else if (cur === "tools") {
      mountTools(body);
    } else if (cur === "tokens") {
      mountTokens(body);
    } else if (cur === "rules") {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Engineering rules</h3><span class="muted" style="font-size:12px">Injected into agent prompts by role</span></div><div class="card-body"><div class="row wrap" id="ruleList"></div><div id="ruleEd" style="margin-top:12px"></div></div></div>`;
      api.rules().then((rows) => {
        $("#ruleList", body).innerHTML = rows.map((r) => `<button type="button" class="chip" data-rule="${esc(r.name)}">${esc(r.name)}</button>`).join("");
        $$("[data-rule]", body).forEach((b) => (b.onclick = async () => {
          $$("[data-rule]", body).forEach((x) => x.classList.toggle("active", x === b));
          const r = await api.rule(b.dataset.rule);
          $("#ruleEd", body).innerHTML = `<div class="field"><label>${esc(r.name)}.md</label><textarea id="ruleText" rows="22" style="font-family:var(--mono);font-size:12px">${esc(r.text)}</textarea></div><div class="modal-actions" style="margin:0"><button type="button" class="btn primary" id="ruleSave">${icon("save")}Save rule</button></div>`;
          $("#ruleSave", body).onclick = async () => { try { await api.saveRule(r.name, $("#ruleText", body).value); toast("success", "Rule saved", r.name); } catch (e) { toast("error", "Save failed", e.message); } };
        }));
      });
    } else {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>About</h3></div><div class="card-body hint stack">
        <div><strong>Relay ${esc(S.build)}</strong> — a local, Windows-first multi-agent engineering orchestrator. Codex, Claude Code and Gemini CLI collaborate through persistent sessions inside isolated git worktrees.</div>
        <div>Data lives next to the app: <code>state/tasks.json</code> (task metadata), <code>runtime/&lt;task&gt;/</code> (conversation, artifacts, raw logs), <code>worktrees/</code> (isolated checkouts), <code>config.json</code>.</div>
        <div>Safety boundary: the orchestrator commits, pushes task branches and opens draft PRs. It never force-pushes or deploys, and it merges only when you approve a change set in the review cockpit.</div>
        <div class="row wrap"><a class="btn sm" href="/api/state" target="_blank">${icon("code")}Raw state JSON</a><button type="button" class="btn sm" id="clearFinished">${icon("trash")}Archive all finished tasks</button></div>
      </div></div>`;
      $("#clearFinished", body).onclick = async () => {
        const fin = [...S.tasks.values()].filter((t) => ["done", "failed", "stopped"].includes(t.status) && !t.archived);
        if (!fin.length) { toast("info", "Nothing to archive"); return; }
        if (!(await confirm("Archive finished tasks?", `${fin.length} task(s) are moved to the Archived filter.`))) return;
        for (const t of fin) await api.action(t.id, "archive", { archived: true });
        toast("success", "Archived", `${fin.length} task(s)`);
      };
    }
  }
  function renderNotifications(c) {
    const perm = permission();
    const events = c.ui_notify_events || {};
    const [tone, label, text] = {
      granted: ["green", "Allowed", "This browser lets Relay show desktop notifications."],
      denied: ["red", "Blocked", "This browser blocks notifications from Relay. Allow them in the site settings (the icon to the left of the address) and reload the page."],
      default: ["amber", "Not allowed yet", "Your browser asks once, when you click Allow notifications. Until then you only see alerts inside Relay."],
      unsupported: ["", "Not supported", "This browser has no desktop notifications. Alerts inside Relay still appear."],
    }[perm] || ["", perm, ""];
    body.innerHTML = `<div class="card"><div class="card-head"><h3>Desktop notifications</h3><span class="badge ${tone}">${esc(label)}</span></div><div class="card-body">
        <div class="notif-perm"><div class="notif-perm-copy"><strong>Browser permission</strong><span class="hint">${esc(text)}</span></div>${perm === "default" ? `<button type="button" class="btn sm primary" id="nAllow">${icon("bell")}Allow notifications</button>` : ""}</div>
        <div class="field inline"><label>Notify me when Relay is in the background<span class="help">Inside the Relay window you always get in-app alerts instead.</span></label><span class="switch ${c.ui_notifications ? "on" : ""}" data-sw-cfg="ui_notifications"></span></div>
        <div class="notif-events ${c.ui_notifications ? "" : "is-off"}" id="nEvents">
          ${NOTIFY_EVENTS.map(([k, l, d]) => `<div class="field inline"><label>${esc(l)}<span class="help">${esc(d)}</span></label><span class="switch ${events[k] !== false ? "on" : ""}" data-ev="${k}" role="switch" aria-label="${esc(l)}"></span></div>`).join("")}
          <div class="help">Anything else, such as an issue picked up from GitHub, follows the switch above.</div>
        </div>
      </div></div>
      <div class="card"><div class="card-head"><h3>Sound</h3></div><div class="card-body">
        <div class="field inline"><label>Play a short chime with each desktop alert<span class="help">It rises for deliveries and questions and falls for failures.</span></label><span class="switch ${c.ui_sound ? "on" : ""}" data-sw-cfg="ui_sound"></span></div>
        <div class="row wrap"><button type="button" class="btn sm" id="nChime">${icon("play")}Play chime</button><button type="button" class="btn sm" id="nChimeFail">${icon("play")}Play failure chime</button></div>
      </div></div>
      <div class="card"><div class="card-head"><h3>Check it works</h3></div><div class="card-body stack">
        <div class="hint">Sends a sample notification right away, even with Relay in front. If nothing appears, check your system's Do Not Disturb or Focus settings.</div>
        <div class="row wrap"><button type="button" class="btn sm" id="nTest">${icon("send")}Send test notification</button><span class="help" id="nTestResult" role="status"></span></div>
        <div class="hint">While something needs you, the browser tab shows a count, such as <b>(2) Relay</b>, and its icon gets a red dot. It counts questions, approvals, paused or interrupted tasks, and failures you have not opened yet.</div>
      </div></div>`;
    bindAuto();
    $("[data-sw-cfg='ui_notifications']", body).addEventListener("click", (e) => $("#nEvents", body).classList.toggle("is-off", !e.currentTarget.classList.contains("on")));
    $$("[data-ev]", body).forEach((sw) => (sw.onclick = () => {
      sw.classList.toggle("on");
      save({ ui_notify_events: { ...(S.config.ui_notify_events || {}), [sw.dataset.ev]: sw.classList.contains("on") } });
    }));
    $("#nAllow", body) && ($("#nAllow", body).onclick = async () => { await requestPermission(); renderNotifications(S.config); });
    $("#nChime", body).onclick = () => chime("success");
    $("#nChimeFail", body).onclick = () => chime("error");
    $("#nTest", body).onclick = async () => {
      const out = $("#nTestResult", body);
      let perm = permission();
      if (perm === "default") { perm = await requestPermission(); renderNotifications(S.config); }
      const sample = { id: `test-${Date.now()}`, level: "success", kind: "delivered", title: "Test notification", body: "This is how Relay lets you know a task needs you." };
      if (S.config.ui_sound) chime("success");
      const sent = perm === "granted" && showDesktop(sample);
      const target = $("#nTestResult", body) || out;
      target.textContent = sent ? "Sent." : perm === "denied" ? "The browser blocked it. Allow notifications for this site first." : perm === "unsupported" ? "This browser cannot show desktop notifications." : "Not sent: notifications are not allowed yet.";
      target.style.color = sent ? "var(--green)" : "var(--red)";
    };
  }

  function renderPrompts(c) {
    const rows = JSON.parse(JSON.stringify(c.saved_prompts || []));
    const tpls = S.templates || [];
    const persist = () => save({ saved_prompts: rows.filter((p) => p.name.trim() || p.text.trim()) });
    const persistSoon = debounce(persist, 600);
    const draw = () => {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Saved prompts</h3><button type="button" class="btn sm primary" id="pAdd">${icon("plus")}Add prompt</button></div><div class="card-body">
        <div class="hint" style="margin-bottom:12px">Requests you make often. Pick one in step 2 of the New task wizard to fill in the requirements, then edit the placeholders in angle brackets. Changes save as you type.</div>
        <div class="stack" style="gap:12px">${rows.length ? rows.map((p, i) => `<div class="prompt-edit" data-i="${i}">
          <div class="prompt-edit-head">
            <div class="field"><label for="pName${i}">Name</label><input id="pName${i}" data-p="name" value="${esc(p.name)}" placeholder="e.g. Add an empty state to a list"></div>
            <div class="field"><label for="pTpl${i}">Task type</label><select id="pTpl${i}" data-p="template"><option value="">Any type</option>${tpls.map((x) => `<option value="${esc(x.id)}" ${p.template === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></div>
            <button type="button" class="btn sm danger" data-pdel="${i}" title="Delete this prompt">${icon("trash")}<span>Delete</span></button>
          </div>
          <div class="field" style="margin-bottom:0"><label for="pText${i}">Request</label><textarea id="pText${i}" data-p="text" rows="5" placeholder="Describe the change the way you would in the wizard.">${esc(p.text)}</textarea></div>
        </div>`).join("") : `<div class="empty small">${icon("message", "lg")}<p>No saved prompts yet. Add one for a request you make often.</p></div>`}</div>
      </div></div>`;
      $("#pAdd", body).onclick = () => { rows.unshift({ id: `p_${Date.now().toString(36)}`, name: "", text: "", template: "" }); draw(); $("#pName0", body).focus(); };
      $$(".prompt-edit", body).forEach((box) => {
        const p = rows[Number(box.dataset.i)];
        $$("[data-p]", box).forEach((f) => f.addEventListener(f.tagName === "SELECT" ? "change" : "input", () => { p[f.dataset.p] = f.value; persistSoon(); }));
      });
      $$("[data-pdel]", body).forEach((b) => (b.onclick = async () => {
        const i = Number(b.dataset.pdel);
        if ((rows[i].name || rows[i].text) && !(await confirm("Delete this prompt?", rows[i].name || "Untitled prompt", { danger: true, okLabel: "Delete" }))) return;
        rows.splice(i, 1); persist(); draw();
      }));
    };
    draw();
  }

  render();
  return { update(reason) { if (reason === "route") { const s = S.route.section; if (s && s !== cur && SECTIONS.some(([k]) => k === s)) { cur = s; render(); } } }, destroy() {} };
}

// ---------------------------------------------------------------------------- autopilot (orchestrator/autopilot.py)
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function autopilotSettings(c) {
  const a = c.autopilot || {};
  const q = a.quiet_hours || {};
  const fb = a.fallbacks || {};
  const win = a.windows || [];
  const sw = (key, on) => `<span class="switch ${on ? "on" : ""}" data-ap-sw="${key}" role="switch" aria-checked="${!!on}" tabindex="0"></span>`;
  const agentsList = agentIds().map((x) => `<option value="${esc(x)}">`).join("");
  return `<div class="card"><div class="card-head"><div><h3>Running unattended</h3><p class="card-sub">Queue a stack of tasks and walk away. The status bar at the top shows what the autopilot is doing.</p></div></div><div class="card-body">
      <div class="field inline"><label>Pause everything (nothing starts; running tasks stop after their current turn)</label>${sw("paused", a.paused)}</div>
      <div class="field inline"><label>A task waiting for your answer frees its parallel slot, so independent tasks keep running</label>${sw("park_waiting_tasks", a.park_waiting_tasks !== false)}</div>
      <div class="grid3">
        <div class="field"><label>Parallel tasks</label><input type="number" min="1" max="8" data-cfg="max_parallel" value="${esc(c.max_parallel)}"></div>
        <div class="field"><label>Automatic retries after an agent crash, hang or rate limit</label><input type="number" min="0" max="5" data-ap="retry_infra_failures" value="${esc(a.retry_infra_failures ?? 1)}"><div class="help">Resumes from the checkpoint. Judge outcomes (budget, review, verification) are never retried.</div></div>
        <div class="field"><label>Time zone</label><input data-ap="timezone" value="${esc(a.timezone || "")}" placeholder="server time, e.g. Europe/Athens"><div class="help">For run windows, quiet hours and the digest.</div></div>
      </div>
    </div></div>
    <div class="card"><div class="card-head"><div><h3>Run windows</h3><p class="card-sub">When new tasks may start. A running task always finishes its work.</p></div>
      <select data-ap="schedule" aria-label="Schedule"><option value="always" ${a.schedule !== "windows" ? "selected" : ""}>Always</option><option value="windows" ${a.schedule === "windows" ? "selected" : ""}>Only in these windows</option></select></div>
      <div class="card-body ${a.schedule === "windows" ? "" : "ap-dim"}">
        <div class="ap-windows">${win.map((w, i) => `<div class="ap-window" data-win="${i}">
          <div class="ap-days">${DAYS.map((d, k) => `<label class="ap-day"><input type="checkbox" data-win-day="${k}" ${(w.days || []).includes(k) ? "checked" : ""}><span>${d}</span></label>`).join("")}</div>
          <div class="ap-times"><input type="time" data-win-start value="${esc(w.start || "00:00")}" aria-label="Start"><span>to</span><input type="time" data-win-end value="${esc(w.end || "23:59")}" aria-label="End"><button type="button" class="btn xs ghost" data-win-del="${i}" title="Remove window">${icon("trash")}</button></div>
        </div>`).join("") || '<div class="empty small">No windows: nothing starts while the schedule is set to windows.</div>'}</div>
        <button type="button" class="btn sm" id="apWinAdd">${icon("plus")}Add window</button>
        <div class="help">An end earlier than the start runs past midnight (20:00 to 07:00).</div>
      </div></div>
    <div class="card"><div class="card-head"><div><h3>Quiet hours</h3><p class="card-sub">Nobody answers at night: judge decisions take their safe automatic choice instead of waiting. Agent questions still wait for you.</p></div>${sw("quiet_hours.enabled", q.enabled)}</div>
      <div class="card-body"><div class="grid3"><div class="field"><label>From</label><input type="time" data-ap="quiet_hours.start" value="${esc(q.start || "22:00")}"></div><div class="field"><label>Until</label><input type="time" data-ap="quiet_hours.end" value="${esc(q.end || "08:00")}"></div></div></div></div>
    <div class="card"><div class="card-head"><div><h3>Plan limits and fallbacks</h3><p class="card-sub">Before a task starts Relay reads each agent's sign-in, 5-hour and weekly limits and balance (Agents page). A blocked role switches to its fallback chain, or the task waits for the reset.</p></div>${sw("limit_check", a.limit_check !== false)}</div>
      <div class="card-body">
        <div class="grid3">
          <div class="field"><label>Treat a limit as reached at (%)</label><input type="number" min="50" max="100" data-ap="limit_threshold_percent" value="${esc(a.limit_threshold_percent ?? 90)}"></div>
          <div class="field"><label>When a role's agent is blocked</label><select data-ap="limit_action"><option value="fallback" ${a.limit_action !== "wait" ? "selected" : ""}>Use its fallback chain, else wait</option><option value="wait" ${a.limit_action === "wait" ? "selected" : ""}>Wait for the reset</option></select></div>
        </div>
        <datalist id="apAgents">${agentsList}</datalist>
        ${["supervisor", "worker", "reviewer"].map((r) => `<div class="field"><label>${r[0].toUpperCase() + r.slice(1)} fallback chain</label><input data-ap-fb="${r}" list="apAgents" value="${esc((fb[r] || []).join(", "))}" placeholder="e.g. codex, kilo:kilo/some-model:free"><div class="help">Agents tried in order, comma separated; agent:model picks a model.</div></div>`).join("")}
      </div></div>
    <div class="card"><div class="card-head"><div><h3>Cost caps</h3><p class="card-sub">Estimated from token prices (Usage &amp; budget). 0 means no cap.</p></div></div><div class="card-body"><div class="grid3">
      <div class="field"><label>Per task (USD)</label><input type="number" min="0" step="0.5" data-ap="task_cost_cap_usd" value="${esc(a.task_cost_cap_usd ?? 0)}"><div class="help">The task pauses after the turn that crosses it and appears under Needs you.</div></div>
      <div class="field"><label>Per day (USD)</label><input type="number" min="0" step="1" data-ap="daily_cost_cap_usd" value="${esc(a.daily_cost_cap_usd ?? 0)}"><div class="help">No new task starts until tomorrow once reached.</div></div>
    </div></div></div>
    <div class="card"><div class="card-head"><div><h3>Watchdog and digest</h3></div></div><div class="card-body">
      <div class="field inline"><label>Watchdog: resume tasks whose runner died or whose agent process vanished</label>${sw("watchdog", a.watchdog !== false)}</div>
      <div class="grid3">
        <div class="field"><label>Agent process gone for (minutes)</label><input type="number" min="2" data-ap="watchdog_silent_minutes" value="${esc(a.watchdog_silent_minutes ?? 20)}"></div>
        <div class="field"><label>Morning digest at</label><input type="time" data-ap="digest_time" value="${esc(a.digest_time || "")}"><div class="help">A notification once a day; clear it to turn it off.</div></div>
        <div class="field"><label>Digest covers (hours)</label><input type="number" min="1" max="168" data-ap="digest_hours" value="${esc(a.digest_hours ?? 24)}"></div>
      </div>
    </div></div>`;
}

// Design exploration (orchestrator/exploration.py): mockups and an internal focus group before UI work is built.
function explorationCard(c) {
  const ask = c.design_ask_when_close === true ? "on" : c.design_ask_when_close === false ? "off" : String(c.design_ask_when_close || "auto");
  return `<div class="card"><div class="card-head"><h3>Design exploration</h3><span class="badge ${c.design_exploration === "off" ? "" : "green"}">${c.design_exploration === "off" ? "off" : "auto"}</span></div><div class="card-body">
    <p class="help" style="margin-top:0">For design and layout work, the designer draws 2 or 3 different directions as mockups, Relay renders them (desktop and phone, light and dark), and a panel of personas scores them. Relay builds the winner without asking you; the choice appears on the task's Mockups tab with one click to switch.</p>
    <div class="grid3">
      <div class="field"><label for="cfgDx">Explore directions</label><select id="cfgDx" data-cfg="design_exploration">${[["auto", "Auto: design and layout tasks"], ["off", "Off"]].map(([v, l]) => `<option value="${v}" ${(c.design_exploration || "auto") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="field"><label for="cfgDn">Directions</label><select id="cfgDn" data-cfg="design_directions" data-num="1">${[2, 3].map((n) => `<option value="${n}" ${Number(c.design_directions || 3) === n ? "selected" : ""}>${n}</option>`).join("")}</select></div>
      <div class="field"><label for="cfgDm">Focus group cost</label><select id="cfgDm" data-cfg="design_focus_group_mode">${[["full", "Full: one reviewer turn per persona"], ["lean", "Lean: one turn for the whole panel"]].map(([v, l]) => `<option value="${v}" ${(c.design_focus_group_mode || "full") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
    </div>
    <div class="grid3">
      <div class="field"><label for="cfgDa">Ask me on a close call</label><select id="cfgDa" data-cfg="design_ask_when_close">${[["auto", "Auto: only when agents may ask freely"], ["on", "Yes, when the top two are close and different"], ["off", "Never: the panel decides"]].map(([v, l]) => `<option value="${v}" ${ask === v ? "selected" : ""}>${l}</option>`).join("")}</select><div class="help">With the default question policy (ask only when blocked), Relay decides and tells you.</div></div>
      <div class="field"><label for="cfgDc">Close-call margin (points of 10)</label><input id="cfgDc" type="number" min="0" max="5" step="0.1" data-cfg="design_close_margin" value="${esc(c.design_close_margin ?? 0.5)}"></div>
      <div class="field"><label for="cfgDt">Answer timeout (minutes)</label><input id="cfgDt" type="number" min="0" max="10080" data-cfg="design_pick_timeout_minutes" value="${esc(c.design_pick_timeout_minutes ?? 240)}"><div class="help">After this, Relay builds the panel's pick. 0 waits for you.</div></div>
    </div>
    <div class="field"><label for="cfgDp">Personas (one per line, "Name: what they judge")</label><textarea id="cfgDp" data-cfg="design_personas" data-list="1" rows="4">${esc((c.design_personas || []).join("\n"))}</textarea><div class="help"><code>{product}</code> is replaced with the repository's description. Up to 6 personas; each scores clarity, efficiency, accessibility, design-system fit, research fit and feasibility from 1 to 10.</div></div>
  </div></div>`;
}

function bindAutopilot(body, c, save, render) {
  const cur = () => JSON.parse(JSON.stringify((S.config || c).autopilot || {}));
  const put = async (a, redraw) => { await save({ autopilot: a }); if (redraw) render(); };
  const setPath = (obj, path, v) => { const ks = path.split("."); let o = obj; ks.slice(0, -1).forEach((k) => { o[k] = { ...(o[k] || {}) }; o = o[k]; }); o[ks[ks.length - 1]] = v; };
  $$("[data-ap]", body).forEach((i) => i.addEventListener("change", () => {
    const a = cur();
    const v = i.type === "number" ? Number(i.value) || 0 : i.value;
    setPath(a, i.dataset.ap, v);
    put(a, i.dataset.ap === "schedule");
  }));
  $$("[data-ap-sw]", body).forEach((s) => {
    const flip = () => { s.classList.toggle("on"); const a = cur(); setPath(a, s.dataset.apSw, s.classList.contains("on")); put(a); };
    s.onclick = flip;
    s.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } };
  });
  $$("[data-ap-fb]", body).forEach((i) => i.addEventListener("change", () => {
    const a = cur();
    a.fallbacks = { ...(a.fallbacks || {}), [i.dataset.apFb]: i.value.split(",").map((x) => x.trim()).filter(Boolean) };
    put(a);
  }));
  const readWindows = () => $$("[data-win]", body).map((w) => ({
    days: $$("[data-win-day]", w).filter((x) => x.checked).map((x) => Number(x.dataset.winDay)),
    start: $("[data-win-start]", w).value || "00:00", end: $("[data-win-end]", w).value || "23:59",
  }));
  $$("[data-win-day], [data-win-start], [data-win-end]", body).forEach((i) => i.addEventListener("change", () => { const a = cur(); a.windows = readWindows(); put(a); }));
  $$("[data-win-del]", body).forEach((b) => (b.onclick = () => { const a = cur(); a.windows = readWindows().filter((_, k) => k !== Number(b.dataset.winDel)); put(a, true); }));
  $("#apWinAdd", body).onclick = () => { const a = cur(); a.windows = [...readWindows(), { days: [0, 1, 2, 3, 4], start: "20:00", end: "07:00" }]; put(a, true); };
}
