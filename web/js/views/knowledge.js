// Knowledge: what Relay knows about the systems it works on. The system map, the repositories with their
// environments, stacks and worktrees, the connectors to real environments, the lessons learned from past runs and
// the GitHub repositories it watches, under one roof.
import { $, $$, esc, icon } from "../ui.js";
import { S } from "../state.js";
import { mountRepos } from "./repos.js";
import { mountConnectors } from "./connectors.js";
import { mountLessons } from "./lessons.js";
import { mountGithub } from "./github.js";

const TABS = [
  ["system", "System map", "globe", "How your repositories depend on each other."],
  ["repositories", "Repositories", "folder", "Local clones, their environments, stacks and connectors."],
  ["connectors", "Connectors", "zap", "The APIs, databases and logs agents may check."],
  ["lessons", "Lessons", "brain", "What retrospectives learned, and what reaches the prompts."],
  ["worktrees", "Worktrees", "layers", "Isolated checkouts Relay created for tasks."],
  ["graph", "Branch graph", "branch", "How task branches relate to the main line."],
  ["github", "GitHub", "github", "Repositories watched for labelled or assigned issues."],
];
const REPOS_SECTION = { system: "system", repositories: "list", worktrees: "worktrees", graph: "graph" };

export function mountKnowledge(main, tab) {
  let cur = TABS.some(([k]) => k === tab) ? tab : "system";
  let child = null;
  main.innerHTML = `<div class="page kn" id="kn">
    <header class="page-header">
      <div class="ph-title"><div class="eyebrow">Knowledge</div><h1>What Relay knows about your systems</h1><p class="ph-sub" id="knSub"></p></div>
    </header>
    <nav class="tabbar-inline" role="tablist" aria-label="Knowledge sections" id="knTabs"></nav>
    <div class="kn-embed" id="knBody"></div>
  </div>`;
  const page = $("#kn", main);

  function drawTabs() {
    $("#knTabs", page).innerHTML = TABS.map(([k, l, i]) => `<a role="tab" href="#/knowledge/${k}" aria-selected="${k === cur}" class="${k === cur ? "active" : ""}">${icon(i, "sm")}<span>${esc(l)}</span>${k === "lessons" && S.lessonsPending ? `<span class="count-pill attn">${S.lessonsPending}</span>` : ""}</a>`).join("");
    $("#knSub", page).textContent = TABS.find(([k]) => k === cur)[3];
    $("#knTabs a.active", page)?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  function show(k) {
    cur = k;
    child?.destroy?.();
    child = null;
    const host = $("#knBody", page);
    host.innerHTML = "";
    host.dataset.tab = k;
    drawTabs();
    if (REPOS_SECTION[k]) child = mountRepos(host, REPOS_SECTION[k]);
    else if (k === "connectors") { mountConnectors(host); child = null; }
    else if (k === "lessons") child = mountLessons(host);
    else if (k === "github") child = mountGithub(host);
  }
  show(cur);
  return {
    update(reason, info) {
      if (reason === "route") { const t = S.route.tab && TABS.some(([k]) => k === S.route.tab) ? S.route.tab : "system"; if (t !== cur) show(t); return; }
      if (reason === "lessons") drawTabs();
      child?.update?.(reason, info);
    },
    destroy() { child?.destroy?.(); },
  };
}
