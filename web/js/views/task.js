// Task workspace: header, agent roster, conversation, composer, inspector.
import { $, $$, el, esc, icon, fmtSec, fmtDur, fmtNum, fmtCost, timeAgo, basename, toast, confirm, menu, copyText } from "../ui.js";
import { S, agentLabel, agentInitial, ROLE_LABEL, roleAgent, roleModel, roleEffort, statusOf, LIVE, msgStore, taskElapsed, navigate, bus } from "../state.js";
import { api } from "../api.js";
import { Conversation } from "./conversation.js";
import { mountInspector, TABS } from "./inspector.js";
import { openNewTask } from "./newtask.js";

const PHASES = [["kickoff", "Plan"], ["dialogue", "Work"], ["review", "Review"], ["deliver", "Deliver"], ["done", "Done"]];

export function mountTask(main, id) {
  const getTask = () => S.tasks.get(id);
  let t = getTask();
  if (!t) { main.innerHTML = `<div class="page"><div class="empty">${icon("alert", "lg")}<h3>Task not found</h3><p>It may have been deleted.</p><p><a href="#/">Back to dashboard</a></p></div></div>`; return { update() {}, destroy() {} }; }

  main.innerHTML = `<div class="ws">
    <header class="ws-head"><div class="ws-title" id="wsTitle"></div><div class="ws-actions" id="wsActions"></div></header>
    <section class="roster" id="roster"></section>
    <div class="ws-body ${S.ui.inspector ? "" : "inspector-hidden"}" id="wsBody">
      <div class="convo-pane">
        <div class="convo-head"><span class="row" style="gap:10px">Team conversation<button class="files-chip" id="filesChip" hidden></button></span><div class="convo-toolbar">
          <button class="btn xs ${S.ui.showTools ? "" : "ghost"}" id="toggleTools" title="Show tool calls">${icon("terminal")}Tools</button>
          <button class="btn xs ${S.ui.showThinking ? "" : "ghost"}" id="toggleThinking" title="Show agent reasoning">${icon("brain")}Reasoning</button>
          <button class="btn xs ${S.ui.showGit ? "" : "ghost"}" id="toggleGit" title="Show the orchestrator's git and GitHub commands">${icon("branch")}Git</button>
        </div></div>
        <div class="convo-wrap">
          <div class="convo ${S.ui.showTools ? "" : "hide-tools"} ${S.ui.showThinking ? "" : "hide-thinking"} ${S.ui.showGit ? "" : "hide-git"}" id="convo"></div>
          <button class="btn sm primary jump-latest" id="jumpLatest" hidden>${icon("chevronDown")}Jump to latest</button>
        </div>
        <div class="composer" id="composer"></div>
      </div>
      <aside class="inspector" id="inspector"></aside>
    </div>
  </div>`;

  const convo = new Conversation($("#convo", main), getTask);
  convo.setJumpButton($("#jumpLatest", main));
  const insp = mountInspector($("#inspector", main), getTask);
  if (S.route.tab && TABS.some(([k]) => k === S.route.tab)) insp.setTab(S.route.tab);

  $("#toggleTools", main).onclick = () => { S.ui.showTools = !S.ui.showTools; $("#convo", main).classList.toggle("hide-tools", !S.ui.showTools); $("#toggleTools", main).classList.toggle("ghost", !S.ui.showTools); };
  $("#toggleGit", main).onclick = () => { S.ui.showGit = !S.ui.showGit; $("#convo", main).classList.toggle("hide-git", !S.ui.showGit); $("#toggleGit", main).classList.toggle("ghost", !S.ui.showGit); };
  main.addEventListener("click", (e) => { const b = e.target.closest("[data-open-tab]"); if (b) { if (!S.ui.inspector) $("#inspToggle", main).click(); insp.setTab(b.dataset.openTab); } });
  $("#toggleThinking", main).onclick = () => { S.ui.showThinking = !S.ui.showThinking; $("#convo", main).classList.toggle("hide-thinking", !S.ui.showThinking); $("#toggleThinking", main).classList.toggle("ghost", !S.ui.showThinking); };

  // ---------------------------------------------------------------- header
  function renderHeader() {
    t = getTask(); if (!t) return;
    const st = statusOf(t);
    const live = LIVE.has(t.status);
    const wf = t.workflow || {};
    const chain = ["supervisor", "worker", "reviewer"].filter((r) => roleAgent(t, r)).map((r) => agentLabel(roleAgent(t, r))).join(" → ");
    $("#wsTitle", main).innerHTML = `
      <div class="crumbs">${icon("folder")}<span>${esc(basename(t.repo))}</span>${t.branch ? `<span>›</span>${icon("branch")}<span class="mono">${esc(t.branch)}</span>` : ""}</div>
      <h1><span class="truncate">${esc(t.name)}</span><span class="status-pill ${st.attention ? "needs" : ""}" title="${esc(t.detail || "")}"><span class="dot ${live ? "live" : ""}" style="background:${st.tone ? `var(--${st.tone === "accent" ? "accent" : st.tone})` : "var(--text-3)"}"></span><span>${esc(st.label)}</span></span></h1>
      <div class="ws-meta"><span>${icon("bot")}${esc(chain)}</span><span>${icon("clock")}${esc(t.detail || "")}</span>${t.pr_url ? `<a href="${esc(t.pr_url)}" target="_blank" rel="noopener">${icon("external")}PR #${esc(t.pr_number || "")}</a>` : ""}${t.github_issue_url ? `<a href="${esc(t.github_issue_url)}" target="_blank" rel="noopener">${icon("github")}Issue #${esc(t.github_issue_number)}</a>` : ""}<span title="${esc(t.created_at)}">created ${timeAgo(t.created_at)}</span></div>`;
    const active = live || t.status === "needs_input" || t.status === "paused";
    const paused = t.status === "paused" || t.pause_requested;
    $("#wsActions", main).innerHTML = `
      ${active ? (paused ? `<button class="btn sm" data-act="resume">${icon("play")}Resume</button>` : `<button class="btn sm" data-act="pause" title="Pause after the current agent turn">${icon("pause")}Pause</button>`) : ""}
      ${active ? `<button class="btn sm danger" data-act="stop">${icon("stop")}Stop</button>` : ""}
      ${["failed", "stopped", "interrupted"].includes(t.status) ? `<button class="btn sm primary" data-act="retry" title="${t.checkpoint ? "Resume from checkpoint using the same agent sessions" : "Start again"}">${icon("retry")}${t.checkpoint ? "Resume" : "Retry"}</button>` : ""}
      ${["queued", "draft"].includes(t.status) ? `<button class="btn sm primary" data-act="start" title="Start this task now, alongside anything already running">${icon("play")}Start now</button>` : ""}
      ${t.branch ? `<button class="btn sm ${t.status === "done" ? "primary" : ""}" data-open-tab="result" title="Commands to see, run, accept or clean up the result">${icon("play")}Try it</button>` : ""}
      ${t.pr_url ? `<a class="btn sm" href="${esc(t.pr_url)}" target="_blank" rel="noopener">${icon("external")}Open PR</a>` : ""}
      <button class="btn sm icon" id="moreBtn" title="More">${icon("more")}</button>
      <button class="btn sm icon ghost" id="inspToggle" title="Toggle inspector">${icon("panel")}</button>`;
    $$("[data-act]", main).forEach((b) => (b.onclick = () => act(b.dataset.act)));
    $("#moreBtn", main).onclick = (e) => menu(e.currentTarget, [
      { label: "Retry from scratch (new worktree)", icon: "retry", onClick: () => act("retry-fresh"), disabled: active },
      { label: "Duplicate task", icon: "duplicate", onClick: () => act("duplicate") },
      { label: "Edit task settings", icon: "edit", onClick: () => openNewTask({ edit: t }) },
      "-",
      { label: "Open worktree folder", icon: "folder", onClick: () => api.open(t.id, "worktree").catch((e) => toast("error", "Cannot open", e.message)) },
      { label: "Open in VS Code", icon: "code", onClick: () => api.open(t.id, "vscode").catch((e) => toast("error", "Cannot open", e.message)) },
      { label: "Open run folder (logs, artifacts)", icon: "file", onClick: () => api.open(t.id, "run").catch((e) => toast("error", "Cannot open", e.message)) },
      { label: "Export report (markdown)", icon: "download", onClick: () => { location.href = `/api/tasks/${encodeURIComponent(t.id)}/export`; } },
      { label: "Copy task id", icon: "copy", onClick: () => copyText(t.id) },
      "-",
      { label: t.archived ? "Unarchive" : "Archive", icon: "archive", onClick: () => act(t.archived ? "unarchive" : "archive") },
      { label: "Delete task…", icon: "trash", danger: true, onClick: () => act("delete") },
    ]);
    $("#inspToggle", main).onclick = () => { S.ui.inspector = !S.ui.inspector; $("#wsBody", main).classList.toggle("inspector-hidden", !S.ui.inspector); };
  }

  async function act(a) {
    t = getTask();
    try {
      if (a === "stop") { if (!(await confirm("Stop this task?", "The running agent process is terminated. You can resume later from the checkpoint.", { danger: true, okLabel: "Stop" }))) return; await api.action(t.id, "stop"); toast("info", "Stopping", t.name); }
      else if (a === "pause") { await api.action(t.id, "pause"); toast("info", "Pause requested", "Takes effect after the current agent turn."); }
      else if (a === "resume") { await api.action(t.id, "resume"); toast("success", "Resumed"); }
      else if (a === "retry") { await api.action(t.id, "retry"); toast("success", t.checkpoint ? "Resuming from checkpoint" : "Queued again"); }
      else if (a === "retry-fresh") { if (!(await confirm("Retry from scratch?", "A new worktree and new agent sessions are created. The current worktree is kept on disk.", { okLabel: "Retry fresh" }))) return; await api.action(t.id, "retry", { fresh: true }); }
      else if (a === "queue") { await api.action(t.id, "queue"); toast("success", "Queued"); }
      else if (a === "start") { await api.action(t.id, "start"); toast("success", "Started", "Running alongside any other active tasks."); }
      else if (a === "duplicate") { const n = await api.action(t.id, "duplicate"); toast("success", "Duplicated", n.name); navigate(`#/task/${n.id}`); }
      else if (a === "archive") { await api.action(t.id, "archive", { archived: true }); toast("info", "Archived"); }
      else if (a === "unarchive") { await api.action(t.id, "archive", { archived: false }); }
      else if (a === "delete") {
        if (!(await confirm("Delete this task?", "Conversation, artifacts and logs are removed. The git worktree is also removed.", { danger: true, okLabel: "Delete" }))) return;
        await api.deleteTask(t.id, true); toast("info", "Task deleted"); navigate("#/");
      }
    } catch (e) { toast("error", "Action failed", e.message); }
  }

  // ---------------------------------------------------------------- roster
  function renderRoster() {
    t = getTask(); if (!t) return;
    const p = t.process || {};
    const roles = ["supervisor", "worker", "reviewer"];
    const hasRev = !!roleAgent(t, "reviewer");
    const host = $("#roster", main);
    host.classList.toggle("two", !hasRev);
    const m = t.metrics?.roles || {};
    const cards = roles.filter((r) => r !== "reviewer" || hasRev).map((r) => {
      const a = roleAgent(t, r);
      const live = p.state === "running" && p.role === r;
      const mr = m[r] || {};
      const sess = (t.sessions || {})[r] || {};
      const color = (S.agentMeta[a] || {}).color || "var(--green)";
      const idleText = t.current_role === r && t.status !== "done" ? "waiting" : (sess.turns ? `${sess.turns} turn${sess.turns === 1 ? "" : "s"}` : "not started");
      return `<div class="rcard ${live ? "live" : ""}" style="--agent:${color}">
        <span class="av ${esc(a)}">${esc(agentInitial(a))}</span>
        <div class="truncate"><div class="role">${esc(ROLE_LABEL[r])}</div><div class="who">${esc(agentLabel(a))}<small>${esc(sess.model || roleModel(t, r) || "default model")}${roleEffort(t, r) ? ` · ${esc(roleEffort(t, r))}` : ""}</small></div><div class="meta">${fmtNum((mr.input || 0) + (mr.output || 0))} tok · ${fmtCost(mr.cost_usd, mr.estimated)} · ${mr.tool_calls || 0} tools</div></div>
        <div class="state">${live ? `LIVE<small>${fmtSec(p.elapsed)}${Number(p.silent_for || 0) > 15 ? ` · quiet ${fmtSec(p.silent_for)}` : ""}</small>` : `${esc(idleText.toUpperCase())}<small>${live ? "" : (mr.turns ? fmtDur(mr.seconds) : "")}</small>`}</div>
      </div>`;
    }).join("");
    const cp = t.checkpoint || {};
    const phase = t.status === "done" ? "done" : (cp.phase || (LIVE.has(t.status) ? "kickoff" : ""));
    const idx = PHASES.findIndex(([k]) => k === phase);
    const failed = ["failed", "stopped", "interrupted"].includes(t.status);
    const bars = PHASES.map(([k], i) => `<span class="${i < idx || phase === "done" ? "done" : i === idx && phase !== "done" ? (failed ? "fail" : "cur") : ""}" title="${esc(k)}"></span>`).join("");
    const turn = cp.turn || 0;
    const tot = t.metrics?.total || {};
    host.innerHTML = cards + `<div class="rprogress">
      <div class="top"><span>${phase ? `${esc(PHASES[Math.max(0, idx)][1])}${phase === "review" && cp.review_round ? ` · round ${cp.review_round}` : ""}` : "Not started"}</span><b>${turn ? `Work package ${turn}/${(t.workflow || {}).max_turns || "—"}` : ""}</b></div>
      <div class="phases">${bars}</div>
      <div class="foot"><span>${fmtSec(taskElapsed(t))} elapsed</span><span title="${tot.estimated ? "~ = estimated at API-equivalent rates (subscription usage is not billed per token)" : "reported by the CLI"}">${tot.turns || 0} turns · ${fmtCost(tot.cost_usd, tot.estimated)}</span></div>
    </div>`;
  }

  // ---------------------------------------------------------------- composer
  function renderComposer() {
    t = getTask(); if (!t) return;
    const host = $("#composer", main);
    const pending = t.pending;
    const answerMode = pending && pending.kind === "question";
    const approvalMode = pending && pending.kind === "approval";
    const terminal = ["done", "failed", "stopped", "interrupted", "draft"].includes(t.status);
    const draft = host.querySelector("textarea")?.value || "";
    host.classList.toggle("answer-mode", !!answerMode);
    host.innerHTML = `
      ${answerMode ? `<div class="mode-note">${icon("question", "sm")}Answering ${esc(agentLabel(pending.agent))}'s question — your message is delivered immediately.</div>` : ""}
      ${approvalMode ? `<div class="mode-note" style="color:var(--green)">${icon("shield", "sm")}Delivery is waiting for your approval. Use the card above, or approve here.</div>` : ""}
      <div class="box">
        <textarea id="guidance" rows="1" placeholder="${answerMode ? "Type your answer…" : terminal ? "Add guidance for when this task resumes…" : "Guide the team… e.g. “use the existing DateService instead of a new helper”"}">${esc(draft)}</textarea>
        <div class="bar">
          ${answerMode ? "" : `<select id="gTo" title="Deliver to"><option value="next">Next turn (whoever runs next)</option><option value="supervisor">Supervisor · ${esc(agentLabel(roleAgent(t, "supervisor")))}</option><option value="worker">Worker · ${esc(agentLabel(roleAgent(t, "worker")))}</option><option value="both">Both</option></select>
          <div class="seg" id="gMode"><button data-m="queue" class="active" title="Delivered at the next safe agent boundary">Queue</button><button data-m="interrupt" title="Stop the current agent turn now and redirect it">Interrupt now</button></div>`}
          <span class="spacer"></span>
          ${approvalMode ? `<button class="btn sm danger" id="rejectBtn">Request changes</button><button class="btn sm primary" id="approveBtn">${icon("check")}Approve</button>` : `<button class="btn sm primary" id="sendBtn">${icon("send")}${answerMode ? "Answer" : "Send"}</button>`}
        </div>
      </div>
      <div class="foot"><span><kbd>Ctrl</kbd>+<kbd>Enter</kbd> to send${answerMode ? "" : " · queued guidance is injected into the next agent prompt"}</span><span id="composerHint"></span></div>`;
    const ta = $("#guidance", host);
    const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(180, ta.scrollHeight) + "px"; };
    ta.addEventListener("input", grow); grow();
    let mode = "queue";
    $$("#gMode button", host).forEach((b) => (b.onclick = () => { mode = b.dataset.m; $$("#gMode button", host).forEach((x) => x.classList.toggle("active", x === b)); }));
    const send = async () => {
      const text = ta.value.trim();
      if (!text) return;
      const btn = $("#sendBtn", host); if (btn) btn.disabled = true;
      try {
        if (answerMode) { await api.action(t.id, "answer", { id: pending.id, text }); toast("success", "Answer delivered"); }
        else {
          const to = $("#gTo", host)?.value || "next";
          const r = await api.action(t.id, "guidance", { text, to, mode });
          toast("success", r.applied === "interrupt" ? "Interrupting the current turn" : r.applied === "answer" ? "Delivered as the answer" : "Guidance queued", r.applied === "interrupt" ? "The agent restarts with your note." : r.applied === "next_boundary" ? `Delivered to the ${to === "next" ? "next agent turn" : to}.` : "");
        }
        ta.value = ""; grow();
      } catch (e) { toast("error", "Could not send", e.message); }
      finally { if (btn) btn.disabled = false; }
    };
    $("#sendBtn", host) && ($("#sendBtn", host).onclick = send);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (approvalMode) $("#approveBtn", host).click(); else send(); } });
    if (approvalMode) {
      $("#approveBtn", host).onclick = async () => { try { await api.action(t.id, "approve", { note: ta.value.trim() }); toast("success", "Approved"); } catch (e) { toast("error", "Failed", e.message); } };
      $("#rejectBtn", host).onclick = async () => { const note = ta.value.trim(); if (!note) { toast("warning", "Add a note", "Describe the changes you want."); ta.focus(); return; } try { await api.action(t.id, "reject", { note }); toast("info", "Changes requested"); ta.value = ""; } catch (e) { toast("error", "Failed", e.message); } };
    }
  }

  // ---------------------------------------------------------------- messages
  async function loadMessages() {
    const store = msgStore(id);
    if (!store.loaded) {
      try {
        const r = await api.messages(id, 0, 3000);
        store.list = r.messages || [];
        store.byId = new Map(store.list.map((m) => [m.id, m]));
        store.count = r.count || store.list.length;
        store.loaded = true;
      } catch (e) { $("#convo", main).innerHTML = `<div class="empty small">${esc(e.message)}</div>`; return; }
    }
    if (S.route.id !== id) return;
    convo.render(store.list);
    renderFilesChip();
  }

  // Which files the agents have edited, taken from their edit and shell calls.
  function renderFilesChip() {
    const chip = $("#filesChip", main);
    if (!chip) return;
    const files = convo.filesTouched();
    chip.hidden = !files.length;
    if (!files.length) return;
    chip.innerHTML = `${icon("edit", "sm")}${files.length} file${files.length === 1 ? "" : "s"} edited`;
    chip.title = files.slice(0, 25).join("\n") + (files.length > 25 ? `\n…and ${files.length - 25} more` : "");
    chip.onclick = () => insp.setTab("changes");
  }

  renderHeader(); renderRoster(); renderComposer(); loadMessages();
  const timer = setInterval(() => { t = getTask(); if (t && (LIVE.has(t.status) || t.process?.state === "running")) { renderRoster(); convo.updateTyping(); } }, 1000);
  const offRoute = bus.on("route", () => { if (S.route.id === id && S.route.tab && S.route.tab !== insp.tab) insp.setTab(S.route.tab); });

  return {
    update(reason, payload) {
      t = getTask(); if (!t) return;
      if (reason === "task") {
        const pendingChanged = payload?.pendingChanged;
        renderHeader(); renderRoster();
        if (pendingChanged || payload?.statusChanged) { renderComposer(); convo.refreshQuestions(); }
        convo.updateTyping();
        insp.refresh("task");
      } else if (reason === "message") { convo.append(payload); convo.updateTyping(); renderFilesChip(); }
      else if (reason === "message_update") { convo.patch(payload); convo.updateTyping(); renderFilesChip(); }
      else if (reason === "process") { renderRoster(); convo.updateTyping(); }
      else if (reason === "event") { insp.refresh("event"); }
      else if (reason === "artifact") { insp.refresh("artifact"); }
    },
    destroy() { clearInterval(timer); insp.destroy(); offRoute(); },
  };
}
