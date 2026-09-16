// Desktop notifications, the attention chime, and the tab title + favicon badge.
import { S, statusOf, navigate } from "./state.js";

// Server notifications carry a `kind`; each maps onto one of the toggles in
// Settings → Notifications. Kinds without a toggle follow the main switch only.
export const NOTIFY_EVENTS = [
  ["delivered", "Task delivered", "A task finished and its branch or pull request is ready."],
  ["failed", "Task failed or stopped", "A run failed, ran out of turns, or was stopped."],
  ["needs_input", "Agent needs input or approval", "An agent asked you a question, or delivery is waiting for your approval."],
  ["pr_opened", "Pull request opened", "Relay opened a draft pull request on GitHub."],
];
const EVENT_OF_KIND = { delivered: "delivered", failed: "failed", stopped: "failed", needs_input: "needs_input", approval: "needs_input", pr_opened: "pr_opened" };

export function eventAllowed(n) {
  const ev = EVENT_OF_KIND[n.kind];
  return !ev || (S.config.ui_notify_events || {})[ev] !== false;
}

export function permission() {
  return "Notification" in window ? Notification.permission : "unsupported";
}

// Browsers only honour a permission request made from a click, so this is only
// ever called from a button, never on load.
export async function requestPermission() {
  if (!("Notification" in window)) return "unsupported";
  try { return await Notification.requestPermission(); } catch { return Notification.permission; }
}

let audio = null;
// A short two-note chime made on the fly, so Relay ships no audio files. Rising
// for good news and questions, falling for failures, so the sound alone says which.
export function chime(level = "info") {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === "suspended") audio.resume();
    const notes = level === "error" ? [523.25, 392] : [659.25, 880];
    const t0 = audio.currentTime + 0.01;
    notes.forEach((freq, i) => {
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const start = t0 + i * 0.12;
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(0.07, start + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.22);
      osc.connect(gain).connect(audio.destination);
      osc.start(start);
      osc.stop(start + 0.24);
    });
  } catch {}
}

export function showDesktop(n) {
  if (permission() !== "granted") return false;
  try {
    const nt = new Notification(`Relay · ${n.title}`, { body: n.body || "", tag: n.id });
    nt.onclick = () => {
      window.focus();
      nt.close();
      if (n.task_id) navigate(`#/task/${n.task_id}`);
    };
    return true;
  } catch { return false; }
}

// Called for every server notification. Toasts are shown by the caller; this only
// decides whether to reach you outside the window.
export function deliver(n) {
  if (!S.config.ui_notifications || !eventAllowed(n) || document.hasFocus()) return;
  showDesktop(n);
  if (S.config.ui_sound) chime(n.level);
}

// ---------------------------------------------------------------------------- tab title + favicon
// Failed tasks count until you open them. The baseline stops a first visit from
// flagging every failure in the history as new.
const SEEN_KEY = "relay.seenTasks", SINCE_KEY = "relay.seenSince";
let seen = {}, since = Date.now();
try {
  seen = JSON.parse(localStorage.getItem(SEEN_KEY) || "{}") || {};
  since = Number(localStorage.getItem(SINCE_KEY)) || since;
  localStorage.setItem(SINCE_KEY, String(since));
} catch {}

export function markSeen(id) {
  if (!id || document.visibilityState !== "visible") return;
  seen[id] = Date.now();
  // Keep the map small: ids of tasks that no longer exist are dropped.
  if (S.tasks.size) for (const k of Object.keys(seen)) if (!S.tasks.has(k)) delete seen[k];
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(seen)); } catch {}
}

export function attentionTasks() {
  return [...S.tasks.values()].filter((t) => {
    if (t.archived) return false;
    if (statusOf(t).attention) return true;
    if (t.status !== "failed") return false;
    const at = Date.parse(t.finished_at || t.updated_at || "") || 0;
    return at > Math.max(since, seen[t.id] || 0);
  });
}

const ICON_BASE = document.querySelector("link[rel='icon']")?.href || "";
// Favicons cannot read CSS custom properties, so the badge uses the literal
// values of --accent and --red from styles.css.
const ICON_DOT = "data:image/svg+xml," + encodeURIComponent(
  "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='#d97757'/>" +
  "<text x='28' y='43' font-family='Segoe UI,Arial' font-weight='700' font-size='32' fill='white' text-anchor='middle'>R</text>" +
  "<circle cx='50' cy='14' r='13' fill='#c5473f' stroke='white' stroke-width='4'/></svg>");
let lastBadge = null;

export function renderAttention() {
  const n = attentionTasks().length;
  const title = n ? `(${n}) Relay` : "Relay";
  if (document.title !== title) document.title = title;
  if (lastBadge === !!n) return;
  lastBadge = !!n;
  const link = document.querySelector("link[rel='icon']");
  if (link) link.href = n ? ICON_DOT : ICON_BASE;
}
