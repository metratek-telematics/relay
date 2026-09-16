// GitHub inbox: watched repositories → auto-queued issues.
import { $, $$, esc, icon, toast, confirm, timeAgo } from "../ui.js";
import { S } from "../state.js";
import { api } from "../api.js";

export function mountGithub(main) {
  main.innerHTML = `<div class="page" id="ghPage"><div class="empty small">Loading…</div></div>`;
  let alive = true;
  async function render() {
    let st, sources;
    try { [st, sources] = await Promise.all([api.ghStatus(), api.ghSources()]); } catch (e) { $("#ghPage", main).innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (!alive) return;
    const presets = S.presets || [];
    const ghTasks = [...S.tasks.values()].filter((t) => t.github_issue_key).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    $("#ghPage", main).innerHTML = `
      <div class="page-head"><div><h1>GitHub inbox</h1><p>Issues assigned to you or carrying the agent label enter the queue automatically. Delivered tasks push a branch and open a draft PR.</p></div>
        <div class="page-actions"><span class="badge ${st.ready ? "green" : "red"}">${st.ready ? `@${esc(st.login)}` : "not signed in"}</span><button class="btn" id="pollNow">${icon("refresh")}Poll now</button></div></div>
      ${!st.ready ? `<div class="modal-error" style="margin-bottom:16px">GitHub CLI is not ready: ${esc(st.error || "run gh auth login")}.</div>` : ""}
      <div class="dash-grid">
        <div class="stack" style="gap:16px">
          <div class="card"><div class="card-head"><h3>Watched repositories</h3><span class="muted" style="font-size:12px">${st.last_poll ? `last poll ${timeAgo(st.last_poll)}` : "watcher idle"}${st.error ? ` · <span style="color:var(--red)">${esc(st.error)}</span>` : ""}</span></div>
            <div class="card-body stack">${sources.length ? sources.map((s) => `<div class="src-row"><div><strong>${esc(s.repo)}</strong><span>label <code>${esc(s.label)}</code> · ${s.assigned_to_me ? "assigned to me · " : ""}${s.auto_queue === false ? "drafts only" : "auto-queue"} · ${esc(s.local_path || "managed clone")}${s.preset ? ` · ${esc(s.preset)}` : ""}</span></div><div class="row"><span class="badge ${s.enabled !== false ? "green" : ""}">${s.enabled !== false ? "on" : "off"}</span><button class="btn xs" data-toggle="${esc(s.id)}">${s.enabled !== false ? "Disable" : "Enable"}</button><button class="btn xs danger" data-del="${esc(s.id)}">${icon("trash")}</button></div></div>`).join("") : '<div class="empty small">No repositories watched yet.</div>'}</div></div>
          <div class="card"><div class="card-head"><h3>Tasks from GitHub</h3></div><div class="card-body stack">${ghTasks.length ? ghTasks.slice(0, 20).map((t) => `<a class="row between" href="#/task/${esc(t.id)}" style="color:inherit;text-decoration:none"><span class="truncate"><strong>${esc(t.name)}</strong> <span class="muted">${esc(t.github_repo || "")}</span></span><span class="badge">${esc(t.status)}</span></a>`).join("") : '<div class="empty small">No issue-driven tasks yet.</div>'}</div></div>
        </div>
        <div class="card"><div class="card-head"><h3>Watch a repository</h3></div><div class="card-body">
          <div class="field"><label>Repository (owner/name)</label><input id="gRepo" placeholder="owner/repository"></div>
          <div class="field"><label>Local clone path (optional)</label><input id="gLocal" placeholder="Blank = managed clone under managed-repos/"></div>
          <div class="grid2"><div class="field"><label>Agent label</label><input id="gLabel" value="${esc(S.config.github_default_label || "agent")}"></div>
          <div class="field"><label>Workflow preset</label><select id="gPreset"><option value="">Default (${esc(S.config.workflow_preset || "")})</option>${presets.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}</select></div></div>
          <div class="field inline"><label>Include issues assigned to me</label><input type="checkbox" id="gAssigned" checked></div>
          <div class="field inline"><label>Include issues with the label</label><input type="checkbox" id="gWatchLabel" checked></div>
          <div class="field inline"><label>Queue automatically (otherwise create drafts)</label><input type="checkbox" id="gAuto" checked></div>
          <div class="modal-actions" style="margin-top:8px"><button class="btn primary" id="gAdd">${icon("plus")}Add repository</button></div>
        </div></div>
      </div>`;
    $("#pollNow", main).onclick = async () => { await api.ghPoll(); toast("info", "Polling GitHub"); setTimeout(render, 3000); };
    $("#gAdd", main).onclick = async () => {
      try {
        await api.ghAddSource({ repo: $("#gRepo", main).value, local_path: $("#gLocal", main).value, label: $("#gLabel", main).value, preset: $("#gPreset", main).value,
          assigned_to_me: $("#gAssigned", main).checked, watch_label: $("#gWatchLabel", main).checked, auto_queue: $("#gAuto", main).checked, enabled: true });
        toast("success", "Repository watched"); render();
      } catch (e) { toast("error", "Could not add", e.message); }
    };
    $$("[data-del]", main).forEach((b) => (b.onclick = async () => { if (await confirm("Stop watching?", "Existing tasks are kept.", { danger: true, okLabel: "Remove" })) { await api.ghDeleteSource(b.dataset.del); render(); } }));
    $$("[data-toggle]", main).forEach((b) => (b.onclick = async () => { const s = sources.find((x) => x.id === b.dataset.toggle); if (s) { await api.ghAddSource({ ...s, enabled: s.enabled === false }); render(); } }));
  }
  render();
  return { update(reason) { if (reason === "github" || reason === "task") render(); }, destroy() { alive = false; } };
}
