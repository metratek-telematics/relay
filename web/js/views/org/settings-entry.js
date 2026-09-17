// The single Settings entry for the organisation layer: a map of its pages with their current state.
import { esc, icon } from "../../ui.js";
import { ORG, loadMe, xicon, roleBadge, SECTIONS, can } from "./org.js";

const BLURB = {
  profile: "Your name, avatar and appearance.",
  notifications: "Which events reach you, and where: browser, email, Slack, Telegram, Discord, webhooks.",
  tokens: "Personal access tokens for the REST API and CI.",
  welcome: "The setup checklist and a sample task.",
  projects: "Group repositories, connectors, stacks and tasks; defaults and budgets per project.",
  people: "Who has access, their roles, and how identity-provider groups map to roles.",
  integrations: "Team Slack, Discord, Telegram, SMTP, Web Push and signed webhooks; the delivery log.",
  usage: "Spend, tokens and agent time per project and person; monthly budgets and alerts.",
  audit: "Every change: who, when, what, before and after. Filter and export as CSV.",
  api: "The /api/v1 reference with examples and a GitHub Actions recipe.",
};

export async function renderWorkspaceSettings(body) {
  if (!ORG.me) await loadMe();
  const me = ORG.me;
  body.innerHTML = `<div class="card"><div class="card-head"><div><h3>Workspace &amp; access</h3><p class="card-sub">People, projects, roles, integrations, API, usage and audit. These pages also sit in the user menu at the top right.</p></div>
      ${me ? roleBadge(me.role) : ""}</div>
    <div class="card-body">
      ${me && !me.identity_mode ? `<div class="org-note warn" style="margin-bottom:12px">${icon("alert", "sm")}<span>Local mode: nobody signs in, so every visitor is the owner. Set trusted proxies under <a href="#/org/people">People &amp; roles</a> to use your identity provider's accounts and roles.</span></div>` : ""}
      <div class="org-channels">${SECTIONS.flatMap((g) => g.items).filter(([, , , need]) => !need || can(need)).map(([k, l, i]) => `
        <a class="org-channel" href="#/org/${k}" style="text-decoration:none;color:inherit">
          <div class="org-channel-head"><span class="org-ch-ic">${xicon(i)}</span><strong>${esc(l)}</strong>${icon("arrowRight", "sm")}</div>
          <div class="help">${esc(BLURB[k] || "")}</div></a>`).join("")}</div>
    </div></div>`;
}
