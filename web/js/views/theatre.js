// Live theatre: watch a task like a pair-programming stream. The relay track shows which role holds the baton and
// animates each handoff; the editor pane shows the file being changed with its diff; the terminal pane tails the
// commands and their output; the feed lists every step as it happens. Everything comes from the task's message
// stream that the task page already keeps current, so the theatre adds no requests.
import { $, $$, esc, icon, fmtSec, fmtTime, diffHtml, basename } from "../ui.js";
import { S, LIVE, agentLabel, roleAgent, roleModelText, roleEffortText, ROLE_LABEL } from "../state.js";
import { describeEdit, shellEditTargets } from "./conversation.js";
import { activityHtml, activity, stepperHtml, describeActivity } from "../live.js";

const ROLES = ["supervisor", "worker", "reviewer"];
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

export function mountTheatre(host, getTask) {
  let file = null;          // file pinned in the editor pane; null follows the latest edit
  let lastEditId = null, lastRole = null, lastHandoffId = null;
  host.innerHTML = `<div class="theatre" aria-live="off">
    <section class="th-stage" aria-label="Team">
      <div class="th-now" id="thNow"></div>
      <div class="th-track" id="thTrack"></div>
      <div class="th-steps" id="thSteps"></div>
    </section>
    <div class="th-panes">
      <section class="th-pane th-editor" aria-label="Editor">
        <header class="th-pane-head"><span class="th-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="th-pane-title">${icon("code", "sm")}Editor</span><div class="th-files" id="thFiles" role="tablist" aria-label="Files edited"></div></header>
        <div class="th-pane-body" id="thEditor"></div>
      </section>
      <section class="th-pane th-terminal" aria-label="Terminal">
        <header class="th-pane-head"><span class="th-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="th-pane-title">${icon("terminal", "sm")}Terminal</span><span class="th-pane-meta" id="thTermMeta"></span></header>
        <div class="th-pane-body th-term" id="thTerm" tabindex="0"></div>
      </section>
    </div>
    <section class="th-feed" aria-label="Activity feed"><header class="th-feed-head"><h3>${icon("activity", "sm")}Step by step</h3><span class="muted small" id="thFeedMeta"></span></header><ol class="th-feed-list" id="thFeed"></ol></section>
  </div>`;

  const msgs = () => S.msgs.get(getTask()?.id)?.list || [];

  function drawStage() {
    const t = getTask(); if (!t) return;
    const p = t.process || {};
    const roles = ROLES.filter((r) => roleAgent(t, r));
    const store = msgs();
    let active = p.state === "running" && roles.includes(p.role) ? p.role : null;
    if (!active) for (let i = store.length - 1; i >= 0 && i > store.length - 200; i--) { if (roles.includes(store[i].role)) { active = store[i].role; break; } }
    const idx = Math.max(0, roles.indexOf(active));
    const live = LIVE.has(t.status);
    const turns = (r) => (t.sessions || {})[r]?.turns || 0;
    const track = $("#thTrack", host);
    if (!track.dataset.roles || track.dataset.roles !== roles.join(",")) {
      track.dataset.roles = roles.join(",");
      track.style.setProperty("--n", roles.length);
      track.innerHTML = `<div class="th-rail" aria-hidden="true"></div><span class="th-baton" aria-hidden="true"></span>${roles.map((r) => `<div class="th-role" data-role="${r}">
          <span class="th-av-wrap"><span class="av lg ${esc(roleAgent(t, r))}">${esc(agentLabel(roleAgent(t, r)).slice(0, 2))}</span><span class="th-ring" aria-hidden="true"></span></span>
          <span class="th-role-name">${esc(ROLE_LABEL[r])}</span>
          <span class="th-agent">${esc(agentLabel(roleAgent(t, r)))}<small title="Model: ${esc(roleModelText(t, r))}\nEffort: ${esc(roleEffortText(t, r))}">${esc(roleModelText(t, r))}</small></span>
          <span class="th-role-state" data-state="${r}"></span>
        </div>`).join("")}`;
    }
    for (const r of roles) {
      const node = $(`.th-role[data-role="${r}"]`, track);
      const on = live && r === active && p.state === "running";
      node.classList.toggle("on", on);
      node.classList.toggle("holds", r === active);
      $(`[data-state="${r}"]`, track).innerHTML = on ? `<span class="live-dot sm"></span>working · <span class="mono">${fmtSec(p.elapsed || 0)}</span>` : `${turns(r)} turn${turns(r) === 1 ? "" : "s"}`;
    }
    track.style.setProperty("--i", idx);
    track.classList.toggle("idle", !live);
    if (lastRole && active && active !== lastRole && !reduced()) {
      const node = $(`.th-role[data-role="${active}"]`, track);
      node?.classList.remove("handoff"); void node?.offsetWidth; node?.classList.add("handoff");
    }
    lastRole = active;
    $("#thSteps", host).innerHTML = stepperHtml(t);
    const now = activity.get(t.id);
    $("#thNow", host).innerHTML = `<span class="th-mode ${live ? "live" : ""}">${live ? '<span class="live-dot"></span>Live' : `${icon("clock", "sm")}Replay of the last activity`}</span>${activityHtml(t)}${now?.said ? `<span class="th-said"><span class="av xs ${esc(now.said.agent || "system")}"></span>${esc(now.said.text)}</span>` : ""}`;
  }

  // A handoff message travels along the track from the role that sent it to the one it addresses.
  function animateHandoff(m) {
    const t = getTask(); if (!t || reduced()) return;
    const roles = ROLES.filter((r) => roleAgent(t, r));
    const from = roles.indexOf(m.role), to = roles.indexOf(m.to);
    if (from < 0 || to < 0 || from === to) return;
    const rail = $("#thTrack", host); if (!rail) return;
    const pkt = document.createElement("span");
    pkt.className = "th-packet";
    pkt.style.setProperty("--from", from); pkt.style.setProperty("--to", to);
    pkt.title = m.title || "Handoff";
    rail.appendChild(pkt);
    setTimeout(() => pkt.remove(), 1300);
  }

  function edits() {
    const t = getTask(); const out = [];
    for (const m of msgs()) {
      if (m.kind !== "tool") continue;
      const ed = describeEdit(m, t);
      if (ed && ed.files.length) out.push({ m, ed, files: ed.files });
      else if (m.category === "shell") { const f = shellEditTargets(m.input || m.summary, t); if (f.length) out.push({ m, ed: { files: f, diff: "", add: 0, del: 0 }, files: f }); }
    }
    return out;
  }

  function drawEditor() {
    const all = edits();
    const recent = [];
    for (let i = all.length - 1; i >= 0 && recent.length < 6; i--) for (const f of all[i].files) if (!recent.includes(f)) recent.push(f);
    const current = file && recent.includes(file) ? file : recent[0];
    $("#thFiles", host).innerHTML = recent.map((f) => `<button type="button" role="tab" class="th-file ${f === current ? "active" : ""}" data-file="${esc(f)}" aria-selected="${f === current}" title="${esc(f)}">${icon("file", "sm")}<span>${esc(basename(f))}</span></button>`).join("");
    $$("[data-file]", host).forEach((b) => (b.onclick = () => { file = b.dataset.file === recent[0] ? null : b.dataset.file; lastEditId = null; drawEditor(); }));
    const box = $("#thEditor", host);
    const hit = [...all].reverse().find((x) => x.files.includes(current));
    if (!hit) { box.innerHTML = `<div class="th-empty">${icon("code", "lg")}<p>No file changed yet. Edits appear here the moment an agent makes them, with the diff.</p></div>`; return; }
    if (hit.m.id === lastEditId && hit.m.status === box.dataset.status) return;
    const fresh = hit.m.id !== lastEditId;
    lastEditId = hit.m.id; box.dataset.status = hit.m.status || "";
    const st = hit.m.status || "running";
    box.innerHTML = `<div class="th-filebar"><code class="mono">${esc(current)}</code>${hit.ed.add || hit.ed.del ? `<span class="diffstat"><span class="a">+${hit.ed.add}</span><span class="d">−${hit.ed.del}</span></span>` : ""}<span class="th-by"><span class="av xs ${esc(hit.m.agent || "system")}"></span>${esc(agentLabel(hit.m.agent || ""))} · ${esc(fmtTime(hit.m.time || hit.m.ts))}</span><span class="th-st st-${esc(st)}">${st === "running" ? `${icon("spinner", "spin sm")}writing` : st === "error" ? `${icon("x", "sm")}failed` : `${icon("check", "sm")}saved`}</span></div>
      ${hit.ed.diff ? diffHtml(hit.ed.diff) : `<div class="th-empty small"><p>Changed through a shell command, so there is no inline diff. Open <b>Changes</b> for the full diff.</p></div>`}`;
    const pre = $("pre.diff", box);
    if (pre && fresh && !reduced()) { $$(".dl", pre).slice(0, 60).forEach((l, i) => { l.style.setProperty("--i", i); l.classList.add("reveal"); }); }
    const firstAdd = $(".dl.add", box); if (firstAdd && fresh) box.scrollTop = Math.max(0, firstAdd.offsetTop - box.clientHeight / 3);
  }

  function drawTerminal() {
    const t = getTask();
    const rows = msgs().filter((m) => (m.kind === "tool" && m.category === "shell") || m.kind === "command").slice(-8);
    const term = $("#thTerm", host);
    const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    $("#thTermMeta", host).textContent = rows.length ? `${rows.filter((m) => m.status === "running").length ? "running" : "idle"}` : "";
    if (!rows.length) { term.innerHTML = `<div class="th-empty dark">${icon("terminal", "lg")}<p>Commands the agents and Relay run show up here with their output.</p></div>`; return; }
    term.innerHTML = rows.map((m) => {
      const cmd = m.kind === "command" ? (m.content || m.title || "") : String(m.summary || m.input || "").replace(/^\$\s*/, "");
      const out = String(m.output || "").split("\n").slice(-18).join("\n");
      const who = m.kind === "command" ? (ROLE_LABEL[m.role] || "Relay") : agentLabel(m.agent || "");
      const st = m.status || "running";
      return `<div class="term-block st-${esc(st)}"><div class="term-cmd"><span class="term-who">${esc(who)}</span><span class="term-prompt">$</span><span class="term-text">${esc(String(cmd).slice(0, 400))}</span><span class="term-st">${st === "running" ? `${icon("spinner", "spin sm")}` : st === "error" ? `exit ${esc(m.rc ?? "!")}` : m.duration ? esc(`${Number(m.duration).toFixed(1)}s`) : icon("check", "sm")}</span></div>${out.trim() ? `<pre class="term-out">${esc(out)}</pre>` : ""}</div>`;
    }).join("") + (rows.some((m) => m.status === "running") ? '<span class="term-caret" aria-hidden="true"></span>' : "");
    if (atBottom || !term.dataset.seen) { term.scrollTop = term.scrollHeight; term.dataset.seen = "1"; }
  }

  function drawFeed() {
    const t = getTask();
    const rows = msgs().filter((m) => ["tool", "command", "connector", "handoff", "plan", "decision", "review", "verification", "question", "approval", "user", "complete", "error"].includes(m.kind)).slice(-40).reverse();
    $("#thFeedMeta", host).textContent = `${msgs().length} events`;
    $("#thFeed", host).innerHTML = rows.map((m) => {
      let ic = "info", text = "", tone = "";
      if (m.kind === "tool" || m.kind === "command" || m.kind === "connector") { const d = describeActivity(t, { ...m, status: m.status || "ok" }); ic = d?.icon || "cpu"; text = `<b>${esc(d?.verb || m.tool)}</b> ${esc(d?.detail || "")}`; tone = m.status === "error" ? "alarm" : m.status === "running" ? "run" : ""; }
      else if (m.kind === "handoff") { ic = "arrowRight"; text = `<b>${esc(ROLE_LABEL[m.role] || m.role)} → ${esc(ROLE_LABEL[m.to] || m.to || "")}</b> ${esc(m.title || "")}`; tone = "handoff"; }
      else if (m.kind === "plan") { ic = "list"; text = `<b>Plan agreed</b> ${esc(m.summary || "")}`; tone = "handoff"; }
      else if (m.kind === "decision") { ic = m.decision === "done" ? "check" : "retry"; text = `<b>Supervisor: ${m.decision === "done" ? "complete" : "revise"}</b> ${esc(m.summary || "")}`; tone = m.decision === "done" ? "ok" : "warn"; }
      else if (m.kind === "review") { ic = "eye"; text = `<b>Review ${esc(m.verdict || "")}</b> ${esc(m.summary || "")}`; tone = m.verdict === "PASS" ? "ok" : "alarm"; }
      else if (m.kind === "verification") { ic = m.ok ? "shield" : "alert"; text = `<b>Verification ${m.ok ? "passed" : "failed"}</b> ${esc((m.items || []).map((i) => i.command).join(" · "))}`; tone = m.ok ? "ok" : "alarm"; }
      else if (m.kind === "question" || m.kind === "approval") { ic = "question"; text = `<b>${m.kind === "approval" ? "Approval requested" : "Question for you"}</b> ${esc(String(m.content || "").slice(0, 140))}`; tone = "warn"; }
      else if (m.kind === "user") { ic = "user"; text = `<b>You</b> ${esc(String(m.content || "").slice(0, 140))}`; }
      else if (m.kind === "complete") { ic = "checkCircle"; text = "<b>Delivered</b>"; tone = "ok"; }
      else if (m.kind === "error") { ic = "alert"; text = `<b>Error</b> ${esc(String(m.content || "").slice(0, 160))}`; tone = "alarm"; }
      return `<li class="feed-row tone-${tone || "none"}"><time class="mono">${esc(fmtTime(m.time || m.ts))}</time><span class="av xs ${esc(m.agent || (m.role === "user" ? "user" : "system"))}"></span><span class="feed-ic">${icon(ic, "sm")}</span><span class="feed-text">${text}</span></li>`;
    }).join("") || '<li class="muted small">Nothing yet.</li>';
  }

  function drawAll() { drawStage(); drawEditor(); drawTerminal(); drawFeed(); }
  drawAll();
  const timer = setInterval(() => { if (LIVE.has(getTask()?.status)) { drawStage(); } }, 1000);
  let pending = null;
  const soon = () => { if (pending) return; pending = requestAnimationFrame(() => { pending = null; drawEditor(); drawTerminal(); drawFeed(); drawStage(); }); };

  return {
    update(reason, m) {
      if (reason === "message" || reason === "message_update") {
        if (reason === "message" && m?.kind === "handoff" && m.id !== lastHandoffId) { lastHandoffId = m.id; animateHandoff(m); }
        soon();
      } else if (reason === "process" || reason === "task") drawStage();
      else if (reason === "loaded") drawAll();
    },
    destroy() { clearInterval(timer); if (pending) cancelAnimationFrame(pending); },
  };
}
