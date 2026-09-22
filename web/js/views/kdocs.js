// Knowledge → Docs: the markdown docs agents read before planning (platform docs and one per repository), with
// search, staleness against GitHub, "Refresh now", and an editor for owners and admins (saves are audited).
import { $, $$, esc, icon, md, toast, timeAgo, skeleton, debounce, copyText, confirm } from "../ui.js";
import { api } from "../api.js";

const LAST = "relay.knowledge.doc";

function stripFront(text) {
  const m = String(text || "").match(/^---\n[\s\S]*?\n---\n?/);
  return m ? text.slice(m[0].length) : text;
}
// Relative links between docs ([EDGE](../EDGE.md)) open inside the page; md() only links http(s) URLs.
function docLinks(html, cur) {
  const dir = cur.includes("/") ? cur.split("/")[0] + "/" : "";
  return html.replace(/\[([^\]<>\n]+)\]\(((?:\.\.\/)?(?:repos\/)?[\w.-]+\.md)(#[\w-]*)?\)/g, (all, label, href) => {
    let target = href.startsWith("../") ? href.slice(3) : (href.startsWith("repos/") ? href : dir + href);
    return `<a href="#" data-doc="${esc(target)}">${label}</a>`;
  });
}
function staleBadge(d) {
  const s = d.stale || {};
  if (d.kind !== "repo") return "";
  if (s.error) return `<span class="badge red" title="${esc(s.error)}">check failed</span>`;
  if (s.stale) return `<span class="badge amber" title="${esc((s.reasons || []).join(" · "))}">stale${s.behind ? ` · ${esc(s.behind)} behind` : ""}</span>`;
  if ((s.delivery_marks || []).length) return `<span class="badge amber" title="A Relay task delivered changes to key files">may be stale</span>`;
  if (s.untracked || !d.source_commit) return `<span class="badge outline" title="No source commit recorded">untracked</span>`;
  if (s.checked_at) return `<span class="badge green" title="Checked ${esc(timeAgo(s.checked_at))}">current</span>`;
  return "";
}

export function mountKdocs(host) {
  host.innerHTML = `<div class="kd" id="kd">${skeleton("page", 3)}</div>`;
  const root = $("#kd", host);
  let data = null, alive = true, cur = null, doc = null, editing = false, q = "", results = null, draft = "";
  try { cur = localStorage.getItem(LAST); } catch {}

  async function load() {
    try { data = await api.knowledgeDocs(); } catch (e) { if (alive) root.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (!alive) return;
    if (!cur || !data.docs.some((d) => d.path === cur)) cur = data.docs[0]?.path || null;
    drawShell();
    if (cur) open(cur, false);
  }

  function listItem(d) {
    return `<li><button type="button" class="kd-item ${d.path === cur ? "active" : ""}" data-open="${esc(d.path)}" aria-current="${d.path === cur}">
      <span class="kd-item-top"><strong class="kd-title">${esc(d.kind === "repo" ? d.path.replace(/^repos\//, "").replace(/\.md$/, "") : d.title)}</strong>${staleBadge(d)}</span>
      <span class="kd-sum">${esc(d.summary)}</span></button></li>`;
  }
  function drawList() {
    const box = $("#kdList", root);
    if (!box) return;
    if (results) {
      box.innerHTML = results.length ? `<ul class="kd-items">${results.map((r) => `<li><button type="button" class="kd-item ${r.path === cur ? "active" : ""}" data-open="${esc(r.path)}" data-line="${esc(r.hits[0]?.line || "")}">
        <span class="kd-item-top"><strong class="kd-title">${esc(r.path)}</strong></span>
        ${r.hits.slice(0, 3).map((h) => `<span class="kd-hit"><span class="muted">${esc(h.line)}</span> ${esc(h.text)}</span>`).join("")}</button></li>`).join("")}</ul>`
        : `<div class="empty small">No doc contains every word of “${esc(q)}”.</div>`;
    } else {
      const plat = data.docs.filter((d) => d.kind === "platform"), repos = data.docs.filter((d) => d.kind === "repo");
      box.innerHTML = `${plat.length ? `<div class="kd-group">Platform</div><ul class="kd-items">${plat.map(listItem).join("")}</ul>` : ""}
        ${repos.length ? `<div class="kd-group">Repositories</div><ul class="kd-items">${repos.map(listItem).join("")}</ul>` : ""}
        ${!data.docs.length ? `<div class="empty small">No docs yet. Put markdown files in <code>${esc(data.root)}</code> (repository docs in <code>repos/</code>).</div>` : ""}`;
    }
    $$("[data-open]", box).forEach((b) => (b.onclick = () => open(b.dataset.open, true, b.dataset.line)));
  }
  function drawShell() {
    const last = data.last_run;
    root.innerHTML = `
      <div class="kd-head">
        <p class="muted kd-intro">Agents get these paths with a one-line summary in every task prompt and read the ones that apply before planning. Repository docs record the GitHub commit they were written from; a daily check refreshes the stale ones with one cheap agent turn, keeping sections a person edited.</p>
        <div class="row wrap kd-actions">
          <span class="muted kd-last">${last ? `Last check ${esc(timeAgo(last.time))}` : "Not checked yet"}${data.busy.length ? ` · refreshing ${esc(data.busy.join(", "))}` : ""}</span>
          ${data.can_edit ? `<button class="btn sm" id="kdCheck" title="Check every repository doc against GitHub and refresh the stale ones">${icon("refresh")}Refresh now</button>` : ""}
        </div>
      </div>
      <div class="kd-split">
        <aside class="kd-side card" aria-label="Knowledge docs">
          <div class="kd-search"><input class="input" id="kdQ" type="search" placeholder="Search the docs" aria-label="Search the docs" value="${esc(q)}"></div>
          <div id="kdList" class="kd-list"></div>
        </aside>
        <section class="kd-main card" id="kdMain" aria-live="polite"></section>
      </div>`;
    drawList();
    const run = debounce(async () => {
      q = $("#kdQ", root).value.trim();
      if (!q) { results = null; drawList(); return; }
      try { results = (await api.knowledgeSearch(q)).results; } catch (e) { toast("error", "Search", e.message); results = []; }
      if (alive) drawList();
    }, 250);
    $("#kdQ", root).addEventListener("input", run);
    $("#kdCheck", root) && ($("#kdCheck", root).onclick = (e) => refresh(e.currentTarget, "", false));
  }

  async function refresh(btn, repo, force) {
    btn.disabled = true;
    try { await api.knowledgeRefresh({ repo, force }); toast("info", repo ? `Refreshing ${repo}` : "Checking every repository doc", "One cheap agent turn per stale doc; this page updates when it finishes."); }
    catch (e) { toast("error", "Refresh", e.message); btn.disabled = false; }
  }

  async function open(path, remember = true, line = "") {
    if (editing && draft !== (doc?.text || "") && !(await confirm("Discard your edits?", "You have unsaved changes to this doc.", { danger: true, okLabel: "Discard" }))) return;
    editing = false;
    cur = path;
    if (remember) { try { localStorage.setItem(LAST, path); } catch {} }
    $$(".kd-item", root).forEach((b) => { const on = b.dataset.open === path; b.classList.toggle("active", on); b.setAttribute("aria-current", String(on)); });
    const main = $("#kdMain", root);
    main.innerHTML = skeleton("list", 6);
    try { doc = await api.knowledgeDoc(path); } catch (e) { main.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (!alive || cur !== path) return;
    drawDoc();
    if (line) {
      const words = q.toLowerCase().split(/\s+/).filter(Boolean);
      const hit = $$(".kd-body p, .kd-body li, .kd-body td, .kd-body h3, .kd-body h4", main).find((n) => words.every((w) => n.textContent.toLowerCase().includes(w)));
      hit?.scrollIntoView({ block: "center" });
      hit?.classList.add("kd-flash");
    }
  }

  function drawDoc() {
    const main = $("#kdMain", root);
    const d = doc, s = d.stale || {};
    const lr = s.last_refresh;
    const meta = [
      d.repo ? `<span class="badge outline" title="GitHub repository">${icon("github", "sm")}${esc(d.repo)}</span>` : "",
      d.source_commit ? `<span class="badge outline" title="Written from this commit${(d.inferred || []).includes("source_commit") ? " (read from the text)" : ""}">${icon("branch", "sm")}${esc(d.source_commit.slice(0, 7))}</span>` : "",
      d.updated ? `<span class="muted">updated ${esc(d.updated)}</span>` : `<span class="muted">modified ${esc(timeAgo(d.modified))}</span>`,
      staleBadge(d),
      (d.human_sections || []).length ? `<span class="badge purple" title="Sections a person edited; the refresh keeps them">${esc(d.human_sections.length)} kept by hand</span>` : "",
    ].filter(Boolean).join("");
    const notes = [];
    if ((s.reasons || []).length && (s.stale || s.untracked)) notes.push(esc(s.reasons.join(" · ")));
    (s.delivery_marks || []).forEach((m) => notes.push(`Task <a href="#/task/${esc(m.task_id)}">${esc(m.task_id)}</a> delivered changes to ${esc(m.key_files.slice(0, 4).join(", "))}${m.pr_url ? ` (<a href="${esc(m.pr_url)}" target="_blank" rel="noopener">pull request</a>)` : ""}`));
    if (lr) notes.push(`Last refresh ${esc(timeAgo(lr.time))}: ${lr.sections_changed?.length ? `rewrote ${esc(lr.sections_changed.join(", "))}` : "no section needed changes"}${lr.playbook_changed?.length ? `; playbook ${esc(lr.playbook_changed.join(", "))}` : ""}${lr.sections_kept?.length ? `; kept ${esc(lr.sections_kept.join(", "))}` : ""} · ${esc(lr.agent)}${lr.model ? ` ${esc(lr.model)}` : ""}`);
    if (s.error) notes.push(`<span class="kd-err">${esc(s.error)}</span>`);
    main.innerHTML = `
      <div class="kd-doc-head">
        <div class="kd-doc-title"><h2>${esc(d.title)}</h2><div class="row wrap kd-meta">${meta}</div></div>
        <div class="row wrap kd-doc-actions">
          <button class="btn sm ghost" id="kdCopy" title="Copy the path agents use">${icon("copy")}Path</button>
          ${data.can_edit && d.kind === "repo" && d.repo ? `<button class="btn sm" id="kdRefresh" title="Check this repository on GitHub and refresh the doc now, even if it looks current">${icon("sparkles")}Refresh</button>` : ""}
          ${data.can_edit ? (editing ? `<button class="btn sm" id="kdCancel">Cancel</button><button class="btn sm primary" id="kdSave">${icon("check")}Save</button>` : `<button class="btn sm" id="kdEdit">${icon("edit")}Edit</button>`) : ""}
        </div>
      </div>
      ${notes.length ? `<div class="kd-notes">${notes.map((n) => `<div>${n}</div>`).join("")}</div>` : ""}
      ${editing
        ? `<label class="kd-edit-label muted" for="kdText">Markdown. Sections you change are recorded as kept by hand, so the automatic refresh never rewrites them. The save is recorded in the audit log.</label><textarea class="input kd-text" id="kdText" spellcheck="false">${esc(draft)}</textarea>`
        : `<article class="md kd-body">${docLinks(md(stripFront(d.text)), d.path)}</article>`}`;
    $("#kdCopy", main).onclick = () => { copyText(d.abs); toast("success", "Path copied", d.abs); };
    $("#kdRefresh", main) && ($("#kdRefresh", main).onclick = (e) => refresh(e.currentTarget, d.repo, true));
    $("#kdEdit", main) && ($("#kdEdit", main).onclick = () => { editing = true; draft = d.text; drawDoc(); $("#kdText", main).focus(); });
    $("#kdCancel", main) && ($("#kdCancel", main).onclick = () => { editing = false; drawDoc(); });
    $("#kdText", main) && $("#kdText", main).addEventListener("input", (e) => { draft = e.target.value; });
    $("#kdSave", main) && ($("#kdSave", main).onclick = async (e) => {
      e.currentTarget.disabled = true;
      try {
        doc = await api.knowledgeSave({ path: d.path, text: draft, base_sha: d.sha });
        editing = false;
        toast("success", "Doc saved", d.path);
        const i = data.docs.findIndex((x) => x.path === d.path);
        if (i >= 0) data.docs[i] = { ...data.docs[i], ...doc, text: undefined };
        drawList(); drawDoc();
      } catch (err) { e.currentTarget.disabled = false; toast("error", "Save failed", err.message); }
    });
    $$("[data-doc]", main).forEach((a) => (a.onclick = (ev) => { ev.preventDefault(); if (data.docs.some((x) => x.path === a.dataset.doc)) open(a.dataset.doc); else toast("info", "Not a knowledge doc", a.dataset.doc); }));
  }

  load();
  return {
    update(reason) {
      if (reason !== "knowledge" || editing) return;
      api.knowledgeDocs().then((d) => { if (!alive) return; data = d; drawShell(); if (cur) open(cur, false); }).catch(() => {});
    },
    destroy() { alive = false; },
  };
}
