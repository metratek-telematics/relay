// Keyboard shortcuts: the single list behind the ? overlay, palette hints and tooltips.
import { $, esc, modal } from "./ui.js";

const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
const MOD = MAC ? "⌘" : "Ctrl";

// Two-key "go to" sequences: press G, then the letter.
export const GOTO = { h: "#/", d: "#/", w: "#/work", t: "#/work", i: "#/work/issues", k: "#/knowledge", r: "#/knowledge/repositories", l: "#/knowledge/lessons", m: "#/knowledge/system", a: "#/agents", s: "#/settings", y: "#/home/needs", e: "#/home/24h" };

export const SHORTCUTS = [
  { id: "palette", group: "General", keys: [MOD, "K"], label: "Search, jump anywhere, run a command or “create task: …”" },
  { id: "help", group: "General", keys: ["?"], label: "Keyboard shortcuts" },
  { id: "new", group: "General", keys: ["N"], label: "New task" },
  { id: "search", group: "General", keys: ["/"], label: "Search (the board filter on Work)" },
  { id: "close", group: "General", keys: ["Esc"], label: "Close dialogs, menus and panels" },
  { id: "goHome", group: "Go to", keys: ["G", "H"], label: "Mission Control", then: true },
  { id: "goWork", group: "Go to", keys: ["G", "W"], label: "Work board", then: true },
  { id: "goNeeds", group: "Go to", keys: ["G", "Y"], label: "Needs you", then: true },
  { id: "goDigest", group: "Go to", keys: ["G", "E"], label: "Last 24 hours", then: true },
  { id: "goIssues", group: "Go to", keys: ["G", "I"], label: "Issues on the board", then: true },
  { id: "goKnowledge", group: "Go to", keys: ["G", "K"], label: "Knowledge", then: true },
  { id: "goMap", group: "Go to", keys: ["G", "M"], label: "System map", then: true },
  { id: "goRepos", group: "Go to", keys: ["G", "R"], label: "Repositories", then: true },
  { id: "goLessons", group: "Go to", keys: ["G", "L"], label: "Lessons", then: true },
  { id: "goAgents", group: "Go to", keys: ["G", "A"], label: "Agents", then: true },
  { id: "goSettings", group: "Go to", keys: ["G", "S"], label: "Settings", then: true },
  { id: "next", group: "Task page", keys: ["J"], label: "Next task on the board" },
  { id: "prev", group: "Task page", keys: ["K"], label: "Previous task on the board" },
  { id: "views", group: "Task page", keys: ["1", "5"], label: "Conversation, Live, Changes, Checks, Logs" },
  { id: "guidance", group: "Task page", keys: ["."], label: "Focus the guidance box" },
  { id: "tryIt", group: "Task page", keys: ["T"], label: "Try it (commands to run the result)" },
  { id: "rail", group: "Task page", keys: ["I"], label: "Show or hide the details rail" },
  { id: "boardKeys", group: "Work board", keys: ["X"], label: "Select the focused card" },
];

// Short text for palette items and title attributes, e.g. "G D" or "Ctrl+K".
export function keysFor(id) {
  const s = SHORTCUTS.find((x) => x.id === id);
  if (!s) return "";
  return s.then ? s.keys.join(" ") : s.keys.join(s.keys[0] === MOD ? "+" : " ");
}

export function openShortcuts() {
  if ($(".shortcuts-modal")) return;
  const groups = [...new Set(SHORTCUTS.map((s) => s.group))];
  const keys = (s) => s.keys.map((k) => `<kbd>${esc(k)}</kbd>`).join(s.then ? '<span class="kbd-then">then</span>' : s.keys[0] === MOD ? "+" : '<span class="kbd-then">or</span>');
  modal(`<div class="shortcuts-modal"><div class="row between"><h2>Keyboard shortcuts</h2><button class="btn xs ghost" data-close aria-label="Close">Esc</button></div>
    <p class="hint">Shortcuts never fire while you are typing in a field.</p>
    <div class="kbd-groups">${groups.map((g) => `<section><h3 class="section-title">${esc(g)}</h3>${SHORTCUTS.filter((s) => s.group === g).map((s) => `<div class="kbd-row"><span>${esc(s.label)}</span><span class="kbd-keys">${keys(s)}</span></div>`).join("")}</section>`).join("")}</div></div>`);
}
