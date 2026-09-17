// New task wizard: repository → request → team → review.
import { $, $$, esc, icon, modal, toast, basename } from "../ui.js";
import { S, agentLabel, agentInitial, navigate, defaultModelLabel } from "../state.js";
import { api } from "../api.js";
import { connectorPicker } from "./connectors.js";

export function workflowEditor(host, wf, { agents, presets, showAdvanced = true, onChange }) {
  const health = S.agents || {};
  const roles = ["supervisor", "worker", "reviewer"];
  const draw = () => {
    host.innerHTML = `
      <div class="presets">${presets.map((p) => `<button type="button" class="preset ${wf.preset === p.id ? "active" : ""}" data-preset="${esc(p.id)}">
        <div class="flow">${["supervisor", "worker", "reviewer"].filter((r) => p.roles[r]?.agent).map((r) => `<span class="av sm ${esc(p.roles[r].agent)}" title="${r}">${esc(agentInitial(p.roles[r].agent))}</span>`).join(icon("arrowRight"))}</div>
        <strong>${esc(p.name)}</strong><p>${esc(p.description)}</p></button>`).join("")}
        <button type="button" class="preset ${wf.preset === "custom" ? "active" : ""}" data-preset="custom"><div class="flow">${icon("wand")}</div><strong>Custom</strong><p>Pick any agent for any role below.</p></button>
      </div>
      <div class="role-grid" style="margin-top:12px">${roles.map((r) => { const cur = wf.roles[r] || { agent: "", model: "" }; return `<div class="role-box">
        <div class="rt">${r}${r === "reviewer" ? ' <span style="text-transform:none;letter-spacing:0;font-weight:500">(optional)</span>' : ""}</div>
        <div class="agent-pick">${Object.keys(agents).filter((a) => agents[a].builtin || health[a]?.installed || cur.agent === a).map((a) => `<button type="button" data-role="${r}" data-agent="${a}" class="${cur.agent === a ? "active" : ""}" style="--agent:${esc(agents[a].color)}" title="${esc(health[a]?.ok ? "ready" : (health[a]?.error || "not ready"))}"><span class="av sm ${a}">${esc(agentInitial(a))}</span>${esc(agents[a].label)}${health[a] && !health[a].ok ? ' <span class="muted">!</span>' : ""}</button>`).join("")}${r === "reviewer" ? `<button type="button" data-role="reviewer" data-agent="" class="${!cur.agent ? "active" : ""}">none</button>` : ""}</div>
        ${(() => {
          if (!cur.agent) return '<div class="muted" style="font-size:11px">No agent in this role.</div>';
          const catalog = [...new Set([...((S.config.models || {})[cur.agent] || []), ...((S.config.model_recent || {})[cur.agent] || [])])];
          const custom = cur.model && !catalog.includes(cur.model);
          const efforts = (S.agentMeta[cur.agent] || {}).efforts || [];
          return `<label class="rl">Model</label>
          <select data-model-sel="${r}"><option value="">${esc(defaultModelLabel(cur.agent))}</option>${catalog.map((m) => `<option value="${esc(m)}" ${cur.model === m ? "selected" : ""}>${esc(m)}</option>`).join("")}<option value="__custom__" ${custom ? "selected" : ""}>Custom…</option></select>
          <input data-model="${r}" placeholder="exact model id passed to the CLI" value="${esc(custom ? cur.model : "")}" ${custom ? "" : "hidden"}>
          <label class="rl">Reasoning effort</label>
          ${efforts.length ? `<select data-effort="${r}"><option value="">${esc(S.agents?.[cur.agent]?.default_effort ? `CLI default (${S.agents[cur.agent].default_effort})` : "CLI default")}</option>${efforts.map((e) => `<option value="${e}" ${cur.effort === e ? "selected" : ""}>${e}</option>`).join("")}</select>` : '<select disabled><option>not supported by this CLI</option></select>'}`;
        })()}
      </div>`; }).join("")}</div>
      ${showAdvanced ? `<div class="grid3" style="margin-top:6px">
        <div class="field"><label>Max work packages</label><input type="number" min="1" max="60" data-wf="max_turns" value="${esc(wf.max_turns)}"><div class="help">Turn budget for the supervisor ↔ worker loop.</div></div>
        <div class="field"><label>Max review rounds</label><input type="number" min="1" max="10" data-wf="max_review_rounds" value="${esc(wf.max_review_rounds)}"></div>
        <div class="field"><label>Verification</label><select data-wf="verify_mode"><option value="each_report" ${wf.verify_mode === "each_report" ? "selected" : ""}>After every worker report</option><option value="before_review" ${wf.verify_mode === "before_review" ? "selected" : ""}>Only before review / delivery</option><option value="off" ${wf.verify_mode === "off" ? "selected" : ""}>Off</option></select></div>
      </div>
      <div class="grid2">
        <div class="field inline"><label>Require my approval before commit & PR</label><span class="switch ${wf.approval_before_delivery ? "on" : ""}" data-sw="approval_before_delivery"></span></div>
        <div class="field inline"><label>Agents may ask me questions</label><span class="switch ${wf.allow_agent_questions !== false ? "on" : ""}" data-sw="allow_agent_questions"></span></div>
      </div>
      <div class="field"><label>Verification commands (one per line, optional)</label><textarea data-wf="verification_commands" rows="2" placeholder="npm test&#10;python -m pytest -q">${esc((wf.verification_commands || []).join("\n"))}</textarea><div class="help">Auto-detected commands (npm scripts, pytest, gradle, …) are added too unless disabled. <label style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-cb="auto_detect_verification" ${wf.auto_detect_verification !== false ? "checked" : ""}> auto-detect</label></div></div>` : ""}`;
    $$("[data-preset]", host).forEach((b) => (b.onclick = () => {
      wf.preset = b.dataset.preset;
      const p = presets.find((x) => x.id === wf.preset);
      if (p) for (const r of roles) wf.roles[r] = { agent: p.roles[r]?.agent || "", model: "", effort: "" };
      draw(); onChange && onChange(wf);
    }));
    $$("[data-role][data-agent]", host).forEach((b) => (b.onclick = () => { const same = wf.roles[b.dataset.role]?.agent === b.dataset.agent; wf.roles[b.dataset.role] = { agent: b.dataset.agent, model: same ? (wf.roles[b.dataset.role]?.model || "") : "", effort: same ? (wf.roles[b.dataset.role]?.effort || "") : "" }; wf.preset = "custom"; draw(); onChange && onChange(wf); }));
    $$("[data-model-sel]", host).forEach((s) => s.addEventListener("change", () => {
      const r = s.dataset.modelSel;
      const input = host.querySelector(`[data-model="${r}"]`);
      if (s.value === "__custom__") { input.hidden = false; input.focus(); wf.roles[r].model = input.value.trim(); }
      else { input.hidden = true; wf.roles[r].model = s.value; }
      onChange && onChange(wf);
    }));
    $$("[data-model]", host).forEach((i) => i.addEventListener("input", () => { wf.roles[i.dataset.model].model = i.value.trim(); onChange && onChange(wf); }));
    $$("[data-effort]", host).forEach((s) => s.addEventListener("change", () => { wf.roles[s.dataset.effort].effort = s.value; onChange && onChange(wf); }));
    $$("[data-wf]", host).forEach((i) => i.addEventListener("change", () => { const k = i.dataset.wf; wf[k] = k === "verification_commands" ? i.value.split("\n").map((x) => x.trim()).filter(Boolean) : (i.type === "number" ? Number(i.value) : i.value); onChange && onChange(wf); }));
    $$("[data-sw]", host).forEach((s) => (s.onclick = () => { wf[s.dataset.sw] = !s.classList.contains("on"); s.classList.toggle("on"); onChange && onChange(wf); }));
    $$("[data-cb]", host).forEach((c) => c.addEventListener("change", () => { wf[c.dataset.cb] = c.checked; onChange && onChange(wf); }));
  };
  draw();
  return { get value() { return wf; }, redraw: draw };
}

export function defaultWorkflow() {
  const cfg = S.config || {};
  const p = (S.presets || []).find((x) => x.id === cfg.workflow_preset);
  const roles = {};
  for (const r of ["supervisor", "worker", "reviewer"]) roles[r] = { agent: p ? (p.roles[r]?.agent || "") : (cfg.roles?.[r]?.agent || ""), model: cfg.roles?.[r]?.model || "", effort: cfg.roles?.[r]?.effort || "" };
  return { preset: cfg.workflow_preset || "custom", roles, max_turns: cfg.max_turns || 12, max_review_rounds: cfg.max_review_rounds || 3, verify_mode: cfg.verify_mode || "each_report",
    approval_before_delivery: !!cfg.approval_before_delivery, allow_agent_questions: cfg.allow_agent_questions !== false, verification_commands: [], auto_detect_verification: cfg.auto_detect_verification !== false };
}

// A follow-up is a new task on a delivered task's branch, with the same repository and team.
export function openFollowUp(t) { openNewTask({ followUp: t }); }

export function openNewTask(prefill = {}) {
  const edit = prefill.edit;
  const parent = prefill.followUp;
  const data = {
    repo: edit?.repo || parent?.repo || prefill.repo || S.config.recent_repos?.[0] || "",
    name: edit?.name || (parent ? `Follow-up: ${String(parent.name || "").replace(/^(Follow-up:\s*)+/i, "")}` : ""), requirements: edit?.requirements || prefill.requirements || "", issue: edit?.issue || prefill.issue || "",
    template: edit?.template || parent?.template || "feature", priority: edit?.priority || parent?.priority || "normal", tags: (edit?.tags || parent?.tags || []).join(", "),
    workflow: edit ? JSON.parse(JSON.stringify(edit.workflow)) : parent?.workflow ? JSON.parse(JSON.stringify(parent.workflow)) : defaultWorkflow(), queue: true,
    branch: parent ? (parent.branch || parent.branch_name || "") : "", branchEdited: !!parent,
    // Related repositories of a multi-repository task: {repo, name, reason, component, github, checked, source}.
    related: ((edit || parent)?.repos || []).filter((r) => r.role !== "primary").map((r) => ({ repo: r.repo, name: basename(r.repo), reason: r.reason || "", component: r.component || "", github: r.github_repo || "", checked: true, source: "task" }))
      .concat((prefill.related || []).map((r) => ({ repo: r, name: basename(r), reason: "named in the request", component: "", github: "", checked: true, source: "task" }))),
    relatedFor: (edit || parent)?.repos?.length ? (edit || parent).repo : (prefill.related || []).length ? prefill.repo : "",
    // Autopilot: run after other tasks, automatic retries of agent crashes, a cost cap.
    depends_on: edit?.depends_on ? [...edit.depends_on] : parent && parent.status !== "done" ? [parent.id] : [],
    retry: edit?.retry_policy?.infra ?? "", cost_cap: edit?.cost_cap_usd || "",
    connectors: edit?.connectors || parent?.connectors || null, // null: the repository's default connectors
  };
  let step = edit || parent ? 2 : (prefill.step && data.repo ? prefill.step : 0);
  const parentNote = parent ? `<div class="followup-note">${icon("retry", "sm")}<div><strong>Follows up <a href="#/task/${encodeURIComponent(parent.id)}">${esc(parent.name)}</a></strong>
    <span>Builds on <code>${esc(data.branch)}</code> where it left off, with the same team.</span>
    ${parent.summary ? `<blockquote>${esc(parent.summary.length > 360 ? parent.summary.slice(0, 360) + "…" : parent.summary)}</blockquote>` : ""}</div></div>` : "";
  let repoInfo = null;
  const steps = ["Repository", "Related repos", "Request", "Team & workflow", "Review"];
  const m = modal(`<div class="wizard"><div class="wiz-steps" id="wizSteps"></div><div class="wiz-body" id="wizBody"></div></div>`, { wide: true });
  const stepsEl = $("#wizSteps", m.body), body = $("#wizBody", m.body);

  const drawSteps = () => { stepsEl.innerHTML = steps.map((s, i) => `<button type="button" class="${i === step ? "active" : ""} ${i < step ? "done" : ""}" data-step="${i}" title="${esc(s)}" ${i === step ? 'aria-current="step"' : ""}><i>${i < step ? "✓" : i + 1}</i><span>${esc(s)}</span></button>`).join(""); $$("[data-step]", stepsEl).forEach((b) => (b.onclick = () => { if (Number(b.dataset.step) <= step || data.repo) go(Number(b.dataset.step)); })); };
  const nav = (backLabel, nextLabel, nextPrimary = true) => `<div class="modal-actions">${backLabel ? `<button type="button" class="btn" id="wBack">${esc(backLabel)}</button>` : '<button type="button" class="btn" data-close>Cancel</button>'}<span style="flex:1"></span>${nextLabel ? `<button type="button" class="btn ${nextPrimary ? "primary" : ""}" id="wNext">${esc(nextLabel)}</button>` : ""}</div>`;
  const go = (i) => { step = i; drawSteps(); render(); };

  async function loadRepoInfo() {
    const box = $("#repoFacts", body); if (!box) return;
    if (!data.repo) { box.innerHTML = ""; return; }
    box.innerHTML = '<span class="muted">Inspecting…</span>';
    try {
      repoInfo = await api.repoInfo(data.repo);
      box.innerHTML = `<span class="badge ${repoInfo.is_git ? "green" : "red"}">${repoInfo.is_git ? "git repository" : "not a git repo"}</span>${repoInfo.branch ? `<span class="badge outline">${icon("branch", "sm")}${esc(repoInfo.branch)}</span>` : ""}${repoInfo.remote ? `<span class="badge outline">${icon("github", "sm")}${esc(repoInfo.remote.replace(/^.*github\.com[/:]/, "").replace(/\.git$/, ""))}</span>` : ""}${repoInfo.dirty ? `<span class="badge amber">${repoInfo.dirty} uncommitted change(s) will be carried into the worktree</span>` : ""}${(repoInfo.detected_checks || []).length ? `<span class="badge blue">checks: ${esc(repoInfo.detected_checks.join(", "))}</span>` : '<span class="badge">no checks detected</span>'}`;
    } catch (e) { repoInfo = null; box.innerHTML = `<span class="badge red">${esc(e.message)}</span>`; }
  }
  async function drawBrowser(path) {
    const host = $("#browser", body); if (!host) return;
    host.innerHTML = '<div class="bpath"><span>Loading…</span></div>';
    try {
      const r = await api.browse(path);
      host.innerHTML = `<div class="bpath">${r.parent !== null && r.parent !== undefined ? `<button type="button" class="btn xs" id="bUp" title="Up one folder" aria-label="Up one folder">${icon("chevron")}</button>` : ""}<span class="bpath-path" title="${esc(r.path)}">${esc(r.path || "This PC")}</span>${r.is_git ? '<span class="badge green">git</span>' : ""}<button type="button" class="btn xs primary" id="bUse" ${r.path ? "" : "disabled"}>Use this folder</button></div>
        <div class="blist">${r.dirs.map((d) => `<button type="button" class="bitem" data-p="${esc(d.path)}">${icon("folder")}<span class="truncate">${esc(d.name)}</span>${d.git ? '<span class="badge green git">git</span>' : ""}</button>`).join("") || '<div class="empty small">No subfolders</div>'}</div>`;
      $("#bUp", host) && ($("#bUp", host).onclick = () => drawBrowser(r.parent));
      $("#bUse", host).onclick = () => { data.repo = r.path; $("#repoInput", body).value = r.path; loadRepoInfo(); };
      $$("[data-p]", host).forEach((b) => (b.onclick = () => drawBrowser(b.dataset.p)));
    } catch (e) { host.innerHTML = `<div class="bpath"><span>${esc(e.message)}</span></div>`; }
  }

  async function loadClonable() {
    const list = $("#cloneRepos", body), help = $("#cloneHelp", body); if (!list) return;
    try {
      const r = await api.ghRepos();
      list.innerHTML = r.repos.map((x) => `<option value="${esc(x.repo)}">${esc([x.private ? "private" : "public", x.description].filter(Boolean).join(" · "))}</option>`).join("");
      help.textContent = `${r.repos.length} repositories available. Clones go to ${r.root}; an existing clone is fetched and reused.`;
    } catch (e) { help.textContent = `Could not list GitHub repositories (${e.message}). You can still type owner/repository or a git URL.`; }
  }
  async function cloneSelected() {
    const input = $("#cloneRepo", body), btn = $("#cloneBtn", body);
    const repo = input.value.trim(); if (!repo) { toast("warning", "Pick or type a repository to clone"); return; }
    btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Cloning…`;
    try {
      const r = await api.ghClone(repo, $("#cloneName", body).value.trim());
      data.repo = r.path; $("#repoInput", body).value = r.path;
      toast("success", r.cloned ? "Repository cloned" : "Using existing clone", r.path);
      loadRepoInfo(); drawBrowser(r.path);
    } catch (e) { toast("error", "Clone failed", e.message); }
    finally { btn.disabled = false; btn.innerHTML = `${icon("download")}Clone`; }
  }

  // ------------------------------------------------------------ related repositories (multi-repository tasks)
  const checkedRelated = () => data.related.filter((r) => r.checked && r.repo);
  async function drawRelated() {
    body.innerHTML = `<h2>Related repositories</h2>
      <p class="hint">A feature often needs more than one repository: the UI and the service behind it, the API and its database functions. Each ticked repository gets a worktree on the same branch, its own environment and checks, and its own pull request. The team can also ask to add one while planning.</p>
      <div id="relList" class="rel-list"><div class="muted">Looking at the system map…</div></div>
      <div class="field"><label>Add another repository</label>
        <div class="clone-row"><select id="relLocal"><option value="">Loading local repositories…</option></select><button type="button" class="btn" id="relAdd">${icon("plus")}Add</button></div>
        <div class="clone-row" style="margin-top:6px"><input id="relClone" placeholder="or clone owner/repository" autocomplete="off"><button type="button" class="btn" id="relCloneBtn">${icon("download")}Clone and add</button></div>
        <div class="help">Suggestions come from dependencies in the <a href="#/repos/system" data-close>system map</a>; approved ones are ticked.</div></div>
      ${nav("Back", "Next: describe the request")}`;
    $$("[data-close]", body).forEach((b) => b.addEventListener("click", m.close));
    $("#wBack", body).onclick = () => go(0);
    $("#wNext", body).onclick = async () => {
      const btn = $("#wNext", body);
      const missing = data.related.filter((r) => r.checked && !r.repo && r.github);
      if (missing.length) {
        btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Cloning ${missing.length}…`;
        for (const r of missing) {
          try { const res = await api.ghClone(r.github); r.repo = res.path; }
          catch (e) { toast("error", `Could not clone ${r.github}`, e.message); r.checked = false; }
        }
      }
      go(2);
    };
    const list = $("#relList", body);
    const paint = () => {
      list.innerHTML = data.related.length ? data.related.map((r, i) => `<label class="rel-row ${r.checked ? "on" : ""}">
          <input type="checkbox" data-rel="${i}" ${r.checked ? "checked" : ""}>
          <span class="min0 stack" style="gap:2px"><span class="row wrap" style="gap:6px"><strong>${esc(r.name)}</strong>${r.kind ? `<span class="badge">${esc(r.kind)}</span>` : ""}
            ${r.status === "approved" ? '<span class="badge green">approved dependency</span>' : r.status === "proposed" ? '<span class="badge amber">proposed by scan</span>' : r.source === "task" ? '<span class="badge outline">on this task</span>' : '<span class="badge outline">added by you</span>'}
            ${!r.repo ? '<span class="badge amber">cloned when you continue</span>' : ""}</span>
            ${r.reason ? `<span class="muted rel-reason">${esc(r.reason)}</span>` : ""}
            <span class="mono muted truncate" title="${esc(r.repo || r.github)}">${esc(r.repo || r.github)}</span></span></label>`).join("")
        : `<div class="empty small">${icon("globe")}<p>No related repositories known for ${esc(basename(data.repo))}. Scan your repositories in the system map, or add one below.</p></div>`;
      $$("[data-rel]", list).forEach((c) => (c.onchange = () => { data.related[Number(c.dataset.rel)].checked = c.checked; paint(); }));
    };
    if (data.relatedFor !== data.repo) {
      // A different primary repository: start again from the map's suggestions for it, keeping what you added by hand.
      data.related = data.related.filter((r) => r.source === "manual" && r.repo !== data.repo);
      data.relatedFor = data.repo;
      try {
        const r = await api.systemRelated(data.repo);
        for (const s of r.suggestions || []) {
          if (s.path === data.repo || data.related.some((x) => (s.path && x.repo === s.path) || (s.component && x.component === s.component))) continue;
          data.related.push({ repo: s.cloned ? s.path : "", name: s.name, reason: s.reason, component: s.component, github: s.repo, kind: s.kind, status: s.status, checked: !!s.checked, source: "map" });
        }
      } catch {}
    }
    if (step !== 1) return;
    paint();
    const sel = $("#relLocal", body);
    try {
      const r = await api.repos();
      const taken = new Set([data.repo, ...data.related.map((x) => x.repo)]);
      sel.innerHTML = `<option value="">Choose a local repository</option>` + r.repos.filter((x) => !taken.has(x.path)).map((x) => `<option value="${esc(x.path)}">${esc(x.name)}${x.github ? ` · ${esc(x.github)}` : ""}</option>`).join("");
    } catch { sel.innerHTML = '<option value="">Could not list repositories</option>'; }
    $("#relAdd", body).onclick = () => {
      if (!sel.value) return;
      if (!data.related.some((x) => x.repo === sel.value)) data.related.push({ repo: sel.value, name: basename(sel.value), reason: "", checked: true, source: "manual" });
      sel.querySelector(`option[value="${CSS.escape(sel.value)}"]`)?.remove(); sel.value = "";
      paint();
    };
    $("#relCloneBtn", body).onclick = async () => {
      const spec = $("#relClone", body).value.trim(); if (!spec) return;
      const btn = $("#relCloneBtn", body); btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Cloning…`;
      try {
        const res = await api.ghClone(spec);
        if (res.path !== data.repo && !data.related.some((x) => x.repo === res.path)) data.related.push({ repo: res.path, name: basename(res.path), reason: "", checked: true, source: "manual" });
        $("#relClone", body).value = ""; paint();
      } catch (e) { toast("error", "Clone failed", e.message); }
      finally { btn.disabled = false; btn.innerHTML = `${icon("download")}Clone and add`; }
    };
  }

  function render() {
    draw();
    // Step bodies are drawn after the modal wired its close buttons, so wire theirs here.
    $$("[data-close]", body).forEach((b) => b.addEventListener("click", m.close));
  }
  function draw() {
    if (step === 0) {
      body.innerHTML = `<h2>Choose the repository</h2><p class="hint">The team works in an isolated git worktree and branch; your checkout is never touched.</p>
        <div class="field"><label>Repository folder</label><input id="repoInput" value="${esc(data.repo)}" placeholder="C:\\Users\\you\\projects\\app"><div class="repo-facts" id="repoFacts"></div></div>
        ${(S.config.recent_repos || []).length ? `<div class="field"><label>Recent</label><div class="templ">${S.config.recent_repos.slice(0, 8).map((r) => `<button type="button" class="chip" data-recent="${esc(r)}" title="${esc(r)}">${icon("folder", "sm")} ${esc(basename(r))}</button>`).join("")}</div></div>` : ""}
        <div class="field"><label>Or clone from GitHub</label>
          <div class="clone-row"><input id="cloneRepo" list="cloneRepos" placeholder="owner/repository or git URL" autocomplete="off"><input id="cloneName" placeholder="folder (optional)"><button type="button" class="btn primary" id="cloneBtn">${icon("download")}Clone</button></div>
          <datalist id="cloneRepos"></datalist><div class="help" id="cloneHelp">Loading your repositories…</div></div>
        <div class="field"><label>Browse</label><div class="browser" id="browser"></div></div>${nav(null, "Next: related repositories")}`;
      const input = $("#repoInput", body);
      input.addEventListener("change", () => { data.repo = input.value.trim().replace(/^"|"$/g, ""); loadRepoInfo(); });
      $$("[data-recent]", body).forEach((b) => (b.onclick = () => { data.repo = b.dataset.recent; input.value = data.repo; loadRepoInfo(); drawBrowser(data.repo); }));
      loadRepoInfo();
      drawBrowser(data.repo || "");
      loadClonable();
      $("#cloneBtn", body).onclick = cloneSelected;
      $("#cloneRepo", body).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); cloneSelected(); } });
      $("#wNext", body).onclick = () => { data.repo = input.value.trim().replace(/^"|"$/g, ""); if (!data.repo) { toast("warning", "Choose a repository folder"); return; } if (repoInfo && !repoInfo.is_git) { toast("error", "Not a git repository", "Run git init and make an initial commit first."); return; } go(1); };
    } else if (step === 1) {
      drawRelated();
    } else if (step === 2) {
      const tpl = (S.templates || []).find((x) => x.id === data.template) || {};
      const prompts = S.config.saved_prompts || [];
      body.innerHTML = `<h2>${parent ? "What should happen next?" : "Describe the request"}</h2><p class="hint">${parent ? "Describe only what should change on top of the delivered work. The team sees the branch as it is now." : "Write it once. The supervisor turns it into a plan, acceptance criteria and work packages."}</p>
        ${parentNote}
        <div class="field"><div class="row between wrap field-label"><span>Start from a saved prompt</span><a href="#/settings/prompts" data-close>${prompts.length ? "Manage" : "Create one"}</a></div>
          ${prompts.length ? `<div class="templ saved-prompts">${prompts.map((p) => `<button type="button" class="chip prompt-chip ${p.text === data.requirements ? "active" : ""}" data-prompt="${esc(p.id)}" title="${esc(p.text.slice(0, 400))}">${icon("message", "sm")}<span class="truncate">${esc(p.name)}</span></button>`).join("")}</div>`
            : '<div class="help">Save requests you make often in Settings → Saved prompts and pick them here.</div>'}</div>
        <div class="field"><label>Type</label><div class="templ">${(S.templates || []).map((x) => `<button type="button" class="chip ${data.template === x.id ? "active" : ""}" data-tpl="${esc(x.id)}">${esc(x.name)}</button>`).join("")}</div></div>
        <div class="field"><label>Requirements</label><textarea id="req" rows="9" placeholder="${esc(parent ? `What should change next? Last time: ${(parent.summary || parent.name).slice(0, 160)}` : (tpl.hint || "Describe what you want…"))}">${esc(data.requirements)}</textarea><div class="help">${esc(tpl.hint || "")}</div></div>
        <div class="grid3">
          <div class="field"><label>Task name (optional)</label><input id="tname" value="${esc(data.name)}" placeholder="auto from the first line"></div>
          <div class="field"><label>GitHub issue # (optional)</label><input id="tissue" value="${esc(data.issue)}" placeholder="123"></div>
          <div class="field"><label>Priority</label><select id="tprio">${["urgent", "high", "normal", "low"].map((p) => `<option ${data.priority === p ? "selected" : ""}>${p}</option>`).join("")}</select></div>
        </div>
        <div class="field"><label>Tags (comma separated)</label><input id="ttags" value="${esc(data.tags)}" placeholder="frontend, billing"></div>
        ${nav("Back", "Next: choose the team")}`;
      $$("[data-tpl]", body).forEach((b) => (b.onclick = () => { data.template = b.dataset.tpl; collect1(); render(); }));
      $(".followup-note a", body)?.addEventListener("click", () => m.close());
      $$("[data-prompt]", body).forEach((b) => (b.onclick = () => {
        const p = prompts.find((x) => x.id === b.dataset.prompt);
        if (!p) return;
        collect1();
        const before = { requirements: data.requirements, template: data.template };
        data.requirements = p.text;
        if (p.template && (S.templates || []).some((x) => x.id === p.template)) data.template = p.template;
        render();
        // Land on the first <placeholder> so typing replaces it straight away.
        const ta = $("#req", body), hole = /<[^<>\n]{1,80}>/.exec(ta.value);
        ta.focus();
        if (hole) ta.setSelectionRange(hole.index, hole.index + hole[0].length);
        if (before.requirements.trim() && before.requirements !== p.text) {
          toast("info", "Requirements replaced", `Filled from “${p.name}”.`, { action: { label: "Undo", onClick: () => { Object.assign(data, before); if (step === 2) render(); } } });
        }
      }));
      const collect1 = () => { data.requirements = $("#req", body).value; data.name = $("#tname", body).value; data.issue = $("#tissue", body).value; data.priority = $("#tprio", body).value; data.tags = $("#ttags", body).value; };
      $("#wBack", body).onclick = () => { collect1(); go(1); };
      $("#wNext", body).onclick = () => { collect1(); if (!data.requirements.trim() && !data.issue.trim()) { toast("warning", "Describe the task or give an issue number"); return; } go(3); };
    } else if (step === 3) {
      body.innerHTML = `<h2>Team & workflow</h2><p class="hint">One agent supervises: it plans, delegates, verifies and decides. The other implements. Optionally a third reviews independently before delivery.</p><div id="wfEditor"></div>${nav("Back", "Next: review")}`;
      workflowEditor($("#wfEditor", body), data.workflow, { agents: S.agentMeta, presets: S.presets });
      $("#wBack", body).onclick = () => go(2);
      $("#wNext", body).onclick = () => { const r = data.workflow.roles; if (!r.supervisor.agent || !r.worker.agent) { toast("warning", "Pick a supervisor and a worker"); return; } go(4); };
    } else {
      const r = data.workflow.roles;
      const h = S.agents || {};
      const warn = ["supervisor", "worker", "reviewer"].filter((x) => r[x].agent && h[r[x].agent] && !h[r[x].agent].ok).map((x) => `${agentLabel(r[x].agent)} (${x}) is not ready: ${h[r[x].agent].error || "check Agents page"}`);
      body.innerHTML = `<h2>${edit ? "Save changes" : "Ready to launch"}</h2>
        <div class="summary-box">
          <div><b>Repository</b>${esc(data.repo)}</div>
          ${checkedRelated().length ? `<div><b>Also changes</b>${checkedRelated().map((r) => esc(r.name)).join(", ")} <span class="muted">(same branch in each, one pull request per repository)</span></div>` : ""}
          ${parent ? `<div><b>Follows up</b>${esc(parent.name)}</div>` : ""}
          <div><b>Request</b>${esc((data.requirements || `Issue #${data.issue}`).slice(0, 400))}${data.requirements.length > 400 ? "…" : ""}</div>
          <div><b>Team</b>${["supervisor", "worker", "reviewer"].filter((x) => r[x].agent).map((x) => `${esc(agentLabel(r[x].agent))} (${x}${r[x].model ? `, ${esc(r[x].model)}` : ", CLI default model"}${r[x].effort ? `, ${esc(r[x].effort)} effort` : ""})`).join(" · ")}</div>
          <div><b>Budget</b>${data.workflow.max_turns} work packages · ${data.workflow.max_review_rounds} review rounds · verification ${esc(data.workflow.verify_mode)}${data.workflow.approval_before_delivery ? " · approval gate on" : ""}</div>
          <div><b>Delivery</b>isolated branch → ${S.config.github_auto_create_pr ? "draft PR" : "branch only"} (never merges)</div>
        </div>
        ${warn.length ? `<div class="modal-error" style="margin-top:12px">${warn.map(esc).join("<br>")}<br><a href="#/agents" data-close>Open Agents page</a></div>` : ""}
        ${!edit ? `<div class="field" style="margin-top:14px"><label>Branch</label><input id="tbranch" class="mono" value="${esc(data.branch)}" placeholder="suggesting…" spellcheck="false"><div class="help">${parent && data.branch === (parent.branch || parent.branch_name) ? `The branch ${esc(parent.name)} delivered; this task adds commits on top of it.` : "Suggested from the task name. Edit it freely; an existing branch is built on."}</div></div>` : ""}
        ${autopilotOptions(data, edit)}
        <details class="field conn-task" id="connBox" style="margin-top:14px"><summary class="field-label">Advanced · connectors <span class="muted" id="connSum">loading…</span></summary>
          <div id="connPick" style="margin-top:8px"></div><div class="help">Real environments the agents may check through Relay. The default is the repository's connectors, never production.</div></details>
        ${!edit ? `<div class="field inline" style="margin-top:14px"><label>Queue immediately (and start the queue if idle)</label><span class="switch ${data.queue ? "on" : ""}" id="qSwitch"></span></div>` : ""}
        <div class="modal-actions"><button type="button" class="btn" id="wBack">Back</button><span style="flex:1"></span><button type="button" class="btn primary" id="wCreate">${icon(edit ? "save" : "sparkles")}${edit ? "Save" : data.queue ? "Create & queue" : "Create draft"}</button></div>`;
      $("#wBack", body).onclick = () => go(3);
      api.connectorsForRepo(data.repo).then((r) => {
        const host = $("#connPick", body); if (!host) return;
        const sum = (names) => { const s = $("#connSum", body); if (s) s.textContent = `· ${names.length ? names.join(", ") : "none"}${data.connectors ? "" : " (repository default)"}`; };
        const current = data.connectors || r.defaults;
        sum(current);
        connectorPicker(host, { connectors: r.connectors, selected: current, onChange: (names) => { data.connectors = names; sum(names); } });
      }).catch(() => { const s = $("#connSum", body); if (s) s.textContent = "· unavailable"; });
      const bInput = $("#tbranch", body);
      if (bInput) {
        bInput.addEventListener("input", () => { data.branch = bInput.value.trim(); data.branchEdited = true; });
        if (!data.branchEdited) api.branchName({ repo: data.repo, name: data.name, requirements: data.requirements, template: data.template, issue: data.issue })
          .then((r) => { if (!data.branchEdited) { data.branch = r.branch; bInput.value = r.branch; } }).catch(() => { bInput.placeholder = "chosen when the task starts"; });
      }
      const collectAp = () => {
        data.depends_on = $$("[data-dep]:checked", body).map((c) => c.value);
        data.retry = $("#tretry", body)?.value ?? data.retry;
        data.cost_cap = $("#tcap", body)?.value ?? data.cost_cap;
      };
      $$("[data-dep], #tretry, #tcap", body).forEach((i) => i.addEventListener("change", collectAp));
      $("#qSwitch", body) && ($("#qSwitch", body).onclick = () => { collectAp(); data.queue = !data.queue; render(); });
      $("#wCreate", body).onclick = async () => {
        const btn = $("#wCreate", body); btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}${edit ? "Saving…" : "Creating…"}`;
        try {
          const payload = { repo: data.repo, name: data.name, requirements: data.requirements, issue: data.issue, template: data.template, priority: data.priority,
            tags: data.tags.split(",").map((x) => x.trim()).filter(Boolean), workflow: data.workflow, queue: data.queue, branch: edit ? undefined : data.branch, follow_up_of: parent?.id,
            repos: checkedRelated().map((r) => ({ repo: r.repo, reason: r.reason, component: r.component })),
            depends_on: data.depends_on, cost_cap_usd: Number(data.cost_cap) || 0 };
          if (String(data.retry).trim() !== "") payload.retry_policy = { infra: Math.max(0, Number(data.retry) || 0) };
          if (data.connectors) payload.connectors = data.connectors; // untouched: the repository's defaults apply when the task starts
          if (edit) { await api.updateTask(edit.id, payload); toast("success", "Task updated"); m.close(); return; }
          const t = await api.createTask(payload);
          m.close();
          toast("success", data.queue ? "Task queued" : "Draft created", t.name);
          if (data.queue) { try { await api.queueStart(); } catch {} }
          navigate(`#/task/${t.id}`);
        } catch (e) { toast("error", edit ? "Could not save" : "Could not create task", e.message); btn.disabled = false; btn.innerHTML = edit ? "Save" : "Create & queue"; }
      };
    }
  }
  drawSteps(); render();
}

// "Runs after" and the other autopilot options on the review step.
function autopilotOptions(data, edit) {
  const open = [...S.tasks.values()].filter((t) => !t.archived && t.id !== edit?.id && !["stopped"].includes(t.status) && (t.status !== "done" || data.depends_on.includes(t.id)))
    .sort((a, b) => (b.number || 0) - (a.number || 0)).slice(0, 30);
  const n = data.depends_on.length;
  return `<details class="ap-opts" ${n || data.cost_cap || String(data.retry) !== "" ? "open" : ""}><summary>${icon("clock", "sm")}Autopilot options${n ? ` · runs after ${n} task${n === 1 ? "" : "s"}` : ""}</summary>
    <div class="field"><label>Runs after</label>${open.length ? `<div class="dep-list">${open.map((t) => `<label class="dep-opt"><input type="checkbox" data-dep value="${esc(t.id)}" ${data.depends_on.includes(t.id) ? "checked" : ""}><span class="truncate">${t.number ? `#${esc(t.number)} ` : ""}${esc(t.name)}</span><span class="muted">${esc(t.status)}</span></label>`).join("")}</div>` : '<div class="help">No other open tasks.</div>'}
      <div class="help">It starts only once these are delivered. Other queued tasks carry on meanwhile.</div></div>
    <div class="grid2">
      <div class="field"><label for="tretry">Automatic retries after an agent crash or hang</label><input id="tretry" type="number" min="0" max="5" value="${esc(data.retry)}" placeholder="default (${esc(S.config.autopilot?.retry_infra_failures ?? 1)})"><div class="help">Never for judge outcomes such as a used-up budget.</div></div>
      <div class="field"><label for="tcap">Cost cap (USD, estimated)</label><input id="tcap" type="number" min="0" step="0.5" value="${esc(data.cost_cap)}" placeholder="${S.config.autopilot?.task_cost_cap_usd ? `default $${esc(S.config.autopilot.task_cost_cap_usd)}` : "no cap"}"><div class="help">The task pauses after the turn that crosses it.</div></div>
    </div></details>`;
}
