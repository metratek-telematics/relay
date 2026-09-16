// The supervisor's context packet and the checks nobody could run, rendered the same way everywhere.
import { esc, icon } from "./ui.js";

const list = (items, cls = "") => `<ul class="pk-list ${cls}">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;

export function packetHtml(plan, { done = false } = {}) {
  if (!plan) return "";
  const out = [];
  if ((plan.requirements || []).length) out.push(`<h4>Requirements</h4>${list(plan.requirements)}`);
  if ((plan.acceptance || []).length) {
    out.push(`<h4>Acceptance criteria</h4><ul class="acceptance">${plan.acceptance.map((a) => `<li class="${done ? "ok" : ""}"><i>${icon("check")}</i><span>${esc(a)}</span></li>`).join("")}</ul>`);
  }
  if ((plan.optional || []).length) out.push(`<h4>Optional <span class="muted pk-note">never blocks done</span></h4>${list(plan.optional, "muted")}`);
  const files = plan.known_files || {};
  if ((files.primary || []).length || (files.supporting || []).length) {
    out.push(`<h4>Known files</h4><div class="pk-files">${(files.primary || []).map((f) => `<code class="primary" title="primary">${esc(f)}</code>`).join("")}${(files.supporting || []).map((f) => `<code title="supporting">${esc(f)}</code>`).join("")}</div>`);
  }
  if ((plan.findings || []).length) {
    out.push(`<h4>Findings</h4><ul class="pk-list">${plan.findings.map((f) => `<li>${f.file ? `<code>${esc(f.file)}</code> ` : ""}${esc(f.finding)}</li>`).join("")}</ul>`);
  }
  if ((plan.constraints || []).length) out.push(`<h4>Constraints</h4>${list(plan.constraints)}`);
  if ((plan.unknowns || []).length) out.push(`<h4>Unknowns</h4>${list(plan.unknowns)}`);
  return out.length ? `<div class="packet">${out.join("")}</div>` : "";
}

export function blockedHtml(checks) {
  if (!(checks || []).length) return "";
  return `<ul class="blocked-checks">${checks.map((c) => `<li>${icon("alert", "sm")}<div><strong>${esc(c.check || "Blocked")}</strong>${c.action_required ? ' <span class="badge amber">needs you</span>' : ""}<div class="muted">${esc(c.reason || "")}${c.impact ? ` · ${esc(c.impact)}` : ""}</div></div></li>`).join("")}</ul>`;
}
