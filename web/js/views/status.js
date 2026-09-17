// Shareable status: a read-only, live view of one task for stakeholders. No navigation, no controls that change
// anything; it sits behind the same sign-in as the rest of Relay.
import { $, esc, icon, fmtSec, fmtCost, timeAgo, copyText, md } from "../ui.js";
import { S, statusOf, LIVE, agentLabel, roleAgent, ROLE_LABEL, taskElapsed } from "../state.js";
import { api } from "../api.js";
import { activityHtml, noteMessage, stepperHtml, phases, repoChips } from "../live.js";

export function mountStatus(main, id) {
  const getTask = () => S.tasks.get(id);
  main.innerHTML = `<div class="status-page" id="sp"></div>`;
  const page = $("#sp", main);
  let updated = Date.now();

  function draw() {
    const t = getTask();
    if (!t) { page.innerHTML = `<div class="sp-card"><div class="empty-state">${icon("alert", "lg")}<h3>This task is not available</h3><p>It may have been deleted.</p></div></div>`; return; }
    const st = statusOf(t), live = LIVE.has(t.status);
    const ph = phases(t), pk = ph.packages;
    const acc = (t.acceptance || []).filter((c) => c.required);
    const proven = acc.filter((c) => c.status === "met" || (c.status === "waived" && c.set_by === "user")).length;
    const prs = [{ name: t.repo?.split(/[\\/]/).pop(), url: t.pr_url, n: t.pr_number }, ...Object.entries(t.repo_worktrees || {}).map(([name, w]) => ({ name, url: w.pr_url, n: w.pr_number }))].filter((p) => p.url);
    const pct = t.status === "done" ? 100 : Math.round((ph.steps.filter((s) => s.state === "done").length / ph.steps.length) * 100);
    const events = [...(t.events || [])].slice(-8).reverse();
    page.innerHTML = `
      <header class="sp-top">
        <span class="sp-brand"><svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9"/><path d="M10 22.5V9.5h7.2a4.2 4.2 0 0 1 0 8.4H13.4l6.6 4.6"/><circle cx="23.4" cy="9.6" r="2.1"/></svg><b>Relay</b><span class="muted">status</span></span>
        <span class="sp-live">${live ? '<span class="live-dot sm"></span>Live' : "Read-only"} · updated <span data-updated>${esc(new Date(updated).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</span></span>
        <button type="button" class="btn xs" id="spCopy">${icon("link", "sm")}Copy link</button>
      </header>
      <main class="sp-card">
        <div class="sp-eyebrow">${repoChips(t, 4)}${t.number ? `<span class="mono muted">#${esc(t.number)}</span>` : ""}</div>
        <h1 class="sp-title">${esc(t.name)}</h1>
        <div class="sp-status"><span class="status-chip xl tone-${esc(st.tone || "none")}">${live ? '<span class="live-dot"></span>' : ""}${esc(st.label)}</span><span class="muted">${esc(t.detail || "")}</span></div>
        <div class="sp-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="Overall progress"><i style="width:${pct}%"></i></div>
        ${stepperHtml(t, { size: "lg" })}
        ${live ? `<div class="sp-now"><span class="eyebrow">Right now</span>${activityHtml(t)}</div>` : t.status === "done" && t.summary ? `<div class="sp-now"><span class="eyebrow">Delivered</span><div class="md">${md(t.summary)}</div></div>` : t.pending ? `<div class="sp-now attn"><span class="eyebrow">Waiting</span><p>The team is waiting for a decision from its owner.</p></div>` : ""}
        <dl class="sp-facts">
          <div><dt>Team</dt><dd>${["supervisor", "worker", "reviewer"].filter((r) => roleAgent(t, r)).map((r) => `<span class="sp-member"><span class="av xs ${esc(roleAgent(t, r))}"></span>${esc(agentLabel(roleAgent(t, r)))} <span class="muted">${esc(ROLE_LABEL[r].toLowerCase())}</span></span>`).join("")}</dd></div>
          <div><dt>Time</dt><dd class="mono" data-elapsed>${fmtSec(taskElapsed(t))}</dd></div>
          <div><dt>Work packages</dt><dd>${pk.total ? `${pk.done} of ${pk.total}` : "—"}</dd></div>
          <div><dt>Acceptance</dt><dd>${acc.length ? `${proven} of ${acc.length} proven` : "—"}</dd></div>
          <div><dt>Checks</dt><dd>${t.verification ? `<span class="tone-${t.verification.ok ? "ok" : "alarm"}">${t.verification.ok ? "passing" : "failing"}</span>` : "not run yet"}</dd></div>
          <div><dt>Pull requests</dt><dd>${prs.length ? prs.map((p) => `<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.name)} #${esc(p.n || "")}</a>`).join(" · ") : "—"}</dd></div>
        </dl>
        ${acc.length ? `<h2 class="sp-h">Acceptance criteria</h2><ul class="sp-acc">${(t.acceptance || []).map((c) => `<li class="${c.status === "met" ? "ok" : ""}"><span class="sp-check" aria-hidden="true">${c.status === "met" ? icon("check", "sm") : ""}</span><span>${esc(c.criterion)}</span><span class="sr-only">${c.status === "met" ? "met" : "not yet met"}</span></li>`).join("")}</ul>` : ""}
        ${events.length ? `<h2 class="sp-h">Recent milestones</h2><ol class="sp-events">${events.map((e) => `<li><time>${esc(timeAgo(e.time))}</time><span>${esc(e.title)}</span></li>`).join("")}</ol>` : ""}
      </main>
      <footer class="sp-foot muted">Shared from Relay · this page updates by itself · ${esc(fmtCost(t.metrics?.total?.cost_usd, t.metrics?.total?.estimated))} spent</footer>`;
    $("#spCopy", page).onclick = () => copyText(location.href);
    document.title = `${st.label} · ${t.name} · Relay`;
  }

  (async () => { try { const r = await api.messages(id, 0, 40); for (const m of r.messages || []) noteMessage({ ...m, task_id: id }); draw(); } catch {} })();
  draw();
  const timer = setInterval(() => { const t = getTask(); const e = $("[data-elapsed]", page); if (t && e && LIVE.has(t.status)) e.textContent = fmtSec(taskElapsed(t)); }, 1000);
  let pending = null;
  const soon = () => { if (pending) return; pending = setTimeout(() => { pending = null; updated = Date.now(); draw(); }, 400); };
  return {
    update(reason, info) {
      if (reason === "task" && info?.id && info.id !== id) return;
      if ((reason === "activity" || reason === "process" || reason === "message" || reason === "message_update") && (info?.task_id || id) !== id) return;
      soon();
    },
    destroy() { clearInterval(timer); clearTimeout(pending); document.title = "Relay"; },
  };
}
