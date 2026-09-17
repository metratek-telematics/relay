// Changes: everything a task changed, across all its repositories, in one place. A file navigator grouped by
// repository (in merge order) beside the diff, plus the commit history, "Try it" commands and the file editor.
import { $, $$, esc, icon, toast, diffHtml, copyText, basename } from "../ui.js";
import { api } from "../api.js";
import { mountInspector } from "./inspector.js";

const SUBS = [["diff", "Diff", "branch"], ["commits", "Commits", "clock"], ["try", "Try it", "play"], ["edit", "Edit files", "code"]];
const INSPECTOR_TAB = { commits: "history", try: "result", edit: "repository" };

// The diff explorer on its own, used by the Changes view and the review cockpit.
export function mountDiffExplorer(host, getTask, { compact = false } = {}) {
  const st = { data: null, sel: null, whole: false, error: null, loading: false };
  host.innerHTML = `<div class="dx ${compact ? "compact" : ""}">
    <nav class="dx-files" aria-label="Changed files"><div class="skel-list">${'<span class="skel"></span>'.repeat(6)}</div></nav>
    <section class="dx-view" aria-live="polite"><div class="dx-empty">${icon("branch", "lg")}<p>Pick a file to see its diff.</p></div></section>
  </div>`;
  const nav = $(".dx-files", host), view = $(".dx-view", host);
  const flat = () => (st.data?.repos || []).flatMap((r) => (r.files || []).map((f) => ({ repo: r.name, path: f.path, status: f.status })));

  async function load(force) {
    const t = getTask(); if (!t) return;
    if (st.loading) return;
    st.loading = true;
    try { st.data = await api.changes(t.id); st.error = null; }
    catch (e) { st.error = e.message; }
    finally { st.loading = false; }
    drawNav();
    if (!st.sel) { const first = flat()[0]; if (first) select(first.repo, first.path); else drawEmpty(); }
    else if (force) select(st.sel.repo, st.sel.path);
  }
  function drawEmpty() {
    const t = getTask();
    view.innerHTML = `<div class="dx-empty">${icon("branch", "lg")}<h3>${st.error ? "Could not read the changes" : (st.data?.repos || []).some((r) => r.exists) ? "No changes yet" : "No worktree yet"}</h3><p>${esc(st.error || ((st.data?.repos || []).some((r) => r.exists) ? "Files appear here as soon as an agent edits them." : t?.status === "done" ? "The worktree was removed after delivery; the pull request still has the diff." : "Relay creates an isolated worktree when the task starts."))}</p></div>`;
  }
  function drawNav() {
    if (st.error) { nav.innerHTML = `<div class="dx-err">${icon("alert", "sm")}${esc(st.error)}<button class="btn xs" type="button" data-reload>${icon("refresh", "sm")}Retry</button></div>`; $("[data-reload]", nav).onclick = () => load(true); return; }
    const repos = st.data?.repos || [];
    const multi = repos.length > 1;
    nav.innerHTML = `<div class="dx-nav-head"><span>${repos.reduce((a, r) => a + (r.files || []).length, 0)} files${multi ? ` in ${repos.length} repositories` : ""}</span><button class="btn xs ghost icon" type="button" data-reload title="Read the changes again" aria-label="Refresh">${icon("refresh", "sm")}</button></div>` + repos.map((r, i) => `
      <div class="dx-repo">
        <div class="dx-repo-head">${multi ? `<span class="dx-order" title="Merge order">${i + 1}</span>` : ""}<span class="dx-repo-name">${icon("folder", "sm")}${esc(r.name)}</span>${r.primary && multi ? '<span class="badge outline">primary</span>' : ""}
          <span class="diffstat">${r.stat?.files != null ? `<span class="a">+${r.stat.insertions || 0}</span><span class="d">−${r.stat.deletions || 0}</span>` : ""}</span></div>
        <div class="dx-repo-sub"><code class="mono">${esc(r.branch || "")}</code>${r.pr_url ? `<a href="${esc(r.pr_url)}" target="_blank" rel="noopener">${icon("github", "sm")}PR #${esc(r.pr_number || "")}</a>` : ""}<button class="btn xs ghost" type="button" data-whole="${esc(r.name)}" title="Whole diff of this repository">${icon("eye", "sm")}All</button></div>
        ${!r.exists ? '<div class="dx-note">Worktree not on disk</div>' : (r.files || []).length ? (r.files || []).map((f) => `<button type="button" class="dx-file ${st.sel && st.sel.repo === r.name && st.sel.path === f.path ? "active" : ""}" data-repo="${esc(r.name)}" data-path="${esc(f.path)}" title="${esc(f.path)}"><span class="fst ${esc(f.status[0] === "?" ? "A" : f.status[0])}">${esc(f.status[0] === "?" ? "A" : f.status[0])}</span><span class="dx-path"><span class="dx-dir">${esc(f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "")}</span>${esc(basename(f.path))}</span></button>`).join("") : '<div class="dx-note">No changes</div>'}
      </div>`).join("");
    $$("[data-path]", nav).forEach((b) => (b.onclick = () => select(b.dataset.repo, b.dataset.path)));
    $$("[data-whole]", nav).forEach((b) => (b.onclick = () => select(b.dataset.whole, "")));
    $("[data-reload]", nav).onclick = () => load(true);
  }
  async function select(repo, path) {
    const t = getTask(); if (!t) return;
    st.sel = { repo, path };
    $$(".dx-file", nav).forEach((b) => b.classList.toggle("active", b.dataset.repo === repo && b.dataset.path === path));
    const list = flat(), i = list.findIndex((x) => x.repo === repo && x.path === path);
    view.innerHTML = `<header class="dx-view-head">
        <span class="dx-crumb">${icon("folder", "sm")}${esc(repo)}<span class="sep">/</span><code class="mono">${esc(path || "all changes")}</code></span>
        <span class="dx-tools">
          <button class="btn xs ghost icon" type="button" data-step="-1" ${i <= 0 ? "disabled" : ""} aria-label="Previous file" title="Previous file (K)">${icon("chevronUp")}</button>
          <button class="btn xs ghost icon" type="button" data-step="1" ${i < 0 || i >= list.length - 1 ? "disabled" : ""} aria-label="Next file" title="Next file (J)">${icon("chevronDown")}</button>
          <button class="btn xs ghost" type="button" data-copy disabled>${icon("copy", "sm")}Copy</button>
        </span>
      </header><div class="dx-diff"><div class="skel-diff">${'<span class="skel"></span>'.repeat(8)}</div></div>`;
    $$("[data-step]", view).forEach((b) => (b.onclick = () => { const n = list[i + Number(b.dataset.step)]; if (n) select(n.repo, n.path); }));
    let r;
    try { r = await api.repoDiff(t.id, repo, path); }
    catch (e) { if (st.sel.repo === repo && st.sel.path === path) $(".dx-diff", view).innerHTML = `<div class="dx-empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (st.sel.repo !== repo || st.sel.path !== path) return;
    $(".dx-diff", view).innerHTML = diffHtml(r.text);
    const copy = $("[data-copy]", view); copy.disabled = !r.text; copy.onclick = () => copyText(r.text || "");
  }
  host.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea")) return;
    if (e.key === "j" || e.key === "k") { const b = $(`[data-step="${e.key === "j" ? 1 : -1}"]`, view); if (b && !b.disabled) { e.preventDefault(); e.stopPropagation(); b.click(); } }
  });
  load();
  return { refresh: () => load(true), get data() { return st.data; } };
}

export function mountChanges(host, getTask, sub = "diff") {
  let cur = SUBS.some(([k]) => k === sub) ? sub : "diff";
  const mounted = {};
  host.innerHTML = `<div class="changes">
    <div class="view-bar"><div class="seg" role="tablist" aria-label="Changes view">${SUBS.map(([k, l, i]) => `<button type="button" role="tab" data-sub="${k}" aria-selected="${k === cur}" class="${k === cur ? "active" : ""}">${icon(i, "sm")}${l}</button>`).join("")}</div>
      <span class="view-bar-note muted small" id="chNote"></span></div>
    ${SUBS.map(([k]) => `<div class="changes-sub" data-host="${k}" ${k === cur ? "" : "hidden"}></div>`).join("")}
  </div>`;
  function show(k, { push = true } = {}) {
    cur = k;
    $$("[data-sub]", host).forEach((b) => { b.classList.toggle("active", b.dataset.sub === k); b.setAttribute("aria-selected", b.dataset.sub === k); });
    $$("[data-host]", host).forEach((h) => (h.hidden = h.dataset.host !== k));
    const h = $(`[data-host="${k}"]`, host);
    if (!mounted[k]) mounted[k] = k === "diff" ? mountDiffExplorer(h, getTask) : mountInspector(h, getTask, { tabs: [INSPECTOR_TAB[k]] });
    const t = getTask();
    if (push && t) history.replaceState(null, "", `#/task/${encodeURIComponent(t.id)}/changes${k === "diff" ? "" : `/${k}`}`);
  }
  $$("[data-sub]", host).forEach((b) => (b.onclick = () => show(b.dataset.sub)));
  show(cur, { push: false });
  return {
    show,
    update(reason) {
      if (reason === "task") { mounted.try?.refresh?.("task"); mounted.commits?.refresh?.("task"); }
    },
    destroy() { for (const m of Object.values(mounted)) m?.destroy?.(); },
  };
}
