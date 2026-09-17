// Getting started: the first-run checklist, filled in from what Relay can observe, plus a sample task.
import { $, $$, esc, icon, toast, modal } from "../../ui.js";
import { S, navigate } from "../../state.js";
import { orgApi, ORG, loadMe, can, xicon } from "./org.js";

function ring(done, total) {
  const r = 44, c = 2 * Math.PI * r, frac = total ? done / total : 0;
  return `<svg class="org-ring" viewBox="0 0 108 108" role="img" aria-label="${done} of ${total} steps done">
    <circle class="bg" cx="54" cy="54" r="${r}"/>
    <circle class="fg" cx="54" cy="54" r="${r}" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${(c * (1 - frac)).toFixed(2)}" transform="rotate(-90 54 54)"/>
    <text x="54" y="56" text-anchor="middle">${done}/${total}</text><text class="sub" x="54" y="72" text-anchor="middle">steps done</text></svg>`;
}

const agentName = (a) => (S.agentMeta?.[a] || {}).label || a;

export function mountWelcome(body) {
  let data = null, alive = true, busy = false;
  body.innerHTML = `<div class="empty small">Loading…</div>`;

  async function load() {
    try { data = await orgApi.onboarding(); } catch (e) { if (alive && !data) body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (alive && !busy && !document.querySelector(".modal-backdrop")) draw();
  }

  function stepAction(s) {
    const a = s.action || {};
    if (s.done) return `<span class="badge green">${icon("check", "sm")}Done</span>${a.href ? `<a class="btn xs ghost" href="${esc(a.href)}">Open</a>` : ""}`;
    if (s.running) return `<span class="badge blue">${icon("spinner", "sm")}Running</span>`;
    if (a.href) return `<a class="btn sm" href="${esc(a.href)}">${esc(a.label)}${icon("arrowRight", "sm")}</a>`;
    if (a.do === "team") return can("admin") ? `<button class="btn sm primary" data-do="team">${esc(a.label)}</button>` : `<span class="muted" style="font-size:var(--fs-xs)">An admin chooses the team</span>`;
    if (a.do === "sample") return can("member") ? `<button class="btn sm primary" data-do="sample">${icon("play")}${esc(a.label)}</button>` : `<span class="muted" style="font-size:var(--fs-xs)">Needs the member role</span>`;
    return "";
  }

  function draw() {
    const name = (ORG.me?.user?.name || "").split(/\s+/)[0];
    const nextId = (data.steps.find((s) => !s.done) || {}).id;
    const complete = data.complete;
    body.innerHTML = `
      <div class="card org-welcome-card"><div class="org-welcome">${ring(data.done, data.total)}
        <div class="min0"><h1>${complete ? `Relay is ready${name ? `, ${esc(name)}` : ""}` : `Welcome${name ? `, ${esc(name)}` : ""}. Let's get Relay working for you.`}</h1>
          <p>${complete ? "Every setup step is done. Queue real work, invite your team and connect the channels that should reach people." :
            "Six steps from an empty install to agent teams delivering reviewed pull requests. Steps tick themselves off as Relay sees them done."}</p>
          <div class="row wrap"><button class="btn sm ghost" id="obHide">${data.dismissed ? `${icon("eye")}Show in the menu again` : `${icon("x")}Hide checklist from the menu`}</button></div>
        </div></div>
        ${complete ? `<div class="org-done-links">
          <a href="#/org/projects"><b class="row" style="gap:7px">${xicon("grid")}Organise projects</b><span>Group repositories, budgets and default teams.</span></a>
          <a href="#/org/people"><b class="row" style="gap:7px">${xicon("users")}Invite people</b><span>Roles from your identity provider's groups.</span></a>
          <a href="#/org/integrations"><b class="row" style="gap:7px">${xicon("plug")}Connect channels</b><span>Slack, Telegram, email and signed webhooks.</span></a></div>` : ""}
      </div>
      <div class="org-steps">${data.steps.map((s, i) => `
        <div class="org-step ${s.done ? "done" : ""} ${s.id === nextId ? "next" : ""}">
          <span class="org-step-n">${s.done ? icon("check", "sm") : i + 1}</span>
          <div class="min0"><h4>${esc(s.title)}</h4><p>${esc(s.why || "")}${s.detail ? ` <span class="detail">· ${esc(s.detail)}</span>` : ""}</p></div>
          <div class="org-step-act">${stepAction(s)}</div>
        </div>`).join("")}</div>`;
    $("#obHide", body).onclick = async () => {
      try { await orgApi.dismissOnboarding(!data.dismissed); await loadMe(); load(); } catch (e) { toast("error", "Could not update", e.message); }
    };
    $$("[data-do='team']", body).forEach((b) => (b.onclick = chooseTeam));
    $$("[data-do='sample']", body).forEach((b) => (b.onclick = () => runSample(b)));
  }

  function chooseTeam() {
    const current = (data.steps.find((s) => s.id === "team") || {}).detail;
    const presets = data.presets || [];
    const m = modal(`<h2>Choose a team preset</h2><p class="hint">Who supervises, who writes the code and who reviews. New tasks start with this team; each task and project can still change it.</p>
      <div class="org-presets">${presets.map((p, i) => {
        const roles = Object.entries(p.roles || {}).filter(([, v]) => v.agent).map(([r, v]) => `${r}: ${agentName(v.agent)}`).join(" · ");
        return `<label class="org-preset"><input type="radio" name="preset" value="${esc(p.id)}" ${(current ? p.id === current : i === 0) ? "checked" : ""}><b>${esc(p.name || p.label || p.id)}</b><span>${esc(p.description || "")}${roles ? `<br>${esc(roles)}` : ""}</span></label>`;
      }).join("")}</div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="presetOk">Use this team</button></div>`, { wide: true });
    $("#presetOk", m.body).onclick = async () => {
      const id = $("input[name=preset]:checked", m.body)?.value;
      if (!id) return;
      $("#presetOk", m.body).disabled = true;
      try { data = await orgApi.chooseTeam(id); m.close(); toast("success", "Team chosen", "New tasks use it by default."); loadMe(); draw(); }
      catch (e) { toast("error", "Could not choose the team", e.message); $("#presetOk", m.body).disabled = false; }
    };
  }

  async function runSample(btn) {
    busy = true;
    btn.disabled = true;
    btn.innerHTML = `${icon("spinner")}Starting…`;
    try {
      const r = await orgApi.sampleTask();
      toast("success", "Sample task queued", "Watch the supervisor, worker and reviewer work through it.");
      if (r.task?.id) navigate(`#/task/${r.task.id}`);
    } catch (e) {
      toast("error", "Could not start the sample task", e.message);
      btn.disabled = false;
      btn.innerHTML = `${icon("play")}Run sample`;
    } finally { busy = false; }
  }

  load();
  const timer = setInterval(() => { if (document.visibilityState === "visible") load(); }, 10000);
  return { update() {}, destroy() { alive = false; clearInterval(timer); } };
}
