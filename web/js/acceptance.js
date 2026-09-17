// The acceptance contract as a checklist card: status and evidence per criterion, follow-ups, and a small editor.
import { $, $$, esc, icon, toast, prompt, confirm } from "./ui.js";
import { api } from "./api.js";

const STATUS = { met: ["green", "met"], waived: ["", "waived"], unmet: ["amber", "unmet"] };

export function acceptanceCardHtml(t, { editing = false } = {}) {
  const rows = t.acceptance || [];
  const follow = t.follow_ups || [];
  if (!rows.length && !follow.length) return "";
  const required = rows.filter((c) => c.required);
  const proven = required.filter((c) => c.status === "met" || (c.status === "waived" && c.set_by === "user")).length;
  const items = rows.map((c) => {
    const [tone, label] = STATUS[c.status] || STATUS.unmet;
    return `<li class="ac-row ${esc(c.status || "unmet")}" data-id="${esc(c.id)}">
      <i class="ac-box">${c.status === "met" ? icon("check") : c.status === "waived" ? icon("x") : ""}</i>
      <div class="ac-main">
        <div class="ac-line"><b class="mono">${esc(c.id)}</b> <span>${esc(c.criterion)}</span></div>
        <div class="ac-meta muted">${c.required ? "required" : "optional"} · ${esc(c.how_to_verify || "inspection")}${c.set_by === "user" ? " · set by you" : c.set_by === "relay" ? " · set by Relay's checks" : ""}</div>
        ${c.evidence ? `<div class="ac-evidence">${esc(c.evidence)}</div>` : ""}
        ${editing ? `<div class="row wrap ac-edit">
          <button class="btn xs" data-ac="${c.status === "waived" ? "unwaive" : "waive"}">${c.status === "waived" ? "Restore" : "Waive"}</button>
          <button class="btn xs" data-ac="required">${c.required ? "Make optional" : "Make required"}</button>
          <button class="btn xs" data-ac="text">${icon("edit", "sm")}Edit</button>
          <button class="btn xs danger" data-ac="remove">${icon("trash", "sm")}Remove</button></div>` : ""}
      </div>
      <span class="badge ${tone}">${label}</span></li>`;
  }).join("");
  const followHtml = follow.length ? `<h4 class="ac-sub">Follow-ups <span class="muted pk-note">not blocking · listed in the pull request</span></h4>
    <ul class="pk-list">${follow.map((f) => `<li><span class="badge ${f.severity === "nit" ? "" : "amber"}">${esc(f.severity || "should_fix")}</span> ${f.file ? `<code>${esc(f.file)}</code> ` : ""}${esc(f.problem)}</li>`).join("")}</ul>` : "";
  return `<div class="card" id="acceptanceCard"><div class="card-head"><h3>Acceptance</h3>
      <span class="row" style="gap:8px">${required.length ? `<span class="badge ${proven === required.length ? "green" : "amber"}">${proven}/${required.length} required proven</span>` : ""}
      <button class="btn xs ${editing ? "" : "ghost"}" data-ac-toggle>${icon("edit", "sm")}${editing ? "Done" : "Edit"}</button></span></div>
    <div class="card-body">${rows.length ? `<ul class="ac-list">${items}</ul>` : '<p class="muted">No acceptance contract (legacy task).</p>'}
      ${editing ? `<div class="row ac-add"><input data-ac-new placeholder="Add a criterion, e.g. parse_port rejects 0 with ValueError"><button class="btn xs" data-ac-add>${icon("plus", "sm")}Add</button></div>` : ""}
      ${followHtml}</div></div>`;
}

export function bindAcceptance(host, t, rerender) {
  const card = $("#acceptanceCard", host);
  if (!card) return;
  const send = async (ops) => {
    try { await api.editAcceptance(t.id, ops); rerender(); } catch (e) { toast("error", "Could not update acceptance", e.message || String(e)); }
  };
  $("[data-ac-toggle]", card).onclick = () => { bindAcceptance.editing = !bindAcceptance.editing; rerender(); };
  $$("[data-ac]", card).forEach((b) => (b.onclick = async () => {
    const id = b.closest("[data-id]").dataset.id;
    const c = (t.acceptance || []).find((x) => x.id === id) || {};
    const act = b.dataset.ac;
    if (act === "waive") return send({ update: [{ id, status: "waived" }] });
    if (act === "unwaive") return send({ update: [{ id, status: "unmet" }] });
    if (act === "required") return send({ update: [{ id, required: !c.required }] });
    if (act === "remove") { if (await confirm(`Remove ${id}?`, "The supervisor is told about the change at its next turn.")) send({ remove: [id] }); return; }
    if (act === "text") {
      const v = await prompt(`Edit ${id}`, "Keep it checkable.", { value: c.criterion || "" });
      if (v && v.trim()) send({ update: [{ id, criterion: v.trim() }] });
    }
  }));
  const add = $("[data-ac-add]", card);
  if (add) {
    const input = $("[data-ac-new]", card);
    const go = () => { const v = input.value.trim(); if (v) send({ add: [{ criterion: v, required: true }] }); };
    add.onclick = go;
    input.onkeydown = (e) => { if (e.key === "Enter") go(); };
  }
}
bindAcceptance.editing = false;
