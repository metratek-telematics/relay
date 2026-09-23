// Who did what on a task: created, answered, approved (or asked for changes), merged. Shown on the task overview.
import { esc, icon, timeAgo } from "../../ui.js";
import { ORG, projectOfTask, avatar } from "./org.js";

function person(p) {
  if (!p) return null;
  const name = typeof p === "string" ? p : p.username;
  const u = (ORG.people || []).find((x) => x.username === name);
  return { name, label: u?.name || name, when: typeof p === "string" ? "" : p.time, via: typeof p === "string" ? "" : p.via, u };
}

export function attributionHtml(t) {
  const proj = ORG.projects.find((p) => p.id === projectOfTask(t));
  const bits = [
    ["created", person(t.created_by), t.created_at, "plus"],
    ["answered", person(t.answered_by), null, "message"],
    ["approved", person(t.approved_by), null, "check"],
    ["asked for changes", person(t.changes_requested_by), null, "retry"],
    ["merged", person(t.merged_by), null, "branch"],
  ].filter(([, p]) => p);
  if (!bits.length && !proj) return "";
  const who = (p, verb, when, ic) => {
    const av = p.u ? avatar({ ...p.u, initials: p.u.initials }, 18) : `<span class="org-av" style="width:18px;height:18px;font-size:8px;background:#55606e"><span>${esc((p.name || "?").slice(0, 2).toUpperCase())}</span></span>`;
    return `<span title="${esc(`${verb} by ${p.label}${p.via ? ` via ${p.via}` : ""}`)}">${icon(ic, "sm")}${verb} by ${av}<b>${esc(p.label)}</b>${when || p.when ? `<span class="muted">${esc(timeAgo(when || p.when))}</span>` : ""}${p.via && p.via !== "proxy" && p.via !== "local" ? `<span class="badge outline">${esc(p.via.startsWith("token") ? "API" : p.via)}</span>` : ""}</span>`;
  };
  // Which defaults the task started from, recorded when it was created (orchestrator/personal.py),
  // so the task does not change meaning when somebody else looks at it.
  const df = t.workflow?.defaults_from;
  const started = df && df.personal?.length
    ? `<span title="${esc(`This task started from ${df.user || "the creator"}'s personal defaults, not the organisation's`)}">${icon("user", "sm")}started from <b>${esc(df.user || "their")}</b> own defaults</span>`
    : df
      ? `<span title="This task started from the organisation's defaults">${icon("layers", "sm")}started from the <b>organisation default</b></span>`
      : "";
  return `<div class="org-attrib">${proj ? `<span><span class="org-proj-dot" style="background:${esc(proj.color)}"></span><a href="#/org/project/${esc(proj.id)}"><b>${esc(proj.name)}</b></a></span>` : ""}${bits.map(([verb, p, when, ic]) => who(p, verb, when, ic)).join("")}${started}</div>`;
}
