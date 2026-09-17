// Keyboard shortcuts: the single list behind the ? overlay, palette hints and tooltips.
import { $, esc, modal } from "./ui.js";

const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
const MOD = MAC ? "⌘" : "Ctrl";

// Two-key "go to" sequences: press G, then the letter.
export const GOTO = { d: "#/", t: "#/tasks", a: "#/agents", h: "#/github", s: "#/settings", r: "#/repos", l: "#/lessons" };

export const SHORTCUTS = [
  { id: "palette", group: "General", keys: [MOD, "K"], label: "Command palette" },
  { id: "help", group: "General", keys: ["?"], label: "Keyboard shortcuts" },
  { id: "new", group: "General", keys: ["N"], label: "New task" },
  { id: "search", group: "General", keys: ["/"], label: "Search tasks" },
  { id: "sidebar", group: "General", keys: ["B"], label: "Show or hide the task list" },
  { id: "close", group: "General", keys: ["Esc"], label: "Close dialogs, menus and panels" },
  { id: "goDashboard", group: "Go to", keys: ["G", "D"], label: "Dashboard", then: true },
  { id: "goTasks", group: "Go to", keys: ["G", "T"], label: "Tasks", then: true },
  { id: "goAgents", group: "Go to", keys: ["G", "A"], label: "Agents", then: true },
  { id: "goGithub", group: "Go to", keys: ["G", "H"], label: "GitHub inbox", then: true },
  { id: "goSettings", group: "Go to", keys: ["G", "S"], label: "Settings", then: true },
  { id: "goRepos", group: "Go to", keys: ["G", "R"], label: "Repositories", then: true },
  { id: "goLessons", group: "Go to", keys: ["G", "L"], label: "Lessons", then: true },
  { id: "next", group: "Task list", keys: ["J"], label: "Open the next task" },
  { id: "prev", group: "Task list", keys: ["K"], label: "Open the previous task" },
  { id: "tryIt", group: "Current task", keys: ["T"], label: "Open the Try it tab" },
  { id: "guidance", group: "Current task", keys: ["."], label: "Focus the guidance box" },
  { id: "tabs", group: "Current task", keys: ["[", "]"], label: "Previous or next inspector tab" },
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
