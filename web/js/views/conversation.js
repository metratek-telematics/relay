// Live multi-agent conversation renderer with incremental DOM updates.
import { $, $$, el, esc, icon, md, fmtTime, fmtSec, fmtDur, copyText, diffHtml } from "../ui.js";
import { S, agentLabel, agentInitial, ROLE_LABEL, roleAgent } from "../state.js";
import { api } from "../api.js";
import { toast } from "../ui.js";
import { openFollowUp } from "./newtask.js";
import { packetHtml, blockedHtml } from "../packet.js";

// ---------------------------------------------------------------------------- edits
// Agents change files in two ways: an edit tool, whose input carries the file and the
// exact text, or a shell command such as `sed -i`. Both become something a person can
// read at a glance: which file, how many lines, and the diff when it is known.

function relPath(p, t) {
  const s = String(p || "").replace(/\\/g, "/");
  const roots = [t?.worktree, t?.repo].filter(Boolean).map((r) => String(r).replace(/\\/g, "/").replace(/\/+$/, ""));
  for (const r of roots) if (s.toLowerCase().startsWith(r.toLowerCase() + "/")) return s.slice(r.length + 1);
  return s;
}

function parseInput(raw) {
  if (!raw) return null;
  if (typeof raw === "object") return raw;
  try { return JSON.parse(raw); } catch { return null; }
}

function linesOf(txt) { return txt ? String(txt).replace(/\r\n?/g, "\n").split("\n") : []; }

const EDIT_TOOLS = new Set(["edit", "write", "multiedit", "notebookedit", "write_file", "replace", "create_file", "apply_patch"]);

// Returns { files, add, del, diff, created } for an edit tool call, or null.
export function describeEdit(m, t) {
  const tool = String(m.tool || "").toLowerCase();
  if (!EDIT_TOOLS.has(tool) && m.category !== "edit") return null;
  const inp = parseInput(m.input);
  if (!inp) {
    // Stored input is capped in size, so very large edits may not parse; the path still can.
    const f = (String(m.input || "").match(/"(?:file_path|path|absolute_path)"\s*:\s*"([^"]+)"/) || [])[1] || m.summary;
    return f ? { files: [relPath(f.replace(/\\\\/g, "\\"), t)], add: 0, del: 0, diff: "" } : null;
  }
  const file = inp.file_path || inp.path || inp.absolute_path || inp.notebook_path;
  const out = { files: file ? [relPath(file, t)] : [], add: 0, del: 0, diff: "", created: false };
  const hunk = (oldT, newT) => {
    const o = linesOf(oldT), n = linesOf(newT);
    out.del += o.length;
    out.add += n.length;
    return [...o.map((l) => "-" + l), ...n.map((l) => "+" + l)].join("\n");
  };
  if (Array.isArray(inp.edits)) {
    out.diff = inp.edits.map((e) => "@@ edit @@\n" + hunk(e.old_string, e.new_string)).join("\n");
  } else if ("old_string" in inp || "new_string" in inp) {
    out.diff = hunk(inp.old_string, inp.new_string);
  } else if (typeof inp.content === "string") {
    const ls = linesOf(inp.content);
    out.add = ls.length;
    out.diff = ls.slice(0, 400).map((l) => "+" + l).join("\n");
    out.created = true;
  } else if (Array.isArray(inp.changes)) { // Codex file_change
    out.files = inp.changes.map((c) => relPath(c.path, t) + (c.kind && c.kind !== "update" ? ` (${c.kind})` : ""));
  }
  return out;
}

// Recognises in-place edits hidden inside shell commands and returns their target files.
export function shellEditTargets(cmd, t) {
  const c = String(cmd || "");
  const found = new Set();
  const add = (x) => {
    x = String(x || "").replace(/^['"]|['"]$/g, "");
    // Only plain path-shaped tokens: no brackets, quotes, '=' or other script syntax.
    if (!/^[\w.@~\\/:-]+$/.test(x) || !/\.[A-Za-z0-9]{1,8}$/.test(x)) return;
    if (!x || x.startsWith("-")) return;
    if (/\.(log|tmp|out)$/i.test(x)) return; // command output, not an edit to the project
    const rel = relPath(x, t);
    // An absolute path that stayed absolute lies outside the project, e.g. a scratch file in /tmp.
    if (/^([A-Za-z]:)?[\\/]/.test(rel)) return;
    found.add(rel);
  };
  for (const m of c.matchAll(/\bsed\s+(?:-[A-Za-z]+\s+)*-i\S*\s+(?:-e\s+)?(?:'[^']*'|"[^"]*"|\S+)\s+([^;&|]+)/g)) m[1].trim().split(/\s+/).forEach(add);
  for (const m of c.matchAll(/\bperl\s+-\S*i\S*\s+(?:-e\s+)?(?:'[^']*'|"[^"]*")\s+([^\s;&|]+)/g)) add(m[1]);
  for (const m of c.matchAll(/(?:Set-Content|Add-Content|Out-File)\b[^;|]*?-(?:Path|FilePath)\s+['"]?([^'"\s;|]+)/gi)) add(m[1]);
  for (const m of c.matchAll(/\btee\s+(?:-a\s+)?([^\s;&|]+)/g)) add(m[1]);
  // A real redirect has whitespace or a quote before '>' (so `a>b` in code is ignored).
  for (const m of c.matchAll(/(?:^|[\s"'])>>?\s*['"]?([\w.@~\\/:-]+\.[A-Za-z0-9]{1,8})(?=[\s'";&|]|$)/g)) if (!m[1].startsWith("/dev/")) add(m[1]);
  return [...found];
}

const TOOL_ICON = { shell: "terminal", read: "eye", edit: "edit", search: "search", web: "globe", agent: "bot", plan: "list", mcp: "zap", tool: "cpu" };

function whoAv(m, size = "") {
  const a = m.agent || m.role || "system";
  const cls = m.agent || (m.role === "user" ? "user" : (m.role === "verify" ? "verify" : (m.role === "git" || m.role === "github" ? "git" : "system")));
  return `<span class="av ${esc(cls)} ${size}" title="${esc(agentLabel(a))}">${esc(agentInitial(a))}</span>`;
}
function whoName(m) {
  if (m.agent) return `${esc(agentLabel(m.agent))} <span class="role-tag">· ${esc(ROLE_LABEL[m.role] || m.role || "")}</span>`;
  return esc(ROLE_LABEL[m.role] || m.role || "Orchestrator");
}
function roleAv(t, role) {
  const a = role === "user" ? "user" : role === "orchestrator" ? "system" : roleAgent(t, role) || "system";
  return `<span class="av sm ${esc(a)}" title="${esc(ROLE_LABEL[role] || role)}">${esc(agentInitial(a))}</span>`;
}
function clampBody(html, id) {
  return `<div class="hcard-body md clamp" data-clamp="${id}">${html}</div><button class="hcard-more" data-more="${id}">Show more</button>`;
}

// Replace protocol envelopes (fenced ```json {"type":...}```) inside agent text with a compact chip.
const ENV_FENCE = /```(?:json|JSON)?\s*\n([\s\S]*?)\n\s*```/g;
function stripEnvelopes(text) {
  let chips = "";
  const out = String(text || "").replace(ENV_FENCE, (all, inner) => {
    try {
      const obj = JSON.parse(inner);
      if (obj && typeof obj === "object" && obj.type) {
        const tag = obj.type + (obj.decision ? ` · ${obj.decision}` : obj.status ? ` · ${obj.status}` : obj.verdict ? ` · ${obj.verdict}` : "");
        chips += `<span class="badge outline env-chip" title="Protocol envelope parsed by the orchestrator">${icon("package", "sm")}${esc(tag)}</span> `;
        return "";
      }
    } catch {}
    return all;
  });
  return { text: out.trim(), chips };
}

export function renderMessage(m, t, prevInfo) {
  const time = fmtTime(m.time || m.ts);
  const k = m.kind;
  const cont = prevInfo && prevInfo.role === m.role && prevInfo.agent === m.agent && (prevInfo.kind === "text" || prevInfo.kind === "tool" || prevInfo.kind === "thinking") && (k === "text" || k === "tool" || k === "thinking");
  const idAttr = `data-id="${esc(m.id)}" data-kind="${esc(k)}"`;

  if (k === "text") {
    const { text, chips } = m.streaming ? { text: m.content || "", chips: "" } : stripEnvelopes(m.content);
    return `<div class="m m-text ${cont ? "cont" : ""}" ${idAttr}>
      <div class="m-head">${whoAv(m)}<strong>${whoName(m)}</strong><time>${time}</time></div>
      <div class="m-body md">${md(text)}${chips ? `<div class="env-chips">${chips}</div>` : ""}${m.streaming ? '<span class="typing-caret">▍</span>' : ""}</div>
    </div>`;
  }
  if (k === "thinking") {
    return `<div class="m m-thinking ${cont ? "cont" : ""}" ${idAttr}>
      <div class="m-head">${whoAv(m)}<strong>${whoName(m)}</strong><time>${time}</time></div>
      <button class="thinking" data-toggle="think">${icon("brain", "sm")}Reasoning</button>
      <div class="thinking-body" hidden>${esc(m.content || "")}</div>
    </div>`;
  }
  if (k === "tool") {
    const cat = m.category || "tool";
    const st = m.status || "running";
    const runFor = m.ts ? ` ${fmtSec(Date.now() / 1000 - m.ts)}` : "";
    const stHtml = st === "running" ? `${icon("spinner", "spin")} running${runFor}` : st === "error" ? `${icon("x")} failed${m.duration ? ` · ${fmtDur(m.duration)}` : ""}` : `${icon("check")}${m.duration ? ` ${fmtDur(m.duration)}` : ""}`;
    const head = `<div class="m-head">${whoAv(m)}<strong>${whoName(m)}</strong><time>${time}</time></div>`;
    const rawDetail = `${m.input ? `<div class="td-lbl">Input</div><pre>${esc(m.input)}</pre>` : ""}${m.output !== undefined && m.output !== null ? `<div class="td-lbl">Output</div><pre>${esc(m.output || "(empty)")}</pre>` : ""}`;

    const ed = describeEdit(m, t);
    if (ed && ed.files.length) {
      const counts = (ed.add || ed.del) ? `<span class="diffstat"><span class="a">+${ed.add}</span> <span class="d">&minus;${ed.del}</span></span>` : "";
      // Small diffs open by default, so you see the change without a click.
      const open = ed.diff && linesOf(ed.diff).length <= 40;
      return `<div class="m m-tool m-edit ${cont ? "cont" : ""}" ${idAttr}>
        ${head}
        <div class="tool-row edit ${esc(st)}" data-toggle="tool" title="${esc(ed.files.join(", "))}">
          <span class="tic">${icon("edit")}</span>
          <span class="truncate"><span class="tname">${ed.created ? "Wrote" : "Edited"}</span><span class="tsum">${esc(ed.files.join(", "))}</span></span>
          <span class="tst">${counts} ${stHtml}</span>
        </div>
        <div class="tool-detail edit-detail" ${open ? "" : "hidden"}>
          ${ed.diff ? diffHtml(ed.diff) : ""}
          ${st === "error" ? rawDetail : ""}
        </div>
      </div>`;
    }

    const shellTargets = cat === "shell" ? shellEditTargets(m.input || m.summary, t) : [];
    const label = shellTargets.length
      ? `<span class="tname">Edited via shell</span><span class="tsum">${esc(shellTargets.join(", "))}</span>`
      : `<span class="tname">${esc(m.tool || "tool")}</span><span class="tsum">${esc(m.summary || "")}</span>`;
    return `<div class="m m-tool ${shellTargets.length ? "m-edit" : ""} ${cont ? "cont" : ""}" ${idAttr}>
      ${head}
      <div class="tool-row ${shellTargets.length ? "edit" : esc(cat)} ${esc(st)}" data-toggle="tool" title="${esc(m.summary || "")}">
        <span class="tic">${icon(shellTargets.length ? "edit" : (TOOL_ICON[cat] || "cpu"))}</span>
        <span class="truncate">${label}</span>
        <span class="tst">${stHtml}</span>
      </div>
      <div class="tool-detail" hidden>${rawDetail}</div>
    </div>`;
  }
  if (k === "command") {
    const st = m.status || "running";
    const stHtml = st === "running" ? `${icon("spinner", "spin")} running` : st === "error" ? `${icon("x")} exit ${m.rc ?? "?"}${m.duration ? ` · ${fmtDur(m.duration)}` : ""}` : `${icon("check")}${m.duration ? ` ${fmtDur(m.duration)}` : ""}`;
    return `<div class="m m-cmd m-tool ${m.role === "git" || m.role === "github" ? "m-git" : ""}" ${idAttr}>
      <div class="tool-row shell ${esc(st)}" data-toggle="tool">
        <span class="tic">${icon("terminal")}</span>
        <span class="truncate"><span class="tname">${esc(ROLE_LABEL[m.role] || m.role)}</span><span class="tsum">$ ${esc(m.content || m.title || "")}</span></span>
        <span class="tst">${stHtml}</span>
      </div>
      <div class="tool-detail" hidden>${m.output ? `<div class="td-lbl">Output</div><pre>${esc(m.output)}</pre>` : '<pre class="muted">(no output yet)</pre>'}</div>
    </div>`;
  }
  if (k === "handoff") {
    const sub = m.subtype || "instruction";
    const cls = { revise: "revise", question: "question", report: "report", review: "review", review_request: "review", briefing: "plan", answer: "report" }[sub] || "";
    const body = md(m.content || "");
    const long = (m.content || "").length > 700;
    const files = (m.files || []).length ? `<div class="files">${m.files.slice(0, 40).map((f) => `<code>${esc(f)}</code>`).join("")}</div>` : "";
    const statusBadge = m.status ? `<span class="badge ${m.status === "complete" ? "green" : m.status === "blocked" ? "red" : "amber"}">${esc(m.status)}</span>` : "";
    const findings = ((m.findings || []).length ? `<ul class="findings">${m.findings.map((f) => `<li class="${esc(f.severity || "blocking")}"><code>${esc(f.file || "")}</code> ${esc(f.problem || "")}${f.fix ? `<div class="fix">Fix: ${esc(f.fix)}</div>` : ""}</li>`).join("")}</ul>` : "")
      + ((m.blocked_checks || []).length ? `<h4 style="margin-top:10px">Could not run</h4>${blockedHtml(m.blocked_checks)}` : "");
    return `<div class="m m-handoff" ${idAttr}>
      <div class="hcard ${cls}">
        <div class="hcard-head">
          <span class="flow">${roleAv(t, m.role)}${icon("arrowRight", "sm")}${roleAv(t, m.to || "worker")}</span>
          <span class="ttl">${esc(m.title || "Message")}</span>${statusBadge}<time>${time}</time>
        </div>
        ${m.summary ? `<div class="hcard-body" style="padding-bottom:0"><div class="summary">${esc(m.summary)}</div></div>` : ""}
        ${long ? clampBody(body + files + findings, m.id) : `<div class="hcard-body md">${body}${files}${findings}</div>`}
      </div>
    </div>`;
  }
  if (k === "plan") {
    const packet = packetHtml(m);
    return `<div class="m m-handoff" ${idAttr}>
      <div class="hcard plan">
        <div class="hcard-head"><span class="flow">${whoAv(m, "sm")}</span><span class="ttl">Plan agreed${m.summary ? ` · ${esc(m.summary)}` : ""}</span><span class="badge blue">plan</span><time>${time}</time></div>
        ${clampBody(md(m.content || "") + packet, m.id)}
      </div>
    </div>`;
  }
  if (k === "decision") {
    const done = m.decision === "done";
    return `<div class="m m-handoff" ${idAttr}>
      <div class="hcard ${done ? "done" : "revise"}">
        <div class="hcard-head"><span class="flow">${whoAv(m, "sm")}</span><span class="ttl">${done ? "Supervisor: task complete" : "Supervisor: revision requested"}</span><span class="badge ${done ? "green" : "amber"}">${done ? "done" : "revise"}</span><time>${time}</time></div>
        ${m.summary ? `<div class="hcard-body" style="padding-bottom:0"><div class="summary">${esc(m.summary)}</div></div>` : ""}
        <div class="hcard-body md">${md(m.content || "")}</div>
      </div>
    </div>`;
  }
  if (k === "review") {
    const pass = m.verdict === "PASS";
    const findings = (m.findings || []).length ? `<ul class="findings">${m.findings.map((f) => `<li class="${esc(f.severity || "blocking")}"><code>${esc(f.file || "")}</code> ${esc(f.problem || "")}${f.fix ? `<div class="fix">Fix: ${esc(f.fix)}</div>` : ""}</li>`).join("")}</ul>` : "";
    return `<div class="m m-handoff" ${idAttr}>
      <div class="hcard ${pass ? "done" : "error"}">
        <div class="hcard-head"><span class="flow">${whoAv(m, "sm")}</span><span class="ttl">Independent review · ${pass ? "approved" : "blocked"}</span><span class="badge ${pass ? "green" : "red"}">${esc(m.verdict || "")}</span><time>${time}</time></div>
        ${m.summary ? `<div class="hcard-body" style="padding-bottom:0"><div class="summary">${esc(m.summary)}</div></div>` : ""}
        <div class="hcard-body md">${findings || md(m.content || "")}</div>
      </div>
    </div>`;
  }
  if (k === "question" || k === "approval") {
    const pending = t?.pending && (t.pending.id === m.qid) && !m.answered;
    const isApproval = k === "approval";
    const opts = (m.options || []).length && pending ? `<div class="opts">${m.options.map((o) => `<button class="btn sm" data-opt="${esc(o)}">${esc(o)}</button>`).join("")}</div>` : "";
    const ds = m.diffstat ? `<span class="diffstat">${m.diffstat.files} files <span class="a">+${m.diffstat.insertions}</span> <span class="d">−${m.diffstat.deletions}</span></span>` : "";
    const files = (m.files || []).length ? `<div class="files" style="margin-top:8px">${m.files.slice(0, 30).map((f) => `<code>${esc(f)}</code>`).join("")}</div>` : "";
    const reply = pending ? (isApproval
      ? `<div class="reply"><textarea data-answer placeholder="Optional note for the team (required if requesting changes)…"></textarea><div class="stack"><button class="btn primary" data-approve="1">${icon("check")}Approve & deliver</button><button class="btn danger" data-approve="0">Request changes</button></div></div>`
      : `<div class="reply"><textarea data-answer placeholder="Type your answer… (Ctrl+Enter to send)"></textarea><button class="btn primary" data-send-answer>${icon("send")}Answer</button></div>`) : "";
    const answered = m.answered ? `<div class="answered"><b>${isApproval ? (m.approved === false ? "Changes requested" : "Approved") : "You answered"}:</b> ${esc(m.answer || "(no text)")}</div>` : "";
    return `<div class="m" ${idAttr}>
      <div class="qcard ${isApproval ? "approval" : ""} ${pending ? "pending" : ""}">
        <div class="qcard-head">${whoAv(m, "sm")}<span>${isApproval ? "Approval required before delivery" : `${esc(agentLabel(m.agent))} (${esc(ROLE_LABEL[m.role] || m.role)}) asks you`}</span>${pending ? '<span class="badge amber">waiting</span>' : '<span class="badge green">answered</span>'}</div>
        <div class="qcard-body">
          <div class="md">${md(m.content || "")}</div>
          ${ds}${files}${opts}${reply}${answered}
        </div>
      </div>
    </div>`;
  }
  if (k === "verification") {
    const items = (m.items || []).map((i) => `<li class="${i.ok ? "ok" : "fail"}">${icon(i.ok ? "check" : "x")}<span class="truncate">${esc(i.command)}</span><small>${i.ok ? "pass" : `exit ${i.rc}`} · ${fmtDur(i.duration)}</small></li>`).join("");
    return `<div class="m m-verify" ${idAttr}><div class="vcard ${m.ok ? "" : "fail"}"><div class="vh">${icon(m.ok ? "shield" : "alert")}Verification ${m.ok ? "passed" : "failed"}</div><ul>${items}</ul></div></div>`;
  }
  if (k === "user") {
    return `<div class="m m-user" ${idAttr}><div class="bubble"><div class="to">You → ${esc(ROLE_LABEL[m.to] || m.to || "next turn")}${m.mode === "interrupt" ? " · interrupt" : ""}</div><div class="md">${md(m.content || "")}</div><time>${time}</time></div></div>`;
  }
  if (k === "error") {
    return `<div class="m m-error" ${idAttr}><div class="ecard"><strong>${esc(m.agent ? agentLabel(m.agent) : "Orchestrator")} · error</strong>${esc(m.content || "")}</div></div>`;
  }
  if (k === "notice") {
    return `<div class="m m-notice" ${idAttr}><div class="ncard">${esc(m.content || "")}</div></div>`;
  }
  if (k === "complete") {
    return `<div class="m m-complete" ${idAttr}><div class="ccard"><strong>${icon("check")}Delivered</strong><div class="md" style="margin-top:6px">${md(m.content || "")}</div>
      <div class="row wrap"><button class="btn sm primary" data-open-tab="result">${icon("play")}Try it</button><button class="btn sm" data-follow-up title="Start a new task that builds on this branch">${icon("arrowRight")}Follow up</button>${m.pr_url ? `<a class="btn sm" href="${esc(m.pr_url)}" target="_blank" rel="noopener">${icon("external")}Open pull request</a>` : ""}${m.branch ? `<span class="badge outline">${icon("branch", "sm")}${esc(m.branch)}</span>` : ""}</div></div></div>`;
  }
  if (k === "file_edit") {
    return `<div class="m m-sys" ${idAttr}>${icon("edit", "sm")} You edited <code>${esc(m.content)}</code> in the worktree</div>`;
  }
  return `<div class="m m-sys" ${idAttr}>${icon("info", "sm")} ${esc(m.kind)}: ${esc(m.content || m.summary || "")}</div>`;
}

function turnDivider(m) {
  if (m.turn === undefined || m.turn === null) return "";
  return `<div class="m-turn" data-turn="${m.turn}">${m.turn === 0 ? "Planning" : `Work package ${m.turn}`}</div>`;
}

export class Conversation {
  constructor(container, getTask) {
    this.c = container;
    this.getTask = getTask;
    this.prev = null;
    this.lastTurn = undefined;
    this.follow = true;
    this.jump = null;
    this.c.addEventListener("scroll", () => {
      this.follow = this.nearBottom();
      if (this.jump) this.jump.hidden = this.follow;
    });
    this.c.addEventListener("click", (e) => this.onClick(e));
    this.c.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.matches("[data-answer]")) {
        e.preventDefault();
        const card = e.target.closest(".qcard");
        const btn = card.querySelector("[data-send-answer], [data-approve='1']");
        btn && btn.click();
      }
    });
  }
  nearBottom() { return this.c.scrollHeight - this.c.scrollTop - this.c.clientHeight < 140; }
  scrollBottom(smooth = false) { this.c.scrollTo({ top: this.c.scrollHeight, behavior: smooth ? "smooth" : "auto" }); }
  setJumpButton(btn) { this.jump = btn; btn.onclick = () => { this.follow = true; this.scrollBottom(true); btn.hidden = true; }; }

  render(list) {
    const t = this.getTask();
    this.prev = null;
    this.lastTurn = undefined;
    if (!list.length) {
      this.c.innerHTML = `<div class="empty"><div>${icon("message", "lg")}</div><h3>${esc(t?.status === "queued" ? "Waiting in queue" : t?.status === "draft" ? "Draft task" : "No conversation yet")}</h3><p>${esc(t?.detail || "Run the queue to start the team.")}</p></div>`;
      return;
    }
    const parts = [];
    for (const m of list) {
      parts.push(this.divider(m));
      parts.push(renderMessage(m, t, this.prev));
      this.prev = { role: m.role, agent: m.agent, kind: m.kind };
    }
    this.c.innerHTML = parts.join("") + this.typingHtml();
    this.scrollBottom();
    this.follow = true;
  }
  divider(m) {
    if (m.turn !== undefined && m.turn !== null && m.turn !== this.lastTurn && (m.kind === "handoff" || m.kind === "plan" || this.lastTurn === undefined)) {
      this.lastTurn = m.turn;
      return turnDivider(m);
    }
    if (m.turn !== undefined && m.turn !== null) this.lastTurn = m.turn;
    return "";
  }
  append(m) {
    const t = this.getTask();
    const empty = this.c.querySelector(".empty");
    if (empty) { this.c.innerHTML = ""; this.prev = null; this.lastTurn = undefined; }
    this.c.querySelector(".typing")?.remove();
    const div = this.divider(m);
    if (div) this.c.insertAdjacentHTML("beforeend", div);
    this.c.insertAdjacentHTML("beforeend", renderMessage(m, t, this.prev));
    this.prev = { role: m.role, agent: m.agent, kind: m.kind };
    this.c.insertAdjacentHTML("beforeend", this.typingHtml());
    if (this.follow) requestAnimationFrame(() => this.scrollBottom());
    else if (this.jump) this.jump.hidden = false;
  }
  patch(m) {
    const node = this.c.querySelector(`[data-id="${CSS.escape(m.id)}"]`);
    if (!node) return;
    const open = node.querySelector(".tool-detail:not([hidden]), .thinking-body:not([hidden])") ? true : false;
    const wasCont = node.classList.contains("cont");
    const tmp = el(renderMessage(m, this.getTask(), null));
    if (wasCont) tmp.classList.add("cont");
    node.replaceWith(tmp);
    if (open) { const d = tmp.querySelector(".tool-detail, .thinking-body"); if (d) d.hidden = false; }
    if (this.follow && m.streaming) this.scrollBottom();
  }
  refreshQuestions() {
    const t = this.getTask();
    const store = S.msgs.get(t?.id);
    if (!store) return;
    for (const node of $$('.m[data-kind="question"], .m[data-kind="approval"]', this.c)) {
      const m = store.byId.get(node.dataset.id);
      if (m) this.patch(m);
    }
  }
  typingHtml() {
    const t = this.getTask();
    const p = t?.process;
    if (!p || p.state !== "running") return "";
    const who = p.agent ? `${agentLabel(p.agent)} (${ROLE_LABEL[p.role] || p.role})` : (p.label || "Orchestrator");
    // A running tool is activity, not silence: say what it is doing instead of "quiet".
    const running = this.runningTool();
    let doing;
    if (running) {
      const ed = running.kind === "tool" ? describeEdit(running, t) : null;
      const sh = !ed && running.category === "shell" ? shellEditTargets(running.input || running.summary, t) : [];
      const what = ed && ed.files.length ? `editing ${ed.files.join(", ")}`
        : sh.length ? `editing ${sh.join(", ")} via shell`
        : `running ${running.tool || running.title || "a command"}${running.summary ? `: ${String(running.summary).slice(0, 80)}` : ""}`;
      doing = ` is ${esc(what)} · ${fmtSec(Date.now() / 1000 - (running.ts || Date.now() / 1000))}`;
    } else {
      const silent = Number(p.silent_for || 0);
      doing = silent > 15
        ? ` is thinking · ${fmtSec(p.elapsed)} · no output for ${fmtSec(silent)}`
        : ` is working · ${fmtSec(p.elapsed)}`;
    }
    return `<div class="typing" data-typing><span class="av sm ${esc(p.agent || "system")}">${esc(agentInitial(p.agent || "system"))}</span><span class="truncate"><strong>${esc(who)}</strong>${doing}</span><span class="dots"><span></span><span></span><span></span></span></div>`;
  }
  runningTool() {
    const store = S.msgs.get(this.getTask()?.id);
    if (!store) return null;
    for (let i = store.list.length - 1, n = 0; i >= 0 && n < 60; i--, n++) {
      const m = store.list[i];
      if ((m.kind === "tool" || m.kind === "command") && m.status === "running") return m;
    }
    return null;
  }

  // Distinct files the agents have edited in this conversation, most recent first.
  filesTouched() {
    const t = this.getTask();
    const store = S.msgs.get(t?.id);
    const seen = new Set();
    const order = [];
    for (const m of store?.list || []) {
      if (m.kind !== "tool" || m.status === "error") continue;
      const ed = describeEdit(m, t);
      const files = ed ? ed.files : (m.category === "shell" ? shellEditTargets(m.input || m.summary, t) : []);
      for (const f of files) { if (seen.has(f)) order.splice(order.indexOf(f), 1); seen.add(f); order.push(f); }
    }
    return order.reverse();
  }

  updateTyping() {
    const cur = this.c.querySelector("[data-typing]");
    const html = this.typingHtml();
    if (!html) { cur && cur.remove(); return; }
    if (cur) { const n = el(html); cur.replaceWith(n); }
    else { this.c.insertAdjacentHTML("beforeend", html); if (this.follow) this.scrollBottom(); }
  }
  onClick(e) {
    const tog = e.target.closest("[data-toggle]");
    if (tog) {
      const m = tog.closest(".m");
      const d = m.querySelector(".tool-detail, .thinking-body");
      if (d) d.hidden = !d.hidden;
      return;
    }
    const more = e.target.closest("[data-more]");
    if (more) {
      const body = this.c.querySelector(`[data-clamp="${CSS.escape(more.dataset.more)}"]`);
      if (body) { body.classList.toggle("clamp"); more.textContent = body.classList.contains("clamp") ? "Show more" : "Show less"; }
      return;
    }
    if (e.target.closest("[data-follow-up]")) { const t = this.getTask(); if (t) openFollowUp(t); return; }
    const opt = e.target.closest("[data-opt]");
    if (opt) { const ta = opt.closest(".qcard").querySelector("[data-answer]"); if (ta) { ta.value = opt.dataset.opt; ta.focus(); } return; }
    const send = e.target.closest("[data-send-answer]");
    if (send) { this.answer(send.closest(".qcard"), null); return; }
    const appr = e.target.closest("[data-approve]");
    if (appr) { this.answer(appr.closest(".qcard"), appr.dataset.approve === "1"); return; }
  }
  async answer(card, approved) {
    const t = this.getTask();
    const ta = card.querySelector("[data-answer]");
    const text = (ta?.value || "").trim();
    if (approved === false && !text) { toast("warning", "Add a note", "Tell the team what to change."); ta.focus(); return; }
    const btns = $$("button", card);
    btns.forEach((b) => (b.disabled = true));
    try {
      if (approved === null) await api.action(t.id, "answer", { id: t.pending?.id, text });
      else await api.action(t.id, approved ? "approve" : "reject", { note: text });
      toast("success", approved === null ? "Answer sent" : approved ? "Approved" : "Changes requested");
    } catch (err) {
      toast("error", "Could not send", err.message);
      btns.forEach((b) => (b.disabled = false));
    }
  }
}
