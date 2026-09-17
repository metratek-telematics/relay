// Review cockpit: one screen to review a delivered change set across repositories. The verdict at the top, then the
// design, the acceptance evidence, every diff, the verification and the follow-ups; the actions merge the pull
// requests in order (orchestrator/mission.py merge_change_set) or turn review comments into a follow-up task.
import { $, $$, esc, icon, md, toast, confirm, modal, fmtDur, fmtCost, timeAgo, basename } from "../ui.js";
import { S, navigate, statusOf, agentLabel, roleAgent, ROLE_LABEL } from "../state.js";
import { api } from "../api.js";
import { prStatus, prCached, PR_STATE } from "../prstatus.js";
import { acceptanceCardHtml, bindAcceptance } from "../acceptance.js";
import { blockedHtml } from "../packet.js";
import { stackCardHtml, bindStackCard } from "./stacks.js";
import { openFollowUp } from "./newtask.js";
import { mountDiffExplorer } from "./changes.js";
import { scoreRing } from "./mission.js";
import { repoChips } from "../live.js";

const SECTIONS = [["summary", "Summary", "docs"], ["design", "Design", "layers"], ["acceptance", "Acceptance", "checkCircle"], ["changes", "Changes", "branch"], ["verification", "Verification", "shield"], ["followups", "Follow-ups", "flag"]];

export function mountReview(main, id) {
  const getTask = () => S.tasks.get(id);
  let t = getTask();
  if (!t) { main.innerHTML = `<div class="page"><div class="empty-state">${icon("alert", "lg")}<h3>Task not found</h3><p><a class="btn sm" href="#/work">Back to Work</a></p></div></div>`; return { update() {}, destroy() {} }; }
  let alive = true, changes = null, stopStack = () => {};
  const picked = new Set();

  main.innerHTML = `<div class="page rv" id="rv">
    <header class="page-header rv-head">
      <div class="ph-title"><div class="eyebrow"><a href="#/work">Work</a> / <a href="#/task/${esc(id)}">${t.number ? `#${esc(t.number)}` : "Task"}</a> / Review</div>
        <h1 id="rvTitle"></h1><p class="ph-sub" id="rvSub"></p></div>
      <div class="ph-actions" id="rvActions"></div>
    </header>
    <section class="verdict" id="rvVerdict" aria-label="Verdict"></section>
    <div class="rv-layout">
      <nav class="rv-nav" aria-label="Review sections">${SECTIONS.map(([k, l, i]) => `<a href="#rv-${k}" data-sec="${k}">${icon(i, "sm")}<span>${esc(l)}</span><span class="rv-nav-state" data-nav-state="${k}"></span></a>`).join("")}</nav>
      <div class="rv-main">
        ${SECTIONS.map(([k, l]) => `<section class="rv-sec" id="rv-${k}" aria-labelledby="rvh-${k}"><h2 id="rvh-${k}" class="rv-h">${esc(l)}</h2><div class="rv-body" data-body="${k}"></div></section>`).join("")}
      </div>
    </div>
  </div>`;
  const page = $("#rv", main);
  $$(".rv-nav a", page).forEach((a) => (a.onclick = (e) => { e.preventDefault(); $(`#rv-${a.dataset.sec}`, page)?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" }); }));
  const body = (k) => $(`[data-body="${k}"]`, page);

  // ---------------------------------------------------------------- header + verdict
  function prRows() {
    const rows = [{ name: basename(t.repo), primary: true, url: t.pr_url, number: t.pr_number }];
    for (const [name, w] of Object.entries(t.repo_worktrees || {})) rows.push({ name, url: w.pr_url, number: w.pr_number });
    const order = changes?.merge_order || [];
    return rows.filter((r) => r.url || r.number).sort((a, b) => (order.indexOf(a.name) + 1 || 99) - (order.indexOf(b.name) + 1 || 99));
  }
  function drawHead() {
    t = getTask(); if (!t) return;
    const st = statusOf(t);
    $("#rvTitle", page).innerHTML = `${esc(t.name)} <span class="status-chip lg tone-${esc(st.tone || "none")}">${esc(st.label)}</span>`;
    const tot = t.metrics?.total || {};
    $("#rvSub", page).innerHTML = `${repoChips(t, 4)} <span class="muted">delivered ${esc(timeAgo(t.finished_at || t.updated_at))}${t.started_at && t.finished_at ? ` · took ${esc(fmtDur((Date.parse(t.finished_at) - Date.parse(t.started_at)) / 1000))}` : ""} · ${esc(fmtCost(tot.cost_usd, tot.estimated))}</span>`;
    const prs = prRows();
    const pr = prCached(t.id);
    const merged = pr?.ok && pr.state === "merged";
    $("#rvActions", page).innerHTML = `
      <a class="btn" href="#/task/${esc(id)}">${icon("message")}Conversation</a>
      <button type="button" class="btn" id="rvChanges" ${t.status !== "done" ? "disabled" : ""}>${icon("edit")}Request changes</button>
      <button type="button" class="btn primary" id="rvMerge" ${t.status !== "done" || !prs.length || merged ? "disabled" : ""} title="${!prs.length ? "No pull requests: this task delivered a branch only" : merged ? "Already merged" : "Merge every pull request in order"}">${icon("merge")}${merged ? "Merged" : prs.length > 1 ? `Approve and merge ${prs.length} PRs` : "Approve and merge"}</button>`;
    $("#rvChanges", page).onclick = requestChanges;
    $("#rvMerge", page).onclick = mergeFlow;

    const acc = (t.acceptance || []).filter((c) => c.required);
    const proven = acc.filter((c) => c.status === "met" || (c.status === "waived" && c.set_by === "user")).length;
    const v = t.verification, rv = t.review, sc = t.scorecard;
    const cell = (tone, label, value, sub) => `<div class="vd-cell tone-${tone}"><span class="vd-label">${esc(label)}</span><span class="vd-value">${value}</span><span class="vd-sub">${sub}</span></div>`;
    $("#rvVerdict", page).innerHTML = `
      <div class="vd-score">${scoreRing(sc?.score ?? null, 64)}<div><span class="vd-label">Scorecard</span><strong>${esc(sc?.outcome_label || (t.status === "done" ? "Delivered" : st.label))}</strong><span class="vd-sub">${sc ? (sc.success ? "counts as a success" : "not a success") : "scored after the run"}</span></div></div>
      ${cell(acc.length && proven === acc.length ? "ok" : acc.length ? "warn" : "none", "Acceptance", acc.length ? `${proven}<small>/${acc.length}</small>` : "—", acc.length ? "required criteria proven" : "no contract")}
      ${cell(v ? (v.ok ? "ok" : "alarm") : "none", "Verification", v ? (v.ok ? "Passing" : "Failing") : "—", v ? `${(v.items || []).length} check${(v.items || []).length === 1 ? "" : "s"} · ${esc(timeAgo(v.time))}` : "not run")}
      ${cell(rv ? (rv.verdict === "PASS" ? "ok" : "alarm") : "none", "Independent review", rv ? esc(rv.verdict) : "—", rv ? `round ${esc(rv.round)}` : roleAgent(t, "reviewer") ? "not reached" : "no reviewer in this team")}
      ${cell(prs.length ? "info" : "none", "Pull requests", prs.length ? String(prs.length) : "—", prs.length ? prs.map((p) => `${esc(p.name)} #${esc(p.number || "")}`).join(" · ") : "branch only")}`;
    for (const [k, tone] of [["acceptance", acc.length ? (proven === acc.length ? "ok" : "warn") : ""], ["verification", v ? (v.ok ? "ok" : "alarm") : ""], ["followups", (t.follow_ups || []).length ? "info" : ""]]) {
      const s = $(`[data-nav-state="${k}"]`, page); if (s) s.className = `rv-nav-state ${tone ? `dot ${tone}` : ""}`;
    }
  }

  // ---------------------------------------------------------------- sections
  function drawSummary() {
    body("summary").innerHTML = `${t.summary ? `<div class="md rv-summary">${md(t.summary)}</div>` : '<p class="muted">No summary was recorded.</p>'}
      <details class="rv-request"><summary>${icon("message", "sm")}The original request</summary><div class="md">${md(t.requirements || "(from an issue)")}</div></details>
      <dl class="kv rv-team">${["supervisor", "worker", "reviewer"].filter((r) => roleAgent(t, r)).map((r) => `<dt>${esc(ROLE_LABEL[r])}</dt><dd><span class="av xs ${esc(roleAgent(t, r))}"></span> ${esc(agentLabel(roleAgent(t, r)))}</dd>`).join("")}</dl>`;
  }
  function drawDesign() {
    const d = t.design || {}, sys = t.system_design || {};
    const contracts = d.contracts || sys.api_contracts || [];
    const pkgs = t.plan?.work_packages || d.work_packages || [];
    const order = changes?.merge_order || [];
    const goal = d.goal || sys.summary || t.plan?.summary || "";
    const host = body("design");
    if (!goal && !contracts.length && !pkgs.length && order.length < 2) { host.innerHTML = `<p class="muted">This task went straight to implementation, without a separate system design.${t.plan?.plan ? " The agreed plan:" : ""}</p>${t.plan?.plan ? `<details class="rv-request"><summary>${icon("list", "sm")}Plan</summary><div class="md">${md(t.plan.plan)}</div></details>` : ""}`; return; }
    host.innerHTML = `${goal ? `<div class="md">${md(goal)}</div>` : ""}
      ${contracts.length ? `<h3 class="sub-h">Contracts</h3><ul class="contract-list">${contracts.map((c) => `<li><code class="mono">${esc(c.endpoint || c.name || "")}</code><span>${esc(c.consumer || "?")} → ${esc(c.provider || "?")}</span>${c.response ? `<span class="muted">${esc(c.response)}</span>` : ""}</li>`).join("")}</ul>` : ""}
      ${order.length > 1 ? `<h3 class="sub-h">Merge order</h3><ol class="merge-lane">${order.map((n, i) => `<li><span class="ml-step">${i + 1}</span><span>${icon("folder", "sm")}${esc(n)}</span></li>`).join(`<li class="ml-arrow" aria-hidden="true">${icon("arrowRight", "sm")}</li>`)}</ol>` : ""}
      ${pkgs.length ? `<h3 class="sub-h">Work packages</h3><ol class="pkg-list">${pkgs.map((p) => `<li class="${(t.packages_done || []).includes(p.id) || t.status === "done" ? "done" : ""}"><span class="mono">${esc(p.id)}</span><span>${esc(p.summary || "")}</span>${p.repo ? `<span class="repo-chip xs">${esc(p.repo)}</span>` : ""}</li>`).join("")}</ol>` : ""}`;
  }
  function drawAcceptance() {
    const host = body("acceptance");
    const html = acceptanceCardHtml({ ...t, follow_ups: [] }, { editing: bindAcceptance.editing });
    host.innerHTML = html || '<p class="muted">No acceptance contract was recorded for this task.</p>';
    bindAcceptance(host, t, () => { t = getTask(); drawAcceptance(); drawHead(); });
  }
  let explorer = null;
  function drawChanges() {
    if (!explorer) explorer = mountDiffExplorer(body("changes"), getTask, { compact: true });
  }
  function drawVerification() {
    const v = t.verification, rv = t.review;
    const host = body("verification");
    host.innerHTML = `${v ? `<div class="check-list ${v.ok ? "ok" : "fail"}"><div class="cl-head">${icon(v.ok ? "shield" : "alert")}<b>Last verification ${v.ok ? "passed" : "failed"}</b><span class="muted">${esc(timeAgo(v.time))}</span></div>
        <ul>${(v.items || []).map((i) => `<li class="${i.ok ? "ok" : "fail"}"><span class="cl-ic">${icon(i.ok ? "check" : "x", "sm")}</span><code class="mono">${esc(i.command)}</code><span class="muted mono">${i.ok ? "pass" : `exit ${esc(i.rc)}`} · ${esc(fmtDur(i.duration))}</span></li>`).join("")}</ul></div>` : '<p class="muted">No verification ran.</p>'}
      ${(t.blocked_checks || []).length ? `<h3 class="sub-h">Checks that could not run</h3>${blockedHtml(t.blocked_checks)}` : ""}
      ${rv ? `<h3 class="sub-h">Independent review · round ${esc(rv.round)} <span class="badge ${rv.verdict === "PASS" ? "green" : "red"}">${esc(rv.verdict)}</span></h3>${rv.summary ? `<p>${esc(rv.summary)}</p>` : ""}${(rv.findings || []).length ? `<ul class="findings">${rv.findings.map((f) => `<li class="${esc(f.severity || "blocking")}"><code>${esc(f.file || "")}</code> ${esc(f.problem || "")}${f.fix ? `<div class="fix">Fix: ${esc(f.fix)}</div>` : ""}</li>`).join("")}</ul>` : ""}` : ""}
      ${t.stack ? `<h3 class="sub-h">Integration stack and end-to-end checks</h3>${stackCardHtml(t)}` : ""}`;
    stopStack();
    stopStack = t.stack ? bindStackCard(host, t, { isCurrent: () => alive && host.contains($("#stackBody", host)) }) : () => {};
  }
  function drawFollowups() {
    const f = t.follow_ups || [];
    const host = body("followups");
    if (!f.length) { host.innerHTML = '<p class="muted">No follow-ups. Reviewers list non-blocking findings here; pick some to hand to a follow-up task.</p>'; return; }
    host.innerHTML = `<ul class="fu-list">${f.map((x, i) => `<li><label><input type="checkbox" data-fu="${i}" ${picked.has(i) ? "checked" : ""}><span class="badge ${x.severity === "nit" ? "" : "amber"}">${esc(x.severity || "should fix")}</span><span class="fu-text">${x.file ? `<code class="mono">${esc(x.file)}</code> ` : ""}${esc(x.problem || "")}</span></label></li>`).join("")}</ul>
      <div class="row wrap"><button type="button" class="btn sm" id="fuGo" ${picked.size ? "" : "disabled"}>${icon("arrowRight", "sm")}Follow-up task with ${picked.size || "the selected"} item${picked.size === 1 ? "" : "s"}</button></div>`;
    $$("[data-fu]", host).forEach((c) => (c.onchange = () => { c.checked ? picked.add(Number(c.dataset.fu)) : picked.delete(Number(c.dataset.fu)); drawFollowups(); }));
    $("#fuGo", host).onclick = () => openFollowUp_(followUpText([...picked].map((i) => f[i]), ""));
  }

  // ---------------------------------------------------------------- actions
  const followUpText = (items, note) => [note && note.trim(), items.length ? `Address these review findings on ${t.name}:\n${items.map((x) => `- ${x.file ? `${x.file}: ` : ""}${x.problem}${x.fix ? ` (suggested fix: ${x.fix})` : ""}`).join("\n")}` : ""].filter(Boolean).join("\n\n");
  function openFollowUp_(text) { openFollowUpWith(t, text); }

  function requestChanges() {
    const f = t.follow_ups || [];
    const m = modal(`<h2>Request changes</h2><p class="hint">Your comments become a follow-up task on the same branch, with the same team. The pull requests stay open.</p>
      <div class="field"><label for="rcNote">What should change?</label><textarea id="rcNote" rows="6" placeholder="For example: the /health endpoint must not expose the item count; return only status and version."></textarea></div>
      ${f.length ? `<div class="field"><div class="field-label">Include review findings</div><ul class="fu-list compact">${f.map((x, i) => `<li><label><input type="checkbox" data-rc="${i}" ${picked.has(i) ? "checked" : ""}><span class="fu-text">${x.file ? `<code class="mono">${esc(x.file)}</code> ` : ""}${esc(x.problem || "")}</span></label></li>`).join("")}</ul></div>` : ""}
      <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" id="rcGo">${icon("arrowRight")}Continue to the follow-up</button></div>`);
    $("#rcGo", m.body).onclick = () => {
      const note = $("#rcNote", m.body).value;
      const items = $$("[data-rc]:checked", m.body).map((c) => f[Number(c.dataset.rc)]);
      if (!note.trim() && !items.length) { toast("warning", "Say what should change", "Write a comment or pick a finding."); $("#rcNote", m.body).focus(); return; }
      m.close();
      openFollowUpWith(t, followUpText(items, note));
    };
  }

  async function mergeFlow() {
    let plan;
    try { plan = await api.mergeChangeSet(id, { dry_run: true }); } catch (e) { toast("error", "Cannot merge", e.message); return; }
    const m = modal(`<h2>${icon("merge")} Approve and merge</h2>
      <p class="hint">Relay merges each pull request with the GitHub CLI, in this order, and stops at the first one that fails. Draft pull requests are marked ready first.</p>
      <ol class="merge-plan" id="mpList">${plan.plan.map((p, i) => `<li data-step="${i}"><span class="mp-n">${i + 1}</span><span class="mp-main"><b>${esc(p.name)}</b><a class="muted" href="${esc(p.url || "#")}" target="_blank" rel="noopener">${esc(p.repo)} #${esc(p.number)}</a></span><span class="mp-state" data-mp="${i}">waiting</span></li>`).join("")}</ol>
      <div class="field inline"><label for="mpMethod">Merge method</label><select id="mpMethod"><option value="squash">Squash and merge</option><option value="merge">Merge commit</option><option value="rebase">Rebase and merge</option></select></div>
      <div id="mpResult"></div>
      <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" id="mpGo">${icon("merge")}Merge ${plan.plan.length} pull request${plan.plan.length === 1 ? "" : "s"}</button></div>`);
    $("#mpGo", m.body).onclick = async () => {
      const btn = $("#mpGo", m.body); btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Merging…`;
      $$("[data-mp]", m.body).forEach((s, i) => { if (i === 0) s.innerHTML = `${icon("spinner", "spin sm")}merging`; });
      try {
        const r = await api.mergeChangeSet(id, { method: $("#mpMethod", m.body).value });
        plan.plan.forEach((p, i) => {
          const res = r.results[i], s = $(`[data-mp="${i}"]`, m.body);
          s.className = `mp-state ${res ? (res.ok ? "ok" : "fail") : ""}`;
          s.innerHTML = res ? (res.ok ? `${icon("check", "sm")}${esc(res.skipped || "merged")}` : `${icon("x", "sm")}failed`) : "not attempted";
        });
        const failed = r.results.find((x) => !x.ok);
        $("#mpResult", m.body).innerHTML = failed ? `<div class="modal-error">${esc(failed.name)}: ${esc(failed.error || "failed")}. Later pull requests were not merged.</div>` : `<div class="note ok">${icon("checkCircle", "sm")}All pull requests merged in order.</div>`;
        btn.innerHTML = "Close"; btn.disabled = false; btn.onclick = m.close;
        prStatus(id, true).then(() => alive && drawHead());
      } catch (e) {
        $("#mpResult", m.body).innerHTML = `<div class="modal-error">${esc(e.message)}</div>`;
        btn.disabled = false; btn.innerHTML = `${icon("merge")}Try again`;
      }
    };
  }

  async function loadChanges() {
    try { changes = await api.changes(id); } catch { changes = null; }
    if (alive) { drawHead(); drawDesign(); }
  }

  drawHead(); drawSummary(); drawDesign(); drawAcceptance(); drawChanges(); drawVerification(); drawFollowups();
  loadChanges();
  if (t.pr_url || t.pr_number) prStatus(id).then(() => alive && drawHead());
  return {
    update(reason, info) {
      if (reason !== "task" || (info?.id && info.id !== id)) return;
      t = getTask(); if (!t) return;
      drawHead(); drawSummary(); drawFollowups();
      if (!document.activeElement?.closest?.("#acceptanceCard")) drawAcceptance();
    },
    destroy() { alive = false; stopStack(); },
  };
}

// A follow-up with the request already written: the same dialog as "Follow up", opened on the Request step.
export function openFollowUpWith(t, text) {
  import("./newtask.js").then(({ openNewTask }) => openNewTask({ followUp: t, requirements: text }));
}
