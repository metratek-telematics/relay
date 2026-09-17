// Lessons: review what retrospectives proposed; approved lessons reach future prompts for their repository.
import { $, $$, esc, icon, toast, confirm, modal, timeAgo, skeleton } from "../ui.js";
import { S } from "../state.js";
import { api } from "../api.js";

export function mountLessons(main) {
  main.innerHTML = `<div class="page lessons-page" id="lessonsPage">${skeleton("page", 3)}</div>`;
  const page = $("#lessonsPage", main);
  let data = null, alive = true, filter = "all";
  const drafts = new Map(); // edits typed into the queue survive a refresh

  const repoName = (key) => (data.repos.find((r) => r.key === key) || {}).label || key || "";
  const scopeBadge = (x) => x.scope === "global" ? '<span class="badge purple">all repositories</span>' : `<span class="badge outline" title="${esc(x.repo)}">${esc(repoName(x.repo) || "repository")}</span>`;
  const from = (x) => x.task_id ? `from <a href="#/task/${esc(x.task_id)}">${esc(x.task_name || x.task_id)}</a>` : (x.source === "manual" ? "added by hand" : "");

  async function load() {
    try { data = await api.lessons(); } catch (e) { if (alive) page.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (alive) draw();
  }
  const apply = (d) => { data = d; draw(); };

  function queueItem(x) {
    const text = drafts.has(x.id) ? drafts.get(x.id) : x.text;
    return `<li class="lesson" data-id="${esc(x.id)}">
      <textarea class="input lesson-text" rows="2" data-draft="${esc(x.id)}" aria-label="Lesson text">${esc(text)}</textarea>
      ${x.evidence ? `<div class="lesson-evidence">${icon("eye", "sm")}<span>${esc(x.evidence)}</span></div>` : ""}
      <div class="lesson-foot">
        <span class="lesson-meta"><select class="input scope-sel" data-scope="${esc(x.id)}" aria-label="Where it applies">
          <option value="repo" ${x.scope === "repo" ? "selected" : ""} ${x.proposed_repo ? "" : "disabled"}>${esc(repoName(x.proposed_repo) || "this repository")}</option>
          <option value="global" ${x.scope === "global" ? "selected" : ""}>All repositories</option></select>
          <span class="muted">${from(x)} · ${esc(timeAgo(x.created_at))}</span></span>
        <span class="row" style="gap:6px"><button class="btn sm" data-reject="${esc(x.id)}">${icon("x")}Reject</button><button class="btn sm primary" data-approve="${esc(x.id)}">${icon("check")}Approve</button></span>
      </div></li>`;
  }
  function approvedItem(x) {
    const on = x.enabled !== false;
    return `<li class="lesson ${on ? "" : "off"}" data-id="${esc(x.id)}">
      <div class="lesson-body"><p>${esc(x.text)}</p>
        <div class="lesson-meta">${scopeBadge(x)}${x.edited ? '<span class="badge">edited</span>' : ""}<span class="muted">${from(x)}${x.approved_at ? ` · approved ${esc(timeAgo(x.approved_at))}` : ""}</span></div></div>
      <div class="lesson-actions"><span class="switch ${on ? "on" : ""}" data-toggle="${esc(x.id)}" role="switch" aria-checked="${on}" tabindex="0" title="${on ? "Used in prompts" : "Switched off"}"></span>
        <button class="btn xs ghost" data-edit="${esc(x.id)}" title="Edit">${icon("edit")}</button><button class="btn xs ghost" data-del="${esc(x.id)}" title="Delete">${icon("trash")}</button></div></li>`;
  }

  function draw() {
    const q = data.queue || [], ap = data.approved || [], rej = data.rejected || [];
    const repos = [...new Set(ap.filter((x) => x.scope === "repo").map((x) => x.repo))];
    if (filter !== "all" && filter !== "global" && !repos.includes(filter)) filter = "all";
    const shown = ap.filter((x) => filter === "all" || (filter === "global" ? x.scope === "global" : x.repo === filter));
    const max = S.config.lessons_max_in_prompt || 15;
    page.innerHTML = `
      <div class="page-head"><div><h1>Lessons</h1><p>What Relay learned from finished tasks. A short retrospective after each task proposes lessons; only the ones you approve are added to future prompts, at most ${esc(max)} per task.</p></div>
        <div class="page-actions"><button class="btn primary" id="lAdd">${icon("plus")}Add lesson</button></div></div>
      ${S.config.lessons_inject === false ? `<div class="card lessons-off"><div class="card-body row between wrap"><span class="row">${icon("alert")}Approved lessons are not being added to prompts.</span><a class="btn sm" href="#/settings/workflow">Settings</a></div></div>` : ""}
      <div class="card"><div class="card-head"><div><h3>Awaiting review</h3><p class="card-sub">Edit the wording or where it applies, then approve. Rejected lessons are not proposed again.</p></div><span class="badge ${q.length ? "amber" : ""}">${q.length}</span></div>
        <div class="card-body">${q.length ? `<ul class="lesson-list">${q.map(queueItem).join("")}</ul>` : `<div class="empty small">${icon("check", "lg")}<p>Nothing to review. Retrospectives propose lessons when a finished task shows something worth doing differently.</p></div>`}</div></div>
      <div class="card"><div class="card-head"><div><h3>Approved</h3><p class="card-sub">Repository lessons come first in a prompt, newest first, then lessons for all repositories.</p></div><span class="badge green">${ap.length}</span></div>
        <div class="card-body">
          ${ap.length ? `<div class="lesson-filter"><select class="input" id="lFilter" aria-label="Show lessons for"><option value="all">All lessons</option><option value="global" ${filter === "global" ? "selected" : ""}>All repositories</option>${repos.map((r) => `<option value="${esc(r)}" ${filter === r ? "selected" : ""}>${esc(repoName(r))}</option>`).join("")}</select></div>` : ""}
          ${shown.length ? `<ul class="lesson-list">${shown.map(approvedItem).join("")}</ul>` : `<div class="empty small">No approved lessons${filter === "all" ? " yet" : " here"}.</div>`}
        </div></div>
      ${rej.length ? `<details class="card lessons-rejected"><summary class="card-head"><h3>Recently rejected</h3><span class="badge">${rej.length}</span></summary><div class="card-body"><ul class="lesson-list">${rej.slice().reverse().map((x) => `<li class="lesson off" data-id="${esc(x.id)}"><div class="lesson-body"><p>${esc(x.text)}</p><div class="lesson-meta"><span class="muted">${from(x)}</span></div></div><div class="lesson-actions"><button class="btn xs ghost" data-del="${esc(x.id)}" title="Forget, so it may be proposed again">${icon("trash")}</button></div></li>`).join("")}</ul></div></details>` : ""}`;
    bind();
  }

  const act = async (btn, fn, ok) => {
    btn.disabled = true;
    try { apply(await fn()); if (ok) toast("success", ok); } catch (e) { btn.disabled = false; toast("error", "Lessons", e.message); }
  };
  function bind() {
    $("#lAdd", page).onclick = addDialog;
    $("#lFilter", page) && ($("#lFilter", page).onchange = (e) => { filter = e.target.value; draw(); });
    // Show the whole lesson while it is reviewed, at any width.
    const fit = (ta) => { ta.style.height = "auto"; ta.style.height = `${ta.scrollHeight + 2}px`; };
    $$("[data-draft]", page).forEach((ta) => { fit(ta); ta.addEventListener("input", () => { drafts.set(ta.dataset.draft, ta.value); fit(ta); }); });
    $$("[data-approve]", page).forEach((b) => (b.onclick = () => {
      const id = b.dataset.approve;
      const text = $(`[data-draft="${CSS.escape(id)}"]`, page).value;
      const scope = $(`[data-scope="${CSS.escape(id)}"]`, page).value;
      act(b, () => api.approveLesson(id, { text, scope }), "Lesson approved").then(() => drafts.delete(id));
    }));
    $$("[data-reject]", page).forEach((b) => (b.onclick = () => act(b, () => api.rejectLesson(b.dataset.reject))));
    $$("[data-toggle]", page).forEach((s) => {
      const flip = () => act(s, () => api.updateLesson(s.dataset.toggle, { enabled: !s.classList.contains("on") }));
      s.onclick = flip;
      s.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } };
    });
    $$("[data-edit]", page).forEach((b) => (b.onclick = () => editDialog((data.approved || []).find((x) => x.id === b.dataset.edit))));
    $$("[data-del]", page).forEach((b) => (b.onclick = async () => {
      if (!(await confirm("Delete this lesson?", "Future tasks will no longer see it. This cannot be undone.", { danger: true, okLabel: "Delete" }))) return;
      act(b, () => api.deleteLesson(b.dataset.del), "Lesson deleted");
    }));
  }

  function lessonForm(x = {}) {
    const repoOpts = data.repos.map((r) => `<option value="${esc(r.key)}" ${x.repo === r.key ? "selected" : ""}>${esc(r.label)}</option>`).join("");
    return `<div class="field"><label for="lfText">Lesson</label><textarea id="lfText" rows="3" maxlength="300" placeholder="One actionable sentence, e.g. Run the migrations before the API tests; they fail on an empty schema.">${esc(x.text || "")}</textarea><div class="help">One sentence an agent can act on. Up to 300 characters.</div></div>
      <div class="grid2"><div class="field"><label for="lfScope">Applies to</label><select id="lfScope"><option value="repo" ${x.scope !== "global" ? "selected" : ""} ${data.repos.length ? "" : "disabled"}>One repository</option><option value="global" ${x.scope === "global" || !data.repos.length ? "selected" : ""}>All repositories</option></select></div>
      <div class="field"><label for="lfRepo">Repository</label><select id="lfRepo" ${data.repos.length ? "" : "disabled"}>${repoOpts || "<option>No repositories yet</option>"}</select></div></div>`;
  }
  function wireForm(m) {
    const sync = () => { $("#lfRepo", m.body).disabled = $("#lfScope", m.body).value === "global" || !data.repos.length; };
    $("#lfScope", m.body).onchange = sync; sync();
    return () => ({ text: $("#lfText", m.body).value.trim(), scope: $("#lfScope", m.body).value, repo: $("#lfRepo", m.body).value });
  }
  function addDialog() {
    const m = modal(`<h2>Add a lesson</h2><p class="hint">Lessons you write are approved straight away.</p>${lessonForm({ scope: "global" })}<div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="lfSave">${icon("plus")}Add</button></div>`);
    const read = wireForm(m);
    $("#lfSave", m.body).onclick = async () => {
      const v = read();
      if (!v.text) return toast("warning", "Write the lesson first");
      try { apply(await api.addLesson(v)); m.close(); toast("success", "Lesson added"); } catch (e) { toast("error", "Could not add", e.message); }
    };
  }
  function editDialog(x) {
    if (!x) return;
    const m = modal(`<h2>Edit lesson</h2>${lessonForm(x)}<div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="lfSave">${icon("save")}Save</button></div>`);
    const read = wireForm(m);
    $("#lfSave", m.body).onclick = async () => {
      const v = read();
      if (!v.text) return toast("warning", "The lesson is empty");
      try { apply(await api.updateLesson(x.id, v)); m.close(); toast("success", "Lesson saved"); } catch (e) { toast("error", "Could not save", e.message); }
    };
  }

  load();
  return { update(reason) { if (reason === "lessons") load(); }, destroy() { alive = false; } };
}
