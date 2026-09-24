// Mission Control: one live screen for "what is my AI team doing, what needs me, how well is it going".
// Server side: orchestrator/mission.py (digest + live activity + trend). The range switch turns the same screen into
// the digest for the last day, three days or week.
import { $, $$, esc, icon, fmtDur, fmtSec, timeAgo, toast, throttle, basename , skeleton} from "../ui.js";
import { S, bus, navigate, statusOf, LIVE, queuedInOrder, agentLabel, taskElapsed, roleAgent } from "../state.js";
import { api } from "../api.js";
import { inboxItemHtml, bindInbox } from "./digest.js";
import { openNewTask } from "./newtask.js";
import { seedActivity, activity, activityHtml, stepperHtml, teamHtml, repoChips, phases } from "../live.js";
import { inProject } from "./org/org.js";
import { mockupThumb } from "./mockups.js";

const RANGES = [["today", "Today"], ["24h", "24 h"], ["3d", "3 days"], ["7d", "7 days"]];
const HOURS = { "24h": 24, "3d": 72, "7d": 168 };
const hoursOf = (range) => HOURS[range] || Math.max(1, (Date.now() - new Date().setHours(0, 0, 0, 0)) / 3600000);
const money = (n) => (Number(n) ? `$${Number(n).toFixed(Number(n) < 10 ? 2 : 0)}` : "$0");
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
const dayClock = (ts) => { if (!ts) return ""; const d = new Date(ts * 1000); return d.toDateString() === new Date().toDateString() ? clock(ts) : d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }); };
const scoreTone = (s) => (s == null ? "none" : s >= 80 ? "ok" : s >= 50 ? "warn" : "alarm");

export function scoreRing(score, size = 40) {
  const r = (size - 5) / 2, c = 2 * Math.PI * r, v = score == null ? 0 : Math.max(0, Math.min(100, score));
  return `<span class="ring-score tone-${scoreTone(score)}" style="--size:${size}px" title="${score == null ? "No scorecard yet" : `Scorecard ${score} of 100`}" role="img" aria-label="${score == null ? "no score" : `score ${score}`}">
    <svg viewBox="0 0 ${size} ${size}" aria-hidden="true"><circle class="sr-track" cx="${size / 2}" cy="${size / 2}" r="${r}"/><circle class="sr-val" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-dasharray="${(v / 100) * c} ${c}" transform="rotate(-90 ${size / 2} ${size / 2})"/></svg>
    <b>${score == null ? "–" : esc(score)}</b></span>`;
}

// Delivered and failed per day as columns, success rate as a line: drawn in a fixed viewBox, labels outside the SVG.
export function trendSvg(days, { height = 64 } = {}) {
  if (!days.length) return "";
  const w = days.length * 12, max = Math.max(1, ...days.map((d) => (d.done || 0) + (d.failed || 0) + (d.stopped || 0)));
  const bars = days.map((d, i) => {
    const ok = ((d.done || 0) / max) * (height - 8), bad = (((d.failed || 0) + (d.stopped || 0)) / max) * (height - 8);
    return `<rect class="tr-ok" x="${i * 12 + 2}" y="${height - ok}" width="8" height="${ok}" rx="1.5"/><rect class="tr-bad" x="${i * 12 + 2}" y="${height - ok - bad - (bad && ok ? 1.5 : 0)}" width="8" height="${bad}" rx="1.5"/>`;
  }).join("");
  let path = "", pen = false;
  days.forEach((d, i) => { if (d.success_rate == null) { pen = false; return; } path += `${pen ? "L" : "M"}${i * 12 + 6},${(1 - d.success_rate) * (height - 10) + 5}`; pen = true; });
  return `<svg class="trend-svg" viewBox="0 0 ${w} ${height}" preserveAspectRatio="none" aria-hidden="true">${bars}<path class="tr-line" d="${path}" vector-effect="non-scaling-stroke"/></svg>`;
}

export function mountMission(main, tab) {
  let range = RANGES.some(([k]) => k === tab) ? tab : (tab === "needs" ? "today" : "today");
  const focusNeeds = tab === "needs";
  let d = null, alive = true, loading = false;

  main.innerHTML = `<div class="page mc" id="mc">
    <header class="page-header mc-head">
      <div class="ph-title">
        <div class="eyebrow">Mission Control · ${esc(new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }))}</div>
        <h1 id="mcHeadline"><span class="skel" style="width:320px;height:22px"></span></h1>
        <p class="ph-sub" id="mcSub">&nbsp;</p>
      </div>
      <div class="ph-actions">
        <div class="seg seg-lg" role="tablist" aria-label="Period" id="mcRange">${RANGES.map(([k, l]) => `<button type="button" role="tab" data-range="${k}" aria-selected="${k === range}" class="${k === range ? "active" : ""}">${l}</button>`).join("")}</div>
      </div>
    </header>
    <section class="metric-strip" id="mcMetrics" aria-label="Key figures">${Array.from({ length: 5 }, () => '<div class="metric skel-block"></div>').join("")}</section>
    <div class="mc-grid">
      <section class="panel mc-live" aria-labelledby="mcLiveH"><header class="panel-head"><h2 id="mcLiveH">${icon("activity")}On shift</h2><span class="panel-meta" id="mcLiveMeta"></span></header><div class="panel-body" id="mcLive">${skeleton("cards", 2)}</div></section>
      <section class="panel mc-needs ${focusNeeds ? "focus" : ""}" id="needs" aria-labelledby="mcNeedsH"><header class="panel-head"><h2 id="mcNeedsH">${icon("inbox")}Needs you</h2><span class="count-pill" id="mcNeedsCount"></span></header><div class="panel-body nx-list" id="mcNeeds">${skeleton("list", 3)}</div></section>
      <section class="panel mc-queue" aria-labelledby="mcQueueH"><header class="panel-head"><h2 id="mcQueueH">${icon("list")}Up next</h2><span class="panel-meta" id="mcQueueMeta"></span><span class="panel-actions" id="mcQueueActions"></span></header><div class="panel-body flush" id="mcQueue">${skeleton("list", 3)}</div></section>
      <section class="panel mc-health" aria-labelledby="mcHealthH"><header class="panel-head"><h2 id="mcHealthH">${icon("gauge")}Limits and spend</h2></header><div class="panel-body" id="mcHealth">${skeleton("list", 3)}</div></section>
      <section class="panel mc-delivered" aria-labelledby="mcDelH"><header class="panel-head"><h2 id="mcDelH">${icon("checkCircle")}Delivered</h2><span class="panel-meta" id="mcDelMeta"></span></header><div class="panel-body flush" id="mcDelivered">${skeleton("list", 3)}</div></section>
      <section class="panel mc-trend" aria-labelledby="mcTrendH"><header class="panel-head"><h2 id="mcTrendH">${icon("trend")}How it is going</h2><span class="panel-meta">14 days</span></header><div class="panel-body" id="mcTrend">${skeleton("list", 4)}</div></section>
    </div>
  </div>`;
  const page = $("#mc", main);

  $$("[data-range]", page).forEach((b) => (b.onclick = () => {
    range = b.dataset.range;
    $$("[data-range]", page).forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-selected", x === b); });
    history.replaceState(null, "", range === "today" ? "#/" : `#/home/${range}`);
    load();
  }));

  async function load() {
    if (loading) return;
    loading = true;
    try { d = await api.mission(hoursOf(range)); }
    catch (e) { if (alive && !d) $("#mcLive", page).innerHTML = `<div class="empty-state">${icon("alert", "lg")}<h3>Could not load Mission Control</h3><p>${esc(e.message)}</p><button class="btn sm" data-retry type="button">${icon("refresh")}Try again</button></div>`; $("[data-retry]", page)?.addEventListener("click", load); return; }
    finally { loading = false; }
    if (!alive) return;
    seedActivity(d.live);
    drawAll();
    if (focusNeeds) { $("#needs", page)?.scrollIntoView({ block: "start", behavior: "smooth" }); focusNeedsOnce(); }
  }
  let focused = false;
  function focusNeedsOnce() { if (focused) return; focused = true; setTimeout(() => $("#mcNeeds textarea, #mcNeeds button", page)?.focus({ preventScroll: true }), 300); }

  function drawAll() { drawHead(); drawMetrics(); drawLive(); drawNeeds(); drawQueue(); drawHealth(); drawDelivered(); drawTrend(); }

  const tasks = () => [...S.tasks.values()].filter((t) => !t.archived && inProject(t));
  const liveTasks = () => tasks().filter((t) => LIVE.has(t.status)).sort((a, b) => (a.started_at || "").localeCompare(b.started_at || ""));

  // ---------------------------------------------------------------- headline
  function drawHead() {
    const live = liveTasks().length, needs = d?.inbox?.length || 0, h = d?.headline || {};
    const rangeLabel = range === "today" ? "today" : `in the last ${RANGES.find(([k]) => k === range)[1]}`;
    const parts = [];
    parts.push(live ? `${live} agent team${live === 1 ? "" : "s"} at work` : "No one on shift");
    if (needs) parts.push(`<span class="hl-attn">${needs} thing${needs === 1 ? "" : "s"} need${needs === 1 ? "s" : ""} you</span>`);
    else parts.push("nothing waits for you");
    $("#mcHeadline", page).innerHTML = parts.join(", ") + ".";
    const ap = S.autopilot || {};
    const apText = ap.state === "halted" ? "Queue halted" : ap.paused ? "Autopilot paused" : ap.label || "";
    $("#mcSub", page).innerHTML = `${h.delivered || 0} delivered ${rangeLabel}${h.avg_score != null ? ` · average score ${esc(h.avg_score)}` : ""} · ${esc(apText)}${ap.until_text && ap.state !== "running" ? ` until ${esc(ap.until_text)}` : ""}`;
  }

  // ---------------------------------------------------------------- metrics
  function drawMetrics() {
    const h = d.headline || {}, u = d.usage || {}, ap = S.autopilot || {};
    const trend = d.trend || [];
    const last7 = trend.slice(-7), prev7 = trend.slice(-14, -7);
    const rate = (xs) => { const done = xs.reduce((a, x) => a + (x.done || 0), 0), fin = xs.reduce((a, x) => a + (x.done || 0) + (x.failed || 0) + (x.stopped || 0), 0); return fin ? done / fin : null; };
    const r7 = rate(last7), rp = rate(prev7);
    const delta = r7 != null && rp != null ? Math.round((r7 - rp) * 100) : null;
    const cap = u.daily_cap_usd || ap.daily_cap_usd || 0;
    const running = liveTasks().length, par = ap.max_parallel || S.queue?.max_parallel || 1;
    $("#mcMetrics", page).innerHTML = `
      <a class="metric" href="#/work"><span class="m-label">Running</span><span class="m-value">${running}</span><span class="m-sub">limit ${par} · ${ap.queued || 0} queued${ap.waiting_dependencies ? ` · ${ap.waiting_dependencies} waiting on others` : ""}</span></a>
      <a class="metric ${h.needs_you ? "is-attn" : ""}" href="#needs" data-scroll-needs><span class="m-label">Needs you</span><span class="m-value">${h.needs_you || 0}</span><span class="m-sub">${h.needs_you ? "answer below, work continues" : "the queue is unblocked"}</span></a>
      <div class="metric"><span class="m-label">Delivered</span><span class="m-value">${h.delivered || 0}</span><span class="m-sub">${h.failed ? `${h.failed} failed` : "no failures"} · ${range === "today" ? "today" : RANGES.find(([k]) => k === range)[1]}</span></div>
      <div class="metric"><span class="m-label">Success, 7 days</span><span class="m-value">${r7 == null ? "—" : `${Math.round(r7 * 100)}<small>%</small>`}</span><span class="m-sub">${delta == null ? "no earlier week to compare" : delta === 0 ? "same as the week before" : `<span class="delta ${delta > 0 ? "up" : "down"}">${delta > 0 ? "▲" : "▼"} ${Math.abs(delta)} pts</span> vs the week before`}</span></div>
      <div class="metric"><span class="m-label">Spend${u.cost_usd ? " (est.)" : ""}</span><span class="m-value">${money(u.cost_usd)}</span><span class="m-sub">today ${money(u.today_usd)}${cap ? ` of $${cap}` : ""}</span>${cap ? `<span class="m-bar" aria-hidden="true"><i style="width:${Math.min(100, ((u.today_usd || 0) / cap) * 100)}%"></i></span>` : ""}</div>`;
    $("[data-scroll-needs]", page).onclick = (e) => { e.preventDefault(); $("#needs", page).scrollIntoView({ behavior: "smooth", block: "start" }); };
  }

  // ---------------------------------------------------------------- live agents
  function agentCard(t) {
    const pk = phases(t).packages;
    const cost = t.metrics?.total?.cost_usd;
    const said = activity.get(t.id)?.said;
    const pct = pk.total ? Math.round((pk.done / pk.total) * 100) : 0;
    const p = t.process || {};
    // While a team explores design directions, the card shows the mockup it is looking at.
    const thumb = t.checkpoint?.phase === "explore" || t.pending?.kind === "design_pick" ? mockupThumb(t) : null;
    return `<article class="agent-card" data-task="${esc(t.id)}" style="--agent:var(--${esc(p.agent || roleAgent(t, "worker") || "accent")}, var(--accent))">
      <header class="ac-head">
        <span class="live-dot" aria-hidden="true"></span>
        <a class="ac-name" href="#/task/${esc(t.id)}">${t.number ? `<span class="num">#${esc(t.number)}</span>` : ""}${esc(t.name)}</a>
        <span class="ac-clock mono" data-elapsed="${esc(t.id)}" title="Elapsed">${fmtSec(taskElapsed(t))}</span>
      </header>
      <div class="ac-meta">${repoChips(t, 2)}${teamHtml(t)}<span class="ac-cost mono" title="Estimated spend so far">${cost ? money(cost) : ""}</span></div>
      ${thumb ? `<a class="ac-thumb" href="#/task/${esc(t.id)}/mockups" title="Exploring design directions · ${esc(thumb.id)} ${esc(thumb.title)}"><img src="${esc(thumb.url)}" alt="Mockup ${esc(thumb.id)}: ${esc(thumb.title)}" loading="lazy"><span class="ac-thumb-cap">${icon("image", "sm")}Exploring directions · ${esc((t.exploration.directions || []).length)} mockups</span></a>` : ""}
      <div class="ac-activity" data-activity="${esc(t.id)}">${activityHtml(t)}</div>
      ${said ? `<p class="ac-said" data-said="${esc(t.id)}"><span class="av xs ${esc(said.agent || "system")}"></span>${esc(said.text)}</p>` : `<p class="ac-said muted" data-said="${esc(t.id)}">${esc(t.detail || "")}</p>`}
      <footer class="ac-foot">
        ${stepperHtml(t, { size: "mini" })}
        <span class="ac-pk" title="${pk.total ? `${pk.done} of ${pk.total} work packages` : "Planning"}">${pk.total ? `<span class="pk-bar" aria-hidden="true"><i style="width:${pct}%"></i></span><span class="mono">${pk.done}/${pk.total}</span>` : ""}</span>
        <a class="btn xs" href="#/task/${esc(t.id)}/live">${icon("monitor", "sm")}Watch</a>
      </footer>
    </article>`;
  }
  function drawLive() {
    const rows = liveTasks();
    const ap = S.autopilot || {};
    $("#mcLiveMeta", page).textContent = rows.length ? `${rows.length} running · parallel limit ${ap.max_parallel || S.queue?.max_parallel || 1}` : "";
    const host = $("#mcLive", page);
    if (!rows.length) {
      const next = d?.next?.[0];
      host.innerHTML = `<div class="empty-state compact">
        <div class="es-art" aria-hidden="true">${icon("radar", "lg")}</div>
        <h3>No agents on shift</h3>
        <p>${next ? `Next up: <a href="#/task/${esc(next.task_id)}">${esc(next.name)}</a>${next.eta_start ? `, around ${esc(dayClock(next.eta_start))}` : ""}.` : "Queue a task and a supervisor and a worker pick it up in an isolated branch."} ${ap.state === "halted" ? "The queue is halted." : ap.paused ? "Autopilot is paused." : ""}</p>
        <div class="row wrap center">${ap.state === "halted" && (ap.queued || 0) ? `<button class="btn primary sm" type="button" data-run>${icon("play")}Run the queue</button>` : ap.paused ? `<button class="btn primary sm" type="button" data-resume>${icon("play")}Resume autopilot</button>` : ""}<button class="btn sm" type="button" data-new>${icon("plus")}New task</button></div>
      </div>`;
      $("[data-run]", host)?.addEventListener("click", () => bus.emit("queue:run"));
      $("[data-resume]", host)?.addEventListener("click", () => bus.emit("autopilot:toggle"));
      $("[data-new]", host)?.addEventListener("click", () => openNewTask());
      return;
    }
    host.innerHTML = `<div class="agent-grid">${rows.map(agentCard).join("")}</div>`;
  }
  function patchActivity(tid) {
    const t = S.tasks.get(tid);
    const box = $(`[data-activity="${CSS.escape(tid)}"]`, page);
    if (!t || !box) return;
    box.innerHTML = activityHtml(t);
    const said = activity.get(tid)?.said, s = $(`[data-said="${CSS.escape(tid)}"]`, page);
    if (s && said) { s.classList.remove("muted"); s.innerHTML = `<span class="av xs ${esc(said.agent || "system")}"></span>${esc(said.text)}`; }
    const card = box.closest(".agent-card");
    if (card) { const team = $(".team", card); if (team) team.outerHTML = teamHtml(t); }
  }

  // ---------------------------------------------------------------- needs you
  function drawNeeds() {
    const items = d.inbox || [];
    const host = $("#mcNeeds", page);
    const drafts = new Map($$("[data-inbox]", host).map((c) => [c.dataset.inbox, $("[data-answer-text]", c)?.value || ""]));
    const focusedId = document.activeElement?.closest?.("[data-inbox]")?.dataset.inbox;
    $("#mcNeedsCount", page).textContent = items.length || "";
    $("#mcNeedsCount", page).hidden = !items.length;
    // Anything genuinely blocked, in one place, with what it needs (#65) — including a run that carried on
    // with the work its open question does not block.
    const blocked = (d.blocked || []).filter((b) => !items.some((x) => x.task_id === b.task_id));
    const blockedHtml = blocked.length ? `<div class="nx-blocked"><h3>${icon("alert", "sm")}Blocked · ${blocked.length}</h3>${blocked.map((b) => `
      <a class="nx-blocked-row" href="#/task/${esc(b.task_id)}"><span class="truncate">${b.number ? `#${esc(b.number)} ` : ""}${esc(b.task || "")}</span>
        <span class="muted truncate">${esc(b.needs || "")}</span>
        ${b.working_around ? '<span class="badge amber">working around it</span>' : ""}</a>`).join("")}</div>` : "";
    host.innerHTML = (items.length ? items.map(inboxItemHtml).join("") : `<div class="empty-state compact calm">${icon("checkCircle", "lg")}<h3>You are not blocking anything</h3><p>Questions, approvals and decisions from every task appear here, answerable in place.</p></div>`) + blockedHtml;
    for (const [id, v] of drafts) { const ta = $(`[data-inbox="${CSS.escape(id)}"] [data-answer-text]`, host); if (ta && v) ta.value = v; }
    if (focusedId) $(`[data-inbox="${CSS.escape(focusedId)}"] [data-answer-text]`, host)?.focus();
    bindInbox(host, items, () => setTimeout(load, 500));
  }

  // ---------------------------------------------------------------- queue lane
  function drawQueue() {
    const order = queuedInOrder(tasks());
    const eta = new Map((d.next || []).map((x) => [x.task_id, x]));
    const ap = S.autopilot || {};
    $("#mcQueueMeta", page).textContent = order.length ? `${order.length} queued` : "";
    $("#mcQueueActions", page).innerHTML = ap.state === "halted" ? `<button class="btn xs" type="button" data-q="run">${icon("play")}Run queue</button>` : `<button class="btn xs ghost" type="button" data-q="halt" title="Running tasks continue; nothing new starts">${icon("stop")}Halt</button>`;
    $("[data-q]", page).onclick = (e) => bus.emit(e.currentTarget.dataset.q === "run" ? "queue:run" : "queue:halt");
    const host = $("#mcQueue", page);
    if (!order.length) { host.innerHTML = `<div class="empty-state compact">${icon("list", "lg")}<h3>The queue is empty</h3><p>Drafts and GitHub issues wait on the <a href="#/work">Work board</a>; drag one to Queued to line it up.</p></div>`; return; }
    host.innerHTML = `<ol class="queue-lane" aria-label="Queue in run order">${order.map((t, i) => {
      const e = eta.get(t.id) || {};
      const deps = (t.depends_on || []).map((id) => S.tasks.get(id)).filter(Boolean);
      const waitKind = t.waiting?.kind;
      return `<li class="ql-row" draggable="true" data-id="${esc(t.id)}" data-i="${i}">
        <span class="ql-grip" aria-hidden="true">${icon("grip", "sm")}</span>
        <span class="ql-pos mono">${i + 1}</span>
        <span class="ql-main">
          <a class="ql-name" href="#/task/${esc(t.id)}">${t.number ? `<span class="num">#${esc(t.number)}</span>` : ""}${esc(t.name)}</a>
          <span class="ql-meta">${repoChips(t, 2)}${t.priority && t.priority !== "normal" ? `<span class="badge ${t.priority === "urgent" ? "red" : t.priority === "high" ? "amber" : ""}">${esc(t.priority)}</span>` : ""}${deps.map((x) => `<a class="dep-chip ${x.status === "done" ? "ok" : ""}" href="#/task/${esc(x.id)}" title="Runs after ${esc(x.name)} (${esc(statusOf(x).label)})">${icon("layers", "sm")}after #${esc(x.number || "?")}</a>`).join("")}${t.waiting?.text && waitKind !== "dependency" && waitKind !== "chain" ? `<span class="wait-chip">${icon(waitKind === "limits" ? "gauge" : "clock", "sm")}${esc(t.waiting.text)}</span>` : ""}</span>
        </span>
        <span class="ql-eta" title="${esc(e.basis ? `Estimated from ${e.basis}` : "Estimated from similar tasks")}">${e.eta_start ? `<b>${esc(dayClock(e.eta_start))}</b><span>~${esc(fmtDur(e.estimate_seconds || 0))}</span>` : '<span class="muted">—</span>'}</span>
        <span class="ql-actions">
          <button class="btn xs ghost icon" type="button" data-move="up" ${i === 0 ? "disabled" : ""} aria-label="Run earlier" title="Run earlier">${icon("chevronUp")}</button>
          <button class="btn xs ghost icon" type="button" data-move="down" ${i === order.length - 1 ? "disabled" : ""} aria-label="Run later" title="Run later">${icon("chevronDown")}</button>
          <button class="btn xs" type="button" data-start title="Start now, alongside what is running">${icon("play")}Start</button>
        </span>
      </li>`;
    }).join("")}</ol>`;
    bindQueue(host, order);
  }
  function bindQueue(host, order) {
    const move = async (id, steps) => {
      const dir = steps < 0 ? "up" : "down";
      try { for (let k = 0; k < Math.abs(steps); k++) { const r = await api.moveInQueue(id, dir); if (r.task) S.tasks.set(r.task.id, { ...S.tasks.get(r.task.id), ...r.task }); } }
      catch (e) { toast("error", "Could not reorder", e.message); }
      drawQueue(); reloadSoon();
    };
    $$(".ql-row", host).forEach((row) => {
      const id = row.dataset.id;
      $("[data-move='up']", row).onclick = () => move(id, -1);
      $("[data-move='down']", row).onclick = () => move(id, 1);
      $("[data-start]", row).onclick = async (e) => { e.currentTarget.disabled = true; try { await api.action(id, "start"); toast("success", "Started", S.tasks.get(id)?.name || ""); } catch (err) { toast("error", "Could not start", err.message); e.currentTarget.disabled = false; } };
      row.addEventListener("dragstart", (e) => { row.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", id); } catch {} });
      row.addEventListener("dragend", () => { row.classList.remove("dragging"); $$(".ql-row.over", host).forEach((x) => x.classList.remove("over")); });
      row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("over"); });
      row.addEventListener("dragleave", () => row.classList.remove("over"));
      row.addEventListener("drop", (e) => {
        e.preventDefault(); row.classList.remove("over");
        const from = order.findIndex((t) => t.id === (e.dataTransfer.getData("text/plain") || $(".ql-row.dragging", host)?.dataset.id));
        const to = Number(row.dataset.i);
        if (from >= 0 && from !== to) move(order[from].id, to - from);
      });
    });
  }

  // ---------------------------------------------------------------- limits and spend
  function drawHealth() {
    const u = d.usage || {}, ap = S.autopilot || {};
    const agents = Object.entries(u.by_agent || {}).sort((a, b) => b[1] - a[1]);
    const top = Math.max(0.0001, ...agents.map(([, c]) => c));
    const limits = d.limits || [];
    const par = ap.max_parallel || S.queue?.max_parallel || 1;
    $("#mcHealth", page).innerHTML = `
      <div class="ledger">
        <div class="ledger-row"><span>Autopilot</span><span class="row" style="gap:6px"><span class="dot ${ap.paused ? "warn" : ap.state === "halted" ? "" : "ok"}"></span><b>${esc(ap.paused ? "Paused" : ap.label || ap.state || "—")}</b></span></div>
        <div class="ledger-row"><span>Parallel teams</span><span class="stepper-input"><button type="button" class="btn xs ghost icon" data-par="-1" aria-label="Fewer parallel tasks" ${par <= 1 ? "disabled" : ""}>−</button><b class="mono">${par}</b><button type="button" class="btn xs ghost icon" data-par="1" aria-label="More parallel tasks" ${par >= 8 ? "disabled" : ""}>+</button></span></div>
        ${ap.quiet_hours ? `<div class="ledger-row"><span>Quiet hours</span><b>${icon("moon", "sm")} on</b></div>` : ""}
        <div class="ledger-row"><span>Today</span><b class="mono">${money(u.today_usd)}${u.daily_cap_usd ? ` <span class="muted">/ $${esc(u.daily_cap_usd)}</span>` : ""}</b></div>
      </div>
      ${limits.length ? `<h3 class="sub-h">Agent limits</h3><ul class="limit-list">${limits.map((l) => `<li><span class="av sm ${esc(l.agent)}"></span><span class="ll-name">${esc(l.label || agentLabel(l.agent))}</span><span class="ll-state tone-${l.ok === false ? "warn" : l.ok ? "ok" : "none"}">${l.ok === false ? `${esc(l.reason || "limited")}${l.resets_at ? ` · resets ${esc(dayClock(l.resets_at))}` : ""}` : l.ok ? "available" : esc(l.reason || "no reading yet")}</span></li>`).join("")}</ul>` : ""}
      <h3 class="sub-h">Spend by agent <span class="muted">· ${range === "today" ? "today" : RANGES.find(([k]) => k === range)[1]}</span></h3>
      ${agents.length ? `<ul class="spend-bars">${agents.map(([a, c]) => `<li><span class="av xs ${esc(a)}"></span><span class="sb-name">${esc(agentLabel(a))}</span><span class="sb-track" aria-hidden="true"><i style="width:${(c / top) * 100}%;--c:var(--${esc(a)}, var(--accent))"></i></span><b class="mono">${money(c)}</b></li>`).join("")}</ul>` : '<p class="muted small">No agent turns in this period.</p>'}
      <button class="btn xs ghost" type="button" id="mcLimits">${icon("refresh", "sm")}Read limits again</button>`;
    $$("[data-par]", page).forEach((b) => (b.onclick = async () => {
      const n = Math.max(1, Math.min(8, par + Number(b.dataset.par)));
      try { await api.saveSettings({ max_parallel: n }); if (S.queue?.running) S.queue = await api.queueStart(n); S.autopilot = await api.autopilot(); drawHealth(); drawLive(); drawMetrics(); } catch (e) { toast("error", "Could not change", e.message); }
    }));
    $("#mcLimits", page).onclick = async (e) => { const b = e.currentTarget; b.disabled = true; try { await api.autopilotAction("limits"); await load(); toast("success", "Limits read again"); } catch (err) { toast("error", "Could not read limits", err.message); b.disabled = false; } };
  }

  // ---------------------------------------------------------------- delivered + failures
  function drawDelivered() {
    const del = d.delivered || [], fails = d.failures || [];
    $("#mcDelMeta", page).textContent = `${del.length} delivered${fails.length ? ` · ${fails.length} failed` : ""}`;
    const host = $("#mcDelivered", page);
    if (!del.length && !fails.length) { host.innerHTML = `<div class="empty-state compact">${icon("checkCircle", "lg")}<h3>Nothing delivered ${range === "today" ? "today" : "in this period"} yet</h3><p>Every delivery lands here with its scorecard and pull requests, ready for review.</p></div>`; return; }
    const prBadge = (r) => r.pr_url ? `<a class="pr-chip state-${esc(r.pr_state || "open")}" href="${esc(r.pr_url)}" target="_blank" rel="noopener">${icon("github", "sm")}#${esc(r.pr_number)}<span>${esc(r.pr_state || "open")}</span></a>` : '<span class="pr-chip state-none">branch only</span>';
    host.innerHTML = `<ul class="deliveries">${del.map((r) => `<li class="dl-row">
        ${scoreRing(r.score)}
        <div class="dl-main"><a class="dl-name" href="#/task/${esc(r.task_id)}">${r.number ? `<span class="num">#${esc(r.number)}</span>` : ""}${esc(r.name)}</a>
          ${r.summary ? `<p class="dl-summary">${esc(r.summary)}</p>` : ""}
          <div class="dl-meta">${prBadge(r)}<span>${icon("folder", "sm")}${esc(basename(r.repo || ""))}</span>${r.duration_seconds ? `<span>${icon("clock", "sm")}${esc(fmtDur(r.duration_seconds))}</span>` : ""}<span class="mono">${money(r.cost_usd)}</span>${r.follow_ups ? `<span>${r.follow_ups} follow-up${r.follow_ups === 1 ? "" : "s"}</span>` : ""}<span class="muted">${esc(timeAgo(r.finished_at))}</span></div>
        </div>
        <a class="btn sm dl-review" href="#/review/${esc(r.task_id)}">${icon("eye", "sm")}Review</a>
      </li>`).join("")}
      ${fails.map((r) => `<li class="dl-row is-failed">
        <span class="fail-mark" aria-hidden="true">${icon("x")}</span>
        <div class="dl-main"><a class="dl-name" href="#/task/${esc(r.task_id)}">${r.number ? `<span class="num">#${esc(r.number)}</span>` : ""}${esc(r.name)}</a>
          <p class="dl-summary err">${esc(r.error || "")}</p>
          <div class="dl-meta"><span class="badge ${r.infra ? "amber" : "red"}">${esc(r.category_label || r.category || "failed")}</span><span class="muted">${esc(timeAgo(r.finished_at))}</span></div>
        </div>
        <button class="btn sm" type="button" data-retry="${esc(r.task_id)}">${icon("retry", "sm")}Retry</button>
      </li>`).join("")}</ul>`;
    $$("[data-retry]", host).forEach((b) => (b.onclick = async () => { b.disabled = true; try { await api.action(b.dataset.retry, "retry"); toast("success", "Requeued"); load(); } catch (e) { toast("error", "Could not retry", e.message); b.disabled = false; } }));
  }

  // ---------------------------------------------------------------- trend
  function drawTrend() {
    const days = d.trend || [];
    const done = days.reduce((a, x) => a + (x.done || 0), 0), fin = days.reduce((a, x) => a + (x.done || 0) + (x.failed || 0) + (x.stopped || 0), 0);
    const cost = days.reduce((a, x) => a + (x.cost_usd || 0), 0);
    const med = days.filter((x) => x.median_duration).map((x) => x.median_duration).sort((a, b) => a - b);
    const lessons = d.lessons || {};
    $("#mcTrend", page).innerHTML = `
      <div class="trend-figs"><div><b>${fin ? Math.round((done / fin) * 100) : "—"}<small>${fin ? "%" : ""}</small></b><span>success</span></div><div><b>${done}</b><span>delivered</span></div><div><b>${med.length ? esc(fmtDur(med[Math.floor(med.length / 2)])) : "—"}</b><span>median time</span></div><div><b>${money(cost)}</b><span>spend</span></div></div>
      ${fin ? `<div class="trend-chart">${trendSvg(days)}<div class="trend-axis"><span>${esc(days[0] ? new Date(days[0].date).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "")}</span><span>today</span></div></div>
      <div class="legend"><span><i class="sw ok"></i>delivered</span><span><i class="sw bad"></i>failed or stopped</span><span><i class="sw line"></i>success rate</span></div>` : '<p class="muted small">No task finished in the last 14 days.</p>'}
      <a class="lesson-link ${lessons.pending ? "has" : ""}" href="#/knowledge/lessons">${icon("brain", "sm")}<span>${lessons.pending ? `<b>${lessons.pending}</b> lesson${lessons.pending === 1 ? "" : "s"} from retrospectives to review` : "No lessons waiting for review"}</span>${icon("chevron", "sm")}</a>`;
  }

  // ---------------------------------------------------------------- live updates
  const reloadSoon = throttle(() => { if (!document.activeElement?.closest?.("#mcNeeds") || !document.activeElement.matches("textarea")) load(); }, 4000);
  const clockTimer = setInterval(() => {
    for (const elx of $$("[data-elapsed]", page)) { const t = S.tasks.get(elx.dataset.elapsed); if (t) elx.textContent = fmtSec(taskElapsed(t)); }
    for (const box of $$("[data-activity] .act.is-running", page)) { const tid = box.closest("[data-activity]").dataset.activity; patchActivity(tid); }
  }, 1000);
  load();
  const offCfg = bus.on("config", () => d && drawHealth());

  return {
    update(reason, info) {
      if (!d) return;
      if (reason === "activity" || reason === "process") { const tid = info?.task_id; if (tid) patchActivity(tid); return; }
      if (reason === "task") {
        if (info?.statusChanged || info?.created || info?.deleted) { drawLive(); drawQueue(); drawHead(); drawMetrics(); reloadSoon(); }
        else if (info?.pendingChanged) reloadSoon();
        else if (info?.id && LIVE.has(S.tasks.get(info.id)?.status)) patchActivity(info.id);
        return;
      }
      if (reason === "autopilot" || reason === "queue") { drawHead(); drawMetrics(); drawQueue(); drawHealth(); return; }
      if (reason === "lessons") reloadSoon();
    },
    destroy() { alive = false; clearInterval(clockTimer); offCfg(); },
  };
}
