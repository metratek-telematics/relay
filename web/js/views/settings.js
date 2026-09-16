// Settings: workflow defaults, agents, verification, git/github, appearance, rules, about.
import { $, $$, esc, icon, toast, confirm, debounce } from "../ui.js";
import { S, agentLabel, agentInitial, bus } from "../state.js";
import { api } from "../api.js";
import { workflowEditor } from "./newtask.js";

const SECTIONS = [["workflow", "Workflow", "layers"], ["agents", "Agents", "bot"], ["budget", "Usage & budget", "gauge"], ["verification", "Verification", "shield"], ["git", "Git & GitHub", "github"], ["appearance", "Appearance", "sun"], ["rules", "Rules", "docs"], ["about", "About", "info"]];

export function mountSettings(main, section) {
  let cur = SECTIONS.some(([k]) => k === section) ? section : "workflow";
  main.innerHTML = `<div class="page"><div class="page-head"><div><h1>Settings</h1><p>Defaults for new tasks. Existing tasks keep their own workflow.</p></div><div class="page-actions"><span class="muted" id="saveState" style="font-size:12px"></span></div></div>
    <div class="settings"><nav class="settings-nav" id="sNav"></nav><div class="settings-main" id="sMain"></div></div></div>`;
  const nav = $("#sNav", main), body = $("#sMain", main);
  const saved = (ok = true, msg) => { const s = $("#saveState", main); s.textContent = msg || (ok ? "Saved" : "Save failed"); s.style.color = ok ? "var(--green)" : "var(--red)"; setTimeout(() => { if (s.textContent === "Saved") s.textContent = ""; }, 2000); };
  const save = async (partial) => { try { S.config = await api.saveSettings(partial); saved(true); bus.emit("config"); } catch (e) { saved(false); toast("error", "Could not save", e.message); } };
  const bindAuto = () => {
    $$("[data-cfg]", body).forEach((i) => {
      const k = i.dataset.cfg;
      const handler = () => {
        let v = i.type === "checkbox" ? i.checked : i.type === "number" ? Number(i.value) : i.value;
        if (i.dataset.list) v = v.split("\n").map((x) => x.trim()).filter(Boolean);
        if (i.dataset.args) v = v.trim() ? v.trim().split(/\s+/) : [];
        save({ [k]: v });
      };
      i.addEventListener(i.tagName === "TEXTAREA" || i.type === "text" ? "change" : "change", handler);
    });
    $$("[data-sw-cfg]", body).forEach((s) => (s.onclick = () => { s.classList.toggle("on"); save({ [s.dataset.swCfg]: s.classList.contains("on") }); }));
  };

  function drawNav() { nav.innerHTML = SECTIONS.map(([k, l, i]) => `<a href="#/settings/${k}" class="${k === cur ? "active" : ""}">${icon(i)}${l}</a>`).join(""); }

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
          </div>
          <div class="field"><label>Parallel tasks</label><input type="number" min="1" max="8" data-cfg="max_parallel" value="${esc(c.max_parallel)}" style="width:100px"></div>
        </div></div>`;
      const wf = { preset: c.workflow_preset, roles: JSON.parse(JSON.stringify(c.roles || {})), max_turns: c.max_turns, max_review_rounds: c.max_review_rounds, verify_mode: c.verify_mode, approval_before_delivery: c.approval_before_delivery, allow_agent_questions: c.allow_agent_questions, verification_commands: c.verification_commands || [], auto_detect_verification: c.auto_detect_verification };
      const persist = debounce((w) => save({ workflow_preset: w.preset, roles: JSON.parse(JSON.stringify(w.roles)), max_turns: w.max_turns, max_review_rounds: w.max_review_rounds, verify_mode: w.verify_mode, approval_before_delivery: w.approval_before_delivery, allow_agent_questions: w.allow_agent_questions, verification_commands: w.verification_commands, auto_detect_verification: w.auto_detect_verification }), 400);
      workflowEditor($("#wfEd", body), wf, { agents: S.agentMeta, presets: S.presets, onChange: persist });
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
            ${a === "gemini"
              ? `<input disabled placeholder="not supported"><div class="help">Gemini has no subagent model setting.</div>`
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
        <div class="card"><div class="card-head"><h3><span class="av sm gemini">Ge</span> Gemini CLI</h3><span class="badge ${S.agents?.gemini?.ok ? "green" : "red"}">${esc(S.agents?.gemini?.version || "missing")}</span></div><div class="card-body">
          <div class="field"><label>Approval mode</label><select data-cfg="gemini_approval"><option value="yolo" ${c.gemini_approval === "yolo" ? "selected" : ""}>yolo (auto-approve all tools)</option><option value="auto_edit" ${c.gemini_approval === "auto_edit" ? "selected" : ""}>auto_edit (auto-approve edits only)</option></select></div>
          <div class="field"><label>Extra CLI arguments</label><input data-cfg="gemini_extra_args" data-args="1" value="${esc((c.gemini_extra_args || []).join(" "))}"></div>
          ${catalogField("gemini")}
          <div class="field"><label>Environment variables for Gemini</label><div class="env-table" id="env-gemini">${envRows("gemini")}</div><div class="help">Google Workspace accounts need <code>GOOGLE_CLOUD_PROJECT</code>; alternatively set <code>GEMINI_API_KEY</code>.</div></div>
        </div></div>
        <div class="card"><div class="card-head"><h3>Spend estimation</h3><span class="badge outline">USD per 1M tokens</span></div><div class="card-body">
          <div class="hint" style="margin-bottom:10px">Claude Code reports real cost per turn. Codex and Gemini report tokens only, so Relay estimates an API-equivalent cost from this table (shown with a <b>~</b>). With subscription plans nothing is billed per token; the estimate helps you compare agents and budget work.</div>
          <div class="field inline"><label>Show estimated cost for agents that report tokens only</label><span class="switch ${c.show_estimated_cost !== false ? "on" : ""}" data-sw-cfg="show_estimated_cost"></span></div>
          <div class="md-table"><table style="width:100%"><thead><tr><th>Agent</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead><tbody>
            ${["codex", "claude", "gemini"].map((a) => { const p = (c.pricing || {})[a] || {}; return `<tr><td><span class="row"><span class="av sm ${a}">${esc(agentInitial(a))}</span>${esc(agentLabel(a))}</span></td>${["input", "cached", "output"].map((k) => `<td><input class="input" type="number" step="0.01" min="0" style="width:110px;padding:5px 8px" data-price="${a}.${k}" value="${esc(p[k] ?? 0)}"></td>`).join("")}</tr>`; }).join("")}
          </tbody></table></div>
        </div></div>`;
      bindAuto();
      const saveEnv = debounce(() => {
        const out = {};
        for (const a of ["codex", "claude", "gemini"]) {
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
          <div class="grid3">${["codex", "claude", "gemini"].map((a) => { const cat = (c.models || {})[a] || []; const v = (c.subagent_models || {})[a] || ""; return `<div class="field"><label><span class="av sm ${a}">${esc(agentInitial(a))}</span> ${esc(agentLabel(a))}</label><input list="sub-${a}" data-sub="${a}" value="${esc(v)}" placeholder="${a === "gemini" ? "not supported" : "e.g. " + (a === "codex" ? "gpt-5.1-codex-mini" : "haiku")}" ${a === "gemini" ? "disabled" : ""}><datalist id="sub-${a}">${cat.map((m) => `<option value="${esc(m)}">`).join("")}</datalist></div>`; }).join("")}</div>
          <div class="help">Claude: exported as <code>CLAUDE_CODE_SUBAGENT_MODEL</code>. Codex: passed as <code>agents.&lt;role&gt;.model</code> overrides. Gemini's CLI has no subagent model setting.</div>
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
      </div></div>`;
      bindAuto();
    } else if (cur === "git") {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Git</h3></div><div class="card-body">
        <div class="grid2"><div class="field"><label>Branch prefix</label><input data-cfg="branch_prefix" value="${esc(c.branch_prefix)}"></div><div class="field"><label>Commit message prefix</label><input data-cfg="commit_message_prefix" value="${esc(c.commit_message_prefix)}"></div></div>
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
      <div class="card"><div class="card-head"><h3>GitHub intake</h3></div><div class="card-body">
        <div class="field inline"><label>Watch repositories for eligible issues</label><span class="switch ${c.github_intake_enabled ? "on" : ""}" data-sw-cfg="github_intake_enabled"></span></div>
        <div class="field"><label>Poll interval (seconds)</label><input type="number" min="15" data-cfg="github_poll_seconds" value="${esc(c.github_poll_seconds)}" style="width:120px"></div>
        <a class="btn sm" href="#/github">${icon("github")}Manage watched repositories</a>
      </div></div>`;
      bindAuto();
    } else if (cur === "appearance") {
      body.innerHTML = `<div class="card"><div class="card-head"><h3>Appearance</h3></div><div class="card-body">
        <div class="grid2"><div class="field"><label>Theme</label><select data-cfg="ui_theme">${["system", "light", "dark"].map((x) => `<option ${c.ui_theme === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
        <div class="field"><label>Density</label><select data-cfg="ui_density">${["comfortable", "compact"].map((x) => `<option ${c.ui_density === x ? "selected" : ""}>${x}</option>`).join("")}</select></div></div>
        <div class="field inline"><label>Desktop notifications (questions, approvals, delivery, failures)</label><span class="switch ${c.ui_notifications ? "on" : ""}" data-sw-cfg="ui_notifications"></span></div>
        <div class="field inline"><label>Sound on attention events</label><span class="switch ${c.ui_sound ? "on" : ""}" data-sw-cfg="ui_sound"></span></div>
        <div class="hint">Keyboard: <kbd>Ctrl</kbd>+<kbd>K</kbd> command palette · <kbd>N</kbd> new task · <kbd>/</kbd> search · <kbd>[</kbd> <kbd>]</kbd> inspector tabs · <kbd>Esc</kbd> close.</div>
      </div></div>`;
      bindAuto();
      $$("[data-cfg='ui_theme'],[data-cfg='ui_density']", body).forEach((s) => s.addEventListener("change", () => bus.emit("theme", { theme: $("[data-cfg='ui_theme']", body).value, density: $("[data-cfg='ui_density']", body).value })));
      $("[data-sw-cfg='ui_notifications']", body).addEventListener("click", () => { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); });
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
        <div>Safety boundary: the orchestrator commits, pushes task branches and opens draft PRs. It never merges, force-pushes, or deploys.</div>
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
  render();
  return { update(reason) { if (reason === "route") { const s = S.route.section; if (s && s !== cur && SECTIONS.some(([k]) => k === s)) { cur = s; render(); } } }, destroy() {} };
}
