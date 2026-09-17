// Autopilot views: the "Needs you" inbox (every pending question, approval and decision, answerable in place)
// and the Digest (what happened while you were away, and what happens next). Server side: orchestrator/autopilot.py.
import { $, $$, esc, icon, toast, md, timeAgo, fmtDur, throttle } from "../ui.js";
import { S, agentLabel, navigate } from "../state.js";
import { api } from "../api.js";

const KIND = {
  question: ["Question", "amber", "question"],
  escalation: ["Decision", "amber", "flag"],
  approval: ["Approval", "purple", "shield"],
  parked: ["Paused", "amber", "pause"],
  blocked: ["Blocked", "red", "alert"],
};
const ROLE = { supervisor: "supervisor", worker: "worker", reviewer: "reviewer", orchestrator: "Relay's judge" };
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
const dayClock = (ts) => {
  if (!ts) return "";
  const d = new Date(ts * 1000), today = new Date();
  return d.toDateString() === today.toDateString() ? clock(ts) : d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
};
const money = (n) => (Number(n) ? `~$${Number(n).toFixed(Number(n) < 1 ? 3 : 2)}` : "$0");
const taskRef = (x) => `<a class="nx-task" href="#/task/${esc(x.task_id)}">${x.number ? `<span class="nx-num">#${esc(x.number)}</span>` : ""}${esc(x.task || x.name || "")}</a>`;

// ---------------------------------------------------------------------------- inbox items
export function inboxItemHtml(x) {
  const [kindLabel, tone, ic] = KIND[x.kind] || KIND.question;
  const who = x.kind === "question" ? `${esc(agentLabel(x.agent))} (${esc(ROLE[x.from] || x.from || "agent")}) asks` : x.kind === "escalation" ? "Relay's judge needs a decision" : x.kind === "approval" ? "Ready to deliver" : "";
  const opts = (x.options || []).map((o) => `<button type="button" class="btn sm nx-opt" data-answer-opt="${esc(o)}">${esc(o)}</button>`).join("");
  let actions = "";
  if (x.kind === "question" || x.kind === "escalation") {
    actions = `${opts ? `<div class="nx-opts">${opts}</div>` : ""}
      <div class="nx-reply"><textarea class="input" rows="1" data-answer-text placeholder="${opts ? "Or write your own answer…" : "Your answer…"}" aria-label="Answer"></textarea><button type="button" class="btn sm primary" data-answer-send>${icon("send")}Send</button></div>
      ${x.auto ? `<div class="nx-auto">${icon("clock", "sm")}<span>If nobody answers, Relay takes: <b>${esc(x.auto)}</b></span></div>` : ""}`;
  } else if (x.kind === "approval") {
    actions = `${x.summary ? `<div class="nx-summary">${md(x.summary.length > 900 ? x.summary.slice(0, 900) + "…" : x.summary)}</div>` : ""}
      ${x.diffstat ? `<pre class="nx-diffstat">${esc(String(x.diffstat).split("\n").slice(-4).join("\n"))}</pre>` : ""}
      <div class="nx-reply"><textarea class="input" rows="1" data-answer-text placeholder="Note (required to request changes)" aria-label="Note"></textarea></div>
      <div class="nx-opts"><button type="button" class="btn sm primary" data-approve>${icon("check")}Approve delivery</button><button type="button" class="btn sm" data-reject>${icon("x")}Request changes</button></div>`;
  } else if (x.kind === "parked") {
    actions = `<div class="nx-opts"><button type="button" class="btn sm primary" data-resume>${icon("play")}Resume</button><button type="button" class="btn sm danger" data-stop>${icon("stop")}Stop</button></div>`;
  } else if (x.kind === "blocked" && x.blocker) {
    actions = `<div class="nx-opts"><button type="button" class="btn sm primary" data-retry-dep="${esc(x.blocker.id)}">${icon("retry")}Retry ${esc(x.blocker.label)}</button><button type="button" class="btn sm" data-drop-dep="${esc(x.blocker.id)}">${icon("x")}Run without it</button></div>`;
  }
  const question = x.kind === "approval" ? "" : `<div class="nx-q">${md(x.question || "")}</div>`;
  return `<article class="nx-item" data-inbox="${esc(x.id)}" data-task="${esc(x.task_id)}" data-kind="${esc(x.kind)}">
    <header class="nx-head"><span class="badge ${tone}">${icon(ic, "sm")}${kindLabel}</span>${taskRef(x)}<span class="nx-meta">${esc(x.repo || "")}${x.time ? ` · ${esc(timeAgo(x.time))}` : ""}</span></header>
    ${who ? `<div class="nx-who">${who}</div>` : ""}
    ${question}
    ${(x.blocks || []).length ? `<div class="nx-blocks">${icon("layers", "sm")}<span>Holding up ${esc(x.blocks.slice(0, 3).join(", "))}${x.blocks.length > 3 ? ` +${x.blocks.length - 3}` : ""}</span></div>` : ""}
    ${actions}
  </article>`;
}

export function bindInbox(root, items, onDone) {
  const find = (el) => items.find((x) => x.id === el.closest("[data-inbox]")?.dataset.inbox);
  const run = async (btn, fn, ok) => {
    const card = btn.closest(".nx-item");
    $$("button", card).forEach((b) => (b.disabled = true));
    try { await fn(); card.classList.add("nx-done"); toast("success", ok); onDone && onDone(); }
    catch (e) { $$("button", card).forEach((b) => (b.disabled = false)); toast("error", "Could not send", e.message); }
  };
  const answer = (btn, x, text) => run(btn, () => api.action(x.task_id, "answer", { id: x.id, text }), `Answered · ${x.number ? "#" + x.number : x.task}`);
  $$("[data-answer-opt]", root).forEach((b) => (b.onclick = () => { const x = find(b); if (x) answer(b, x, b.dataset.answerOpt); }));
  $$("[data-answer-send]", root).forEach((b) => (b.onclick = () => {
    const x = find(b); const ta = $("[data-answer-text]", b.closest(".nx-item"));
    if (!x) return;
    if (!ta.value.trim()) { ta.focus(); return toast("warning", "Write an answer first"); }
    answer(b, x, ta.value.trim());
  }));
  $$("[data-answer-text]", root).forEach((ta) => {
    const fit = () => { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight + 2, 200)}px`; };
    ta.addEventListener("input", fit);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("[data-answer-send]", ta.closest(".nx-item"))?.click(); } });
  });
  $$("[data-approve]", root).forEach((b) => (b.onclick = () => { const x = find(b); const note = $("[data-answer-text]", b.closest(".nx-item"))?.value.trim() || ""; run(b, () => api.action(x.task_id, "approve", { note }), "Delivery approved"); }));
  $$("[data-reject]", root).forEach((b) => (b.onclick = () => {
    const x = find(b); const ta = $("[data-answer-text]", b.closest(".nx-item"));
    if (!ta.value.trim()) { ta.focus(); return toast("warning", "Say what should change"); }
    run(b, () => api.action(x.task_id, "reject", { note: ta.value.trim() }), "Changes requested");
  }));
  $$("[data-resume]", root).forEach((b) => (b.onclick = () => { const x = find(b); run(b, () => api.action(x.task_id, "resume"), "Resumed"); }));
  $$("[data-stop]", root).forEach((b) => (b.onclick = () => { const x = find(b); run(b, () => api.action(x.task_id, "stop"), "Stopping"); }));
  $$("[data-retry-dep]", root).forEach((b) => (b.onclick = () => run(b, () => api.action(b.dataset.retryDep, "retry"), "Requeued")));
  $$("[data-drop-dep]", root).forEach((b) => (b.onclick = () => {
    const x = find(b); const t = S.tasks.get(x.task_id);
    const deps = (t?.depends_on || []).filter((d) => d !== b.dataset.dropDep);
    run(b, () => api.updateTask(x.task_id, { depends_on: deps }), "Dependency removed");
  }));
}

function inboxEmpty() {
  return `<div class="empty nx-empty">${icon("check", "lg")}<h3>Nothing needs you</h3><p>Questions, approvals and decisions from every task land here, so you can unblock the whole queue from one screen.</p></div>`;
}

// ---------------------------------------------------------------------------- Needs you page
export function mountInbox(main) {
  main.innerHTML = `<div class="page nx-page" id="nxPage"><div class="empty small">Loading…</div></div>`;
  const page = $("#nxPage", main);
  let alive = true, items = [];
  async function load() {
    try { items = (await api.inbox()).items || []; } catch (e) { if (alive) page.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (!alive) return;
    // Keep what is being typed when the list refreshes underneath it.
    const drafts = new Map($$("[data-inbox]", page).map((c) => [c.dataset.inbox, $("[data-answer-text]", c)?.value || ""]));
    const focused = document.activeElement?.closest?.("[data-inbox]")?.dataset.inbox;
    page.innerHTML = `<div class="page-head"><div><h1>Needs you</h1><p>${items.length ? `${items.length} thing${items.length === 1 ? "" : "s"} waiting. Answer here; each task carries on by itself, and the rest of the queue keeps running meanwhile.` : "Everything is moving without you."}</p></div>
      <div class="page-actions"><a class="btn sm" href="#/digest">${icon("sparkles")}Digest</a></div></div>
      <div class="nx-list">${items.length ? items.map(inboxItemHtml).join("") : inboxEmpty()}</div>`;
    for (const [id, v] of drafts) { const ta = $(`[data-inbox="${CSS.escape(id)}"] [data-answer-text]`, page); if (ta && v) ta.value = v; }
    if (focused) $(`[data-inbox="${CSS.escape(focused)}"] [data-answer-text]`, page)?.focus();
    bindInbox(page, items, () => setTimeout(load, 600));
  }
  const soon = throttle(() => { if (!page.contains(document.activeElement) || !document.activeElement.matches("textarea")) load(); }, 1500);
  load();
  return { update(reason, info) { if (reason === "task" && (info?.pendingChanged || info?.statusChanged)) soon(); if (reason === "autopilot") soon(); }, destroy() { alive = false; } };
}

// ---------------------------------------------------------------------------- Digest page
const PERIODS = [[12, "12 hours"], [24, "24 hours"], [72, "3 days"], [168, "7 days"]];

export function mountDigest(main) {
  main.innerHTML = `<div class="page dg-page" id="dgPage"><div class="empty small">Loading…</div></div>`;
  const page = $("#dgPage", main);
  let alive = true, hours = Number(localStorage.getItem("relay.digestHours")) || S.config?.autopilot?.digest_hours || 24, d = null;
  async function load() {
    try { d = await api.digest(hours); } catch (e) { if (alive) page.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (alive) draw();
  }
  function draw() {
    const h = d.headline, u = d.usage || {};
    const scoreTone = (s) => (s == null ? "" : s >= 80 ? "green" : s >= 50 ? "amber" : "red");
    const delivered = d.delivered.map((r) => `<li class="dg-row">
        <div class="dg-main">${taskRef(r)}${r.summary ? `<p>${esc(r.summary)}</p>` : ""}
          <div class="dg-sub">${r.pr_url ? `<a href="${esc(r.pr_url)}" target="_blank" rel="noopener">${icon("external", "sm")}PR #${esc(r.pr_number)}</a>` : "<span>No pull request</span>"}${r.pr_state && r.pr_state !== "open" ? `<span class="badge ${r.pr_state === "merged" ? "green" : "red"}">${esc(r.pr_state)}</span>` : ""}
          <span>${r.duration_seconds ? fmtDur(r.duration_seconds) : ""}</span><span>${money(r.cost_usd)}</span>${r.follow_ups ? `<span>${r.follow_ups} follow-up${r.follow_ups === 1 ? "" : "s"}</span>` : ""}${r.fallbacks ? `<span title="Autopilot switched agents for limits">${icon("retry", "sm")}fallback</span>` : ""}${r.auto_retries ? `<span>${r.auto_retries} auto retr${r.auto_retries === 1 ? "y" : "ies"}</span>` : ""}</div></div>
        ${r.score != null ? `<span class="dg-score ${scoreTone(r.score)}" title="Scorecard">${esc(r.score)}</span>` : ""}</li>`).join("");
    const failures = d.failures.map((r) => `<li class="dg-row">
        <div class="dg-main">${taskRef(r)}<p class="dg-err">${esc(r.error || "")}</p>
          <div class="dg-sub"><span class="badge ${r.category === "stopped" ? "" : r.infra ? "amber" : "red"}">${esc(r.category_label || r.category)}</span>${r.infra ? `<span>${r.retried ? `retried ${r.retried}×` : "infrastructure"}</span>` : ""}<span>${esc(timeAgo(r.finished_at))}</span></div></div>
        <button type="button" class="btn xs" data-retry="${esc(r.task_id)}">${icon("retry")}Retry</button></li>`).join("");
    const next = d.next.map((r) => `<li class="dg-row">
        <div class="dg-main">${taskRef(r)}<div class="dg-sub">${r.waiting ? `<span class="dg-wait">${esc(r.waiting)}</span>` : ""}<span>${esc(r.repo || "")}</span>${r.priority && r.priority !== "normal" ? `<span class="badge ${r.priority === "urgent" ? "red" : "amber"}">${esc(r.priority)}</span>` : ""}</div></div>
        <div class="dg-eta" title="Estimated from ${esc(r.basis || "history")}"><b>${dayClock(r.eta_start)} → ${clock(r.eta_finish)}</b><span>~${fmtDur(r.estimate_seconds)}</span></div></li>`).join("");
    const agents = Object.entries(u.by_agent || {}).sort((a, b) => b[1] - a[1]);
    const limits = (d.limits || []).map((l) => `<li class="dg-limit"><span class="dot ${l.ok === false ? "warn" : l.ok ? "on" : ""}"></span><b>${esc(agentLabel(l.agent))}</b><span>${l.ok === false ? esc(l.reason) + (l.resets_at ? ` · resets ${dayClock(l.resets_at)}` : "") : l.ok ? "available" : esc(l.reason || "no reading yet")}</span></li>`).join("");
    const lessons = (d.lessons.items || []).map((x) => `<li>${esc(x.text)}${x.task_id ? ` <a href="#/task/${esc(x.task_id)}" class="muted">${esc(x.task_name || "")}</a>` : ""}</li>`).join("");
    const rec = (d.status?.watchdog?.recovered || []);
    page.innerHTML = `
      <div class="page-head dg-head"><div><h1>Digest</h1><p>${esc(d.summary)} · since ${esc(new Date(d.since).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }))}</p></div>
        <div class="page-actions"><select class="input" id="dgHours" aria-label="Period">${PERIODS.map(([v, l]) => `<option value="${v}" ${Number(hours) === v ? "selected" : ""}>Last ${l}</option>`).join("")}</select>
          <button class="btn" id="dgLimits" title="Read every agent's plan limits again">${icon("gauge")}Check limits</button></div></div>
      <div class="kpis dg-kpis">
        <div class="kpi"><div class="lbl">${icon("check", "sm")}Delivered</div><div class="val">${h.delivered}</div><div class="sub">${h.avg_score != null ? `average score ${h.avg_score}` : "no scorecards yet"}</div></div>
        <a class="kpi ${h.needs_you ? "kpi-attn" : ""}" href="#/inbox"><div class="lbl">${icon("bell", "sm")}Needs you</div><div class="val">${h.needs_you}</div><div class="sub">${h.needs_you ? "answer from here" : "nothing waiting"}</div></a>
        <div class="kpi"><div class="lbl">${icon("alert", "sm")}Failed</div><div class="val">${h.failed}</div><div class="sub">${d.failures.filter((f) => f.infra).length} infrastructure</div></div>
        <div class="kpi"><div class="lbl">${icon("list", "sm")}Queued</div><div class="val">${h.queued}</div><div class="sub">${h.running} running</div></div>
        <div class="kpi"><div class="lbl">${icon("dollar", "sm")}Spend</div><div class="val">${money(u.cost_usd)}</div><div class="sub">today ${money(u.today_usd)}${u.daily_cap_usd ? ` of $${u.daily_cap_usd}` : ""} · estimated</div></div>
      </div>
      <div class="dg-grid">
        <div class="dg-col">
          <section class="card"><div class="card-head"><h3>${icon("bell", "sm")} Needs you</h3><span class="badge ${d.inbox.length ? "amber" : ""}">${d.inbox.length}</span></div>
            <div class="card-body nx-list">${d.inbox.length ? d.inbox.map(inboxItemHtml).join("") : `<div class="empty small">${icon("check")} Nothing is waiting for you.</div>`}</div></section>
          <section class="card"><div class="card-head"><h3>Delivered</h3><span class="badge green">${d.delivered.length}</span></div>
            <div class="card-body">${delivered ? `<ul class="dg-list">${delivered}</ul>` : '<div class="empty small">Nothing delivered in this period.</div>'}</div></section>
          <section class="card"><div class="card-head"><h3>Failures</h3><span class="badge ${d.failures.length ? "red" : ""}">${d.failures.length}</span></div>
            <div class="card-body">${failures ? `<ul class="dg-list">${failures}</ul>` : '<div class="empty small">No failures.</div>'}</div></section>
        </div>
        <div class="dg-col">
          <section class="card"><div class="card-head"><h3>Next in the queue</h3><span class="muted dg-note">${esc(d.status?.label || "")}${d.status?.until_text ? ` · ${esc(d.status.until_text)}` : ""}</span></div>
            <div class="card-body">${next ? `<ul class="dg-list">${next}</ul><p class="help">Times are estimates from past durations of similar tasks.</p>` : '<div class="empty small">The queue is empty.</div>'}</div></section>
          <section class="card"><div class="card-head"><h3>Usage and limits</h3></div>
            <div class="card-body">
              ${agents.length ? `<ul class="dg-spend">${agents.map(([a, c]) => `<li><span class="av ${esc(a)}"></span><b>${esc(agentLabel(a))}</b><span>${money(c)}</span></li>`).join("")}</ul>` : '<p class="muted">No agent turns in this period.</p>'}
              ${limits ? `<ul class="dg-limits">${limits}</ul>` : ""}
            </div></section>
          <section class="card"><div class="card-head"><h3>Lessons awaiting review</h3><a class="btn xs" href="#/lessons">Review</a></div>
            <div class="card-body">${d.lessons.pending ? `<ul class="dg-lessons">${lessons}</ul>${d.lessons.pending > 5 ? `<p class="muted">+${d.lessons.pending - 5} more</p>` : ""}` : '<div class="empty small">No lessons to review.</div>'}</div></section>
          ${rec.length ? `<section class="card"><div class="card-head"><h3>Watchdog</h3></div><div class="card-body"><ul class="dg-lessons">${rec.map((r) => `<li><a href="#/task/${esc(r.task_id)}">${esc((S.tasks.get(r.task_id) || {}).name || r.task_id)}</a> · ${esc(r.kind === "orphan" ? "resumed after it lost its runner" : "restarted a vanished agent process")} · ${esc(timeAgo(r.time))}</li>`).join("")}</ul></div></section>` : ""}
        </div>
      </div>`;
    $("#dgHours", page).onchange = (e) => { hours = Number(e.target.value); try { localStorage.setItem("relay.digestHours", String(hours)); } catch {} load(); };
    $("#dgLimits", page).onclick = async (e) => {
      const b = e.currentTarget; b.disabled = true; b.innerHTML = `${icon("spinner", "spin")}Checking…`;
      try { await api.autopilotAction("limits"); await load(); toast("success", "Limits read again"); } catch (err) { toast("error", "Could not read limits", err.message); b.disabled = false; }
    };
    $$("[data-retry]", page).forEach((b) => (b.onclick = async () => { b.disabled = true; try { await api.action(b.dataset.retry, "retry"); toast("success", "Requeued"); load(); } catch (e) { toast("error", "Could not retry", e.message); b.disabled = false; } }));
    bindInbox(page, d.inbox, () => setTimeout(load, 600));
  }
  const soon = throttle(() => { if (!document.activeElement?.matches?.("textarea")) load(); }, 3000);
  load();
  return { update(reason, info) { if ((reason === "task" && (info?.statusChanged || info?.pendingChanged)) || reason === "autopilot") soon(); }, destroy() { alive = false; } };
}
