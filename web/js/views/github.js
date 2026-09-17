// GitHub inbox: watched repositories → auto-queued issues.
import { $, $$, esc, icon, toast, confirm, timeAgo, skeleton } from "../ui.js";
import { S } from "../state.js";
import { api } from "../api.js";

// Accept what people actually paste: owner/repo, a browser URL (even one deep inside
// the repository) or a clone URL. Mirrors normalize_repo_full_name on the server.
export function normalizeRepo(value) {
  let v = String(value || "").trim();
  const m = v.match(/^(?:(?:https?:\/\/)?(?:www\.)?github\.com\/|ssh:\/\/git@github\.com\/|git@github\.com:)(.*)$/i);
  if (m) v = m[1].split(/[?#]/)[0].split("/").slice(0, 2).join("/");
  return v.replace(/\.git$/i, "").replace(/^\/+|\/+$/g, "");
}
const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/;
function repoProblem(raw) {
  if (!raw.trim()) return "Enter a repository as owner/repository.";
  if (!REPO_RE.test(normalizeRepo(raw))) return "Use owner/repository, for example acme/web, or paste the repository's GitHub URL.";
  return "";
}

export function mountGithub(main) {
  main.innerHTML = `<div class="page" id="ghPage">${skeleton("page", 2)}</div>`;
  let alive = true;
  async function render() {
    let st, sources;
    try { [st, sources] = await Promise.all([api.ghStatus(), api.ghSources()]); } catch (e) { $("#ghPage", main).innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (!alive) return;
    const presets = S.presets || [];
    // Live updates redraw the page; carry over what you were typing and any error shown.
    const kept = $("#gRepo", main) ? { fields: ["gRepo", "gLocal", "gLabel", "gPreset"].map((id) => [id, $(`#${id}`, main).value]),
      errors: ["gRepoErr", "gLocalErr", "gFormErr"].map((id) => [id, $(`#${id}`, main).hidden ? "" : $(`#${id}`, main).textContent]),
      focus: document.activeElement?.id } : null;
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
          <div class="field"><label for="gRepo">Repository</label><input id="gRepo" placeholder="owner/repository or GitHub URL" autocomplete="off" spellcheck="false" aria-describedby="gRepoHelp gRepoErr"><div class="help" id="gRepoHelp">For example acme/web. A pasted GitHub link works too.</div><div class="field-error" id="gRepoErr" role="alert" hidden></div></div>
          <div class="field"><label for="gLocal">Local clone path (optional)</label><input id="gLocal" placeholder="Blank = managed clone under managed-repos/" spellcheck="false" aria-describedby="gLocalErr"><div class="field-error" id="gLocalErr" role="alert" hidden></div></div>
          <div class="grid2"><div class="field"><label>Agent label</label><input id="gLabel" value="${esc(S.config.github_default_label || "agent")}"></div>
          <div class="field"><label>Workflow preset</label><select id="gPreset"><option value="">Default (${esc(S.config.workflow_preset || "")})</option>${presets.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}</select></div></div>
          <div class="field inline"><label>Include issues assigned to me</label><input type="checkbox" id="gAssigned" checked></div>
          <div class="field inline"><label>Include issues with the label</label><input type="checkbox" id="gWatchLabel" checked></div>
          <div class="field inline"><label>Queue automatically (otherwise create drafts)</label><input type="checkbox" id="gAuto" checked></div>
          <div class="field-error" id="gFormErr" role="alert" hidden></div>
          <div class="modal-actions" style="margin-top:8px"><button class="btn primary" id="gAdd">${icon("plus")}Add repository</button></div>
        </div></div>
      </div>`;
    $("#pollNow", main).onclick = async () => { try { await api.ghPoll(); toast("info", "Polling GitHub"); setTimeout(render, 3000); } catch (e) { toast("error", "Could not poll", e.message); } };
    const repoInput = $("#gRepo", main);
    const setError = (id, msg) => {
      const box = $(`#${id}`, main);
      box.textContent = msg || "";
      box.hidden = !msg;
      const input = { gRepoErr: repoInput, gLocalErr: $("#gLocal", main) }[id];
      if (input) input.toggleAttribute("aria-invalid", !!msg);
    };
    // Tidy a pasted URL into owner/repository as soon as you leave the field.
    repoInput.addEventListener("blur", () => {
      const v = repoInput.value.trim();
      if (!v) return;
      if (normalizeRepo(v) !== v && REPO_RE.test(normalizeRepo(v))) repoInput.value = normalizeRepo(v);
      setError("gRepoErr", repoProblem(repoInput.value));
    });
    repoInput.addEventListener("input", () => { if (!$("#gRepoErr", main).hidden && !repoProblem(repoInput.value)) setError("gRepoErr", ""); });
    if (kept) {
      kept.fields.forEach(([id, v]) => { $(`#${id}`, main).value = v; });
      kept.errors.forEach(([id, msg]) => setError(id, msg));
      if (kept.focus && $(`#${kept.focus}`, main)) $(`#${kept.focus}`, main).focus();
    }
    repoInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#gAdd", main).click(); } });
    $("#gAdd", main).onclick = async () => {
      setError("gFormErr", ""); setError("gLocalErr", "");
      const problem = repoProblem(repoInput.value);
      setError("gRepoErr", problem);
      if (problem) { repoInput.focus(); return; }
      const btn = $("#gAdd", main);
      btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Adding…`;
      try {
        await api.ghAddSource({ repo: normalizeRepo(repoInput.value), local_path: $("#gLocal", main).value, label: $("#gLabel", main).value, preset: $("#gPreset", main).value,
          assigned_to_me: $("#gAssigned", main).checked, watch_label: $("#gWatchLabel", main).checked, auto_queue: $("#gAuto", main).checked, enabled: true });
        toast("success", "Repository watched", normalizeRepo(repoInput.value));
        repoInput.value = ""; $("#gLocal", main).value = "";
        render();
      } catch (e) {
        // The server names the field at fault; anything else is shown under the form.
        const field = { repo: "gRepoErr", local_path: "gLocalErr" }[e.data?.field];
        setError(field || "gFormErr", e.status ? e.message : `Could not reach Relay: ${e.message}`);
        (field === "gLocalErr" ? $("#gLocal", main) : repoInput).focus();
        btn.disabled = false; btn.innerHTML = `${icon("plus")}Add repository`;
      }
    };
    $$("[data-del]", main).forEach((b) => (b.onclick = async () => { if (await confirm("Stop watching?", "Existing tasks are kept.", { danger: true, okLabel: "Remove" })) { try { await api.ghDeleteSource(b.dataset.del); } catch (e) { toast("error", "Could not remove", e.message); } render(); } }));
    $$("[data-toggle]", main).forEach((b) => (b.onclick = async () => { const s = sources.find((x) => x.id === b.dataset.toggle); if (s) { try { await api.ghAddSource({ ...s, enabled: s.enabled === false }); } catch (e) { toast("error", "Could not update", e.message); } render(); } }));
  }
  render();
  return { update(reason) { if (reason === "github" || reason === "task") render(); }, destroy() { alive = false; } };
}
