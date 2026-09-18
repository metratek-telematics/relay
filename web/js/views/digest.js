// Needs you items: every pending question, approval and decision, answerable in place. Mission Control shows them;
// the digest data behind them comes from orchestrator/autopilot.py.
import { $, $$, esc, icon, toast, md, timeAgo, fmtDur, throttle } from "../ui.js";
import { S, agentLabel, navigate } from "../state.js";
import { api } from "../api.js";
import { toolRequestActionsHtml, bindToolRequests } from "./tools.js";
import { mockupUrl } from "./mockups.js";

const KIND = {
  question: ["Question", "amber", "question"],
  escalation: ["Decision", "amber", "flag"],
  approval: ["Approval", "purple", "shield"],
  design_approval: ["Design approval", "purple", "layers"],
  design_pick: ["Design direction", "purple", "image"],
  parked: ["Paused", "amber", "pause"],
  blocked: ["Blocked", "red", "alert"],
  tool_request: ["Tool request", "blue", "package"],
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
  const who = x.kind === "tool_request" ? `${esc(agentLabel(x.agent))} (${esc(ROLE[x.from] || x.from || "agent")}) asks for a tool` : x.kind === "question" ? `${esc(agentLabel(x.agent))} (${esc(ROLE[x.from] || x.from || "agent")}) asks` : x.kind === "escalation" ? "Relay's judge needs a decision" : x.kind === "approval" ? "Ready to deliver" : x.kind === "design_approval" ? `System design v${esc(x.design_version || 1)} is reviewed and waits for you before any code is written` : x.kind === "design_pick" ? "The focus group scored the mockups; pick the one to build" : "";
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
  } else if (x.kind === "design_approval") {
    actions = `${x.summary ? `<div class="nx-summary">${md(x.summary)}</div>` : ""}
      ${x.design_md ? `<details class="nx-design"><summary>${icon("layers", "sm")}Read the design</summary><div class="doc md nx-design-body">${md(x.design_md)}</div></details>` : ""}
      <div class="nx-reply"><textarea class="input" rows="1" data-answer-text placeholder="What should change? (required to request changes)" aria-label="Note"></textarea></div>
      <div class="nx-opts"><button type="button" class="btn sm primary" data-approve data-design>${icon("check")}Approve design</button><button type="button" class="btn sm" data-reject>${icon("x")}Request changes</button><a class="btn sm ghost" href="#/task/${esc(x.task_id)}/design">${icon("external", "sm")}Design tab</a></div>`;
  } else if (x.kind === "design_pick") {
    const ex = x.exploration || {};
    actions = `<ul class="nx-dirs">${(ex.directions || []).map((d) => `<li class="nx-dir ${d.id === ex.winner ? "is-lead" : ""}">
        ${d.shot ? `<a class="nx-dir-shot" href="#/task/${esc(x.task_id)}/mockups" aria-label="See direction ${esc(d.id)} in the Mockups tab"><img src="${esc(mockupUrl(x.task_id, d.shot))}" alt="" loading="lazy"></a>` : ""}
        <div class="nx-dir-body"><b><span class="mk-letter sm" aria-hidden="true">${esc(d.id)}</span> ${esc(d.title || "")}</b><span class="nx-dir-score mono">${d.mean != null ? `${Number(d.mean).toFixed(1)}/10` : ""}${d.id === ex.winner ? " · panel pick" : ""}</span></div>
        <button type="button" class="btn xs ${d.id === ex.winner ? "primary" : ""}" data-pick-dir="${esc(d.id)}">Build ${esc(d.id)}</button></li>`).join("")}</ul>
      ${x.auto ? `<div class="nx-auto">${icon("clock", "sm")}<span>If nobody answers${ex.timeout_minutes ? ` within ${esc(Math.round(ex.timeout_minutes))} minutes` : ""}, Relay builds <b>${esc(ex.winner || x.auto)}</b>.</span></div>` : ""}
      <div class="nx-opts"><a class="btn sm ghost" href="#/task/${esc(x.task_id)}/mockups">${icon("image", "sm")}Compare in the Mockups tab</a></div>`;
  } else if (x.kind === "parked") {
    actions = `<div class="nx-opts"><button type="button" class="btn sm primary" data-resume>${icon("play")}Resume</button><button type="button" class="btn sm danger" data-stop>${icon("stop")}Stop</button></div>`;
  } else if (x.kind === "tool_request") {
    actions = toolRequestActionsHtml(x);
  } else if (x.kind === "blocked" && x.blocker) {
    actions = `<div class="nx-opts"><button type="button" class="btn sm primary" data-retry-dep="${esc(x.blocker.id)}">${icon("retry")}Retry ${esc(x.blocker.label)}</button><button type="button" class="btn sm" data-drop-dep="${esc(x.blocker.id)}">${icon("x")}Run without it</button></div>`;
  }
  const question = x.kind === "approval" || x.kind === "design_approval" || x.kind === "design_pick" ? "" : `<div class="nx-q">${md(x.question || "")}</div>`;
  return `<article class="nx-item" data-inbox="${esc(x.id)}" data-task="${esc(x.task_id)}" data-kind="${esc(x.kind)}">
    <header class="nx-head"><span class="badge ${tone}">${icon(ic, "sm")}${kindLabel}</span>${taskRef(x)}<span class="nx-meta">${esc(x.repo || "")}${x.time ? ` · ${esc(timeAgo(x.time))}` : ""}</span></header>
    ${who ? `<div class="nx-who">${who}</div>` : ""}
    ${question}
    ${(x.blocks || []).length ? `<div class="nx-blocks">${icon("layers", "sm")}<span>Holding up ${esc(x.blocks.slice(0, 3).join(", "))}${x.blocks.length > 3 ? ` +${x.blocks.length - 3}` : ""}</span></div>` : ""}
    ${actions}
  </article>`;
}

export function bindInbox(root, items, onDone) {
  bindToolRequests(root, onDone);
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
  $$("[data-approve]", root).forEach((b) => (b.onclick = () => { const x = find(b); const note = $("[data-answer-text]", b.closest(".nx-item"))?.value.trim() || ""; run(b, () => api.action(x.task_id, "approve", { note }), b.hasAttribute("data-design") ? "Design approved" : "Delivery approved"); }));
  $$("[data-reject]", root).forEach((b) => (b.onclick = () => {
    const x = find(b); const ta = $("[data-answer-text]", b.closest(".nx-item"));
    if (!ta.value.trim()) { ta.focus(); return toast("warning", "Say what should change"); }
    run(b, () => api.action(x.task_id, "reject", { note: ta.value.trim() }), "Changes requested");
  }));
  $$("[data-pick-dir]", root).forEach((b) => (b.onclick = () => { const x = find(b); if (x) run(b, () => api.chooseDirection(x.task_id, b.dataset.pickDir), `Building ${b.dataset.pickDir}`); }));
  $$("[data-resume]", root).forEach((b) => (b.onclick = () => { const x = find(b); run(b, () => api.action(x.task_id, "resume"), "Resumed"); }));
  $$("[data-stop]", root).forEach((b) => (b.onclick = () => { const x = find(b); run(b, () => api.action(x.task_id, "stop"), "Stopping"); }));
  $$("[data-retry-dep]", root).forEach((b) => (b.onclick = () => run(b, () => api.action(b.dataset.retryDep, "retry"), "Requeued")));
  $$("[data-drop-dep]", root).forEach((b) => (b.onclick = () => {
    const x = find(b); const t = S.tasks.get(x.task_id);
    const deps = (t?.depends_on || []).filter((d) => d !== b.dataset.dropDep);
    run(b, () => api.updateTask(x.task_id, { depends_on: deps }), "Dependency removed");
  }));
}
