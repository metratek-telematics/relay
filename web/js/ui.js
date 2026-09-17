// UI primitives: escaping, markdown, icons, formatting, toasts, modals, command palette.

export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// ---------------------------------------------------------------------------- icons (Lucide-style, 24px viewBox, stroke)
const P = {
  radar: '<path d="M19.07 4.93A10 10 0 1 0 22 12"/><path d="M16.24 7.76A6 6 0 1 0 18 12"/><circle cx="12" cy="12" r="2"/><path d="m13.4 10.6 6-6"/>',
  kanban: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 7v7M12 7v4M16 7v9"/>',
  share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4"/>',
  merge: '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>',
  grip: '<circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  trend: '<path d="m22 7-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  book: '<path d="M2 4h7a3 3 0 0 1 3 3v14a2 2 0 0 0-2-2H2z"/><path d="M22 4h-7a3 3 0 0 0-3 3v14a2 2 0 0 1 2-2h8z"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  columns: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18"/>',
  maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  checkCircle: '<circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/>',
  home: '<path d="M3 11 12 3l9 8v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
  tasks: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="m3 6 1 1 2-2M3 12l1 1 2-2M3 18l1 1 2-2"/>',
  bot: '<rect x="3" y="8" width="18" height="12" rx="2"/><path d="M12 8V4M8 4h8"/><circle cx="9" cy="14" r="1"/><circle cx="15" cy="14" r="1"/>',
  github: '<path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5a5.5 5.5 0 0 0-1.5-3.8 5.2 5.2 0 0 0-.1-3.7s-1.2-.3-3.9 1.5a13.4 13.4 0 0 0-7 0C4.9 1.2 3.7 1.5 3.7 1.5a5.2 5.2 0 0 0-.1 3.7A5.5 5.5 0 0 0 2 9c0 3.5 3 5.5 6 5.5a4.8 4.8 0 0 0-1 3.5v4"/><path d="M9 18c-4.5 2-5-2-7-2"/>',
  settings: '<path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.6a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7V20a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.6a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  play: '<path d="M6 4v16l14-8z"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  retry: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  command: '<path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  alert: '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3"/><path d="M12 9v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronUp: '<path d="m18 15-6-6-6 6"/>',
  issue: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="1.6"/>',
  unqueue: '<path d="M8 6h13M8 12h13M8 18h7"/><path d="m3 10 4 4M7 10l-4 4"/>',
  terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
  file: '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5z"/><path d="M14 2v6h6"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20z"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
  message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  branch: '<path d="M6 3v12"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
  external: '<path d="M15 3h6v6M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9L9.2 3.9A2 2 0 0 0 7.5 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/>',
  more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  send: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5M2 17l10 5 10-5"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  dollar: '<path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  arrowRight: '<path d="M5 12h14M12 5l7 7-7 7"/>',
  brain: '<path d="M12 5a3 3 0 1 0-5.9.9A4 4 0 0 0 4 13a4 4 0 0 0 2 7 3 3 0 0 0 6-1z"/><path d="M12 5a3 3 0 1 1 5.9.9A4 4 0 0 1 20 13a4 4 0 0 1-2 7 3 3 0 0 1-6-1z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  flag: '<path d="M4 22V4a1 1 0 0 1 1-1h11l-1 4 1 4H5"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  code: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
  filter: '<path d="M22 3H2l8 9.5V19l4 2v-8.5z"/>',
  archive: '<rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8M10 12h4"/>',
  trash: '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  duplicate: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
  hash: '<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/>',
  wand: '<path d="m15 4 1 1M5 14l1 1M14 13 3 24M21 12l-1-1M18 7l3-3M12 6l1-1"/><path d="m3 21 9-9-3-3-9 9z"/>',
  gauge: '<path d="m12 14 4-4"/><path d="M3.3 17a10 10 0 1 1 17.4 0"/>',
  question: '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
  spinner: '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',
  refresh: '<path d="M21 12a9 9 0 0 1-15.5 6.3L3 16M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M3 21v-5h5M21 3v5h-5"/>',
  sparkles: '<path d="m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 17v4M17 19h4M5 3v4M3 5h4"/>',
  panel: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M15 3v18"/>',
  docs: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
  keyboard: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M6 13h.01M18 13h.01M10 13h4M7 16h10"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
  sunrise: '<path d="M12 2v6M4.9 10.9l1.4 1.4M2 18h2M20 18h2M17.7 12.3l1.4-1.4M22 22H2M8 5l4-3 4 3M16 18a4 4 0 0 0-8 0"/>',
  package: '<path d="m7.5 4.3 9 5.2M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4a2 2 0 0 0 1-1.7z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
};
export function icon(name, cls = "") {
  return `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || P.info}</svg>`;
}

// ---------------------------------------------------------------------------- formatting
export function fmtSec(n) {
  n = Math.max(0, Math.floor(Number(n) || 0));
  const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = n % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
export function fmtDur(n) {
  n = Math.max(0, Math.round(Number(n) || 0));
  if (n < 60) return `${n}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m ${n % 60}s`;
  return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
}
export function fmtNum(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(0) + "k";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
export function fmtCost(n, estimated = false) { n = Number(n) || 0; return n ? `${estimated ? "~" : ""}$${n.toFixed(n < 1 ? 3 : 2)}` : "—"; }
export function timeAgo(iso) {
  if (!iso) return "";
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  if (!t) return "";
  const d = (Date.now() - t) / 1000;
  if (d < 45) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)}d ago`;
  return new Date(t).toLocaleDateString();
}
export function fmtTime(iso) {
  if (!iso) return "";
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  return t ? new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : String(iso);
}
export function fmtDateTime(iso) {
  const t = Date.parse(iso || "");
  return t ? new Date(t).toLocaleString() : (iso || "");
}
export const basename = (p) => String(p || "").split(/[\\/]/).filter(Boolean).pop() || p || "";

// ---------------------------------------------------------------------------- markdown (safe, small)
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  return s;
}
export function md(text) {
  if (!text) return "";
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```(\w+)?\s*$/);
    if (fence) {
      flushPara();
      const lang = fence[1] || "";
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      out.push(`<pre class="md-code" data-lang="${esc(lang)}"><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushPara(); out.push("<hr>"); i++; continue; }
    const ul = line.match(/^(\s*)[-*+]\s+(.*)$/);
    const ol = line.match(/^(\s*)\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const tag = ul ? "ul" : "ol";
      const items = [];
      const re = ul ? /^(\s*)[-*+]\s+(.*)$/ : /^(\s*)\d+[.)]\s+(.*)$/;
      while (i < lines.length) {
        const m = lines[i].match(re);
        if (m) { items.push(m[2]); i++; continue; }
        if (items.length && /^\s{2,}\S/.test(lines[i]) && !lines[i].match(/^\s*```/)) { items[items.length - 1] += "\n" + lines[i].trim(); i++; continue; }
        break;
      }
      out.push(`<${tag}>${items.map((it) => {
        const task = it.match(/^\[([ xX])\]\s+(.*)$/s);
        if (task) return `<li class="task ${task[1] !== " " ? "done" : ""}"><span class="cb">${task[1] !== " " ? icon("check") : ""}</span>${inline(task[2]).replace(/\n/g, "<br>")}</li>`;
        return `<li>${inline(it).replace(/\n/g, "<br>")}</li>`;
      }).join("")}</${tag}>`);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      flushPara();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
      out.push(`<blockquote>${md(buf.join("\n"))}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flushPara();
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i]); i++; }
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(rows[0]);
      const body = rows.slice(2).map(cells);
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (!line.trim()) { flushPara(); i++; continue; }
    para.push(line);
    i++;
  }
  flushPara();
  return out.join("");
}

// ---------------------------------------------------------------------------- toasts
let toastHost;
export function toast(level, title, body = "", opts = {}) {
  if (!toastHost) { toastHost = el('<div class="toast-host" aria-live="polite"></div>'); document.body.appendChild(toastHost); }
  const ico = { success: "check", error: "alert", warning: "alert", info: "info" }[level] || "info";
  const t = el(`<div class="toast ${esc(level)}" role="status">
    <span class="toast-ic">${icon(ico)}</span>
    <div class="toast-copy"><strong>${esc(title)}</strong>${body ? `<span>${esc(body)}</span>` : ""}</div>
    ${opts.action ? `<button class="toast-action">${esc(opts.action.label)}</button>` : ""}
    <button class="toast-x" aria-label="Dismiss">${icon("x")}</button>
  </div>`);
  const close = () => { t.classList.add("out"); setTimeout(() => t.remove(), 220); };
  t.querySelector(".toast-x").onclick = close;
  if (opts.action) t.querySelector(".toast-action").onclick = () => { opts.action.onClick(); close(); };
  toastHost.appendChild(t);
  requestAnimationFrame(() => t.classList.add("in"));
  if (!opts.sticky) setTimeout(close, opts.duration || (level === "error" ? 9000 : 5000));
  return t;
}

// ---------------------------------------------------------------------------- modals
export function modal(html, { wide = false, onClose } = {}) {
  const back = el(`<div class="modal-backdrop"><div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">${html}</div></div>`);
  document.body.appendChild(back);
  const close = () => { back.classList.add("out"); setTimeout(() => back.remove(), 160); document.removeEventListener("keydown", onKey); onClose && onClose(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  $$("[data-close]", back).forEach((b) => (b.onclick = close));
  requestAnimationFrame(() => back.classList.add("in"));
  const first = back.querySelector("input,textarea,select,button:not([data-close])");
  if (first) setTimeout(() => first.focus(), 30);
  return { root: back, body: back.firstElementChild, close };
}
export function confirm(title, body, { danger = false, okLabel = "Confirm" } = {}) {
  return new Promise((resolve) => {
    const m = modal(`<h2>${esc(title)}</h2><p class="hint">${esc(body)}</p>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn ${danger ? "danger" : "primary"}" id="okBtn">${esc(okLabel)}</button></div>`,
      { onClose: () => resolve(false) });
    $("#okBtn", m.body).onclick = () => { resolve(true); m.close(); };
  });
}
export function prompt(title, body, { placeholder = "", value = "", okLabel = "OK", multiline = false } = {}) {
  return new Promise((resolve) => {
    const m = modal(`<h2>${esc(title)}</h2>${body ? `<p class="hint">${esc(body)}</p>` : ""}
      <div class="field">${multiline ? `<textarea id="pInput" rows="5" placeholder="${esc(placeholder)}">${esc(value)}</textarea>` : `<input id="pInput" value="${esc(value)}" placeholder="${esc(placeholder)}">`}</div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="okBtn">${esc(okLabel)}</button></div>`,
      { onClose: () => resolve(null) });
    const input = $("#pInput", m.body);
    $("#okBtn", m.body).onclick = () => { const v = input.value; m.close(); resolve(v); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (!multiline || e.ctrlKey || e.metaKey)) { e.preventDefault(); const v = input.value; m.close(); resolve(v); } });
  });
}

// ---------------------------------------------------------------------------- dropdown menu
export function menu(anchor, items) {
  $$(".menu-pop").forEach((m) => m.remove());
  const pop = el(`<div class="menu-pop" role="menu">${items.map((it) => it === "-" ? '<div class="menu-sep"></div>' :
    `<button class="menu-item ${it.danger ? "danger" : ""}" ${it.disabled ? "disabled" : ""} data-k="${esc(it.key || it.label)}">${it.icon ? icon(it.icon) : ""}<span>${esc(it.label)}</span>${it.hint ? `<kbd>${esc(it.hint)}</kbd>` : ""}</button>`).join("")}</div>`);
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.top = `${Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 8)}px`;
  pop.style.left = `${Math.min(r.left, innerWidth - pop.offsetWidth - 8)}px`;
  const close = () => { pop.remove(); document.removeEventListener("mousedown", onDoc, true); document.removeEventListener("keydown", onKey); };
  const onDoc = (e) => { if (!pop.contains(e.target)) close(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  setTimeout(() => { document.addEventListener("mousedown", onDoc, true); document.addEventListener("keydown", onKey); }, 0);
  $$(".menu-item", pop).forEach((b) => (b.onclick = () => { const it = items.find((x) => x !== "-" && (x.key || x.label) === b.dataset.k); close(); it && it.onClick && it.onClick(); }));
  return pop;
}

// ---------------------------------------------------------------------------- command palette
// Items: { group, label, sub, icon, hint, keywords, onClick, always }. getItems(query) may add items for the query
// (for example "Create task: …"); matching ranks prefix, word-start, substring, then in-order letters.
export function fuzzyScore(query, text) {
  const q = query.toLowerCase().trim(), t = String(text || "").toLowerCase();
  if (!q) return 1;
  if (t.startsWith(q)) return 100 - Math.min(40, t.length / 10);
  const i = t.indexOf(q);
  if (i >= 0) return (/[\s/#:·._-]/.test(t[i - 1] || "") ? 80 : 60) - Math.min(20, i / 5);
  const words = q.split(/\s+/).filter(Boolean);
  if (words.length > 1 && words.every((w) => t.includes(w))) return 50;
  let j = 0, gaps = 0;
  for (const c of t) { if (c === q[j]) j++; else if (j) gaps++; if (j === q.length) break; }
  return j === q.length ? Math.max(1, 30 - gaps / 2) : 0;
}
export function palette(getItems, { initial = "", placeholder = "Type a command or task name…" } = {}) {
  $$(".palette-back").forEach((p) => p.remove());
  const opener = document.activeElement;
  const back = el(`<div class="palette-back"><div class="palette" role="dialog" aria-modal="true" aria-label="Command palette">
    <div class="palette-input">${icon("search")}<input role="combobox" aria-expanded="true" aria-controls="paletteList" aria-autocomplete="list" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false"></div>
    <div class="palette-list" id="paletteList" role="listbox"></div>
    <div class="palette-foot"><span><kbd>↑↓</kbd> move</span><span><kbd>↵</kbd> run</span><span><kbd>esc</kbd> close</span><span class="palette-tip">Tip: <b>create task:</b> add retries to the api client</span></div>
  </div></div>`);
  document.body.appendChild(back);
  const input = $("input", back), list = $(".palette-list", back);
  input.value = initial;
  let sel = 0, shown = [];
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey, true); try { opener?.focus?.(); } catch {} };
  const render = () => {
    const q = input.value;
    const items = getItems(q) || [];
    const scored = items.map((it, i) => ({ it, i, s: it.always ? 1000 - i : fuzzyScore(q, `${it.label} ${it.keywords || ""} ${it.sub || ""}`) })).filter((x) => x.s > 0);
    const groups = new Map();
    for (const x of scored) { const g = x.it.group || ""; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(x); }
    const ordered = [...groups.values()].map((xs) => (q.trim() ? xs.sort((a, b) => b.s - a.s || a.i - b.i) : xs))
      .sort((a, b) => (q.trim() ? Math.max(...b.map((x) => x.s)) - Math.max(...a.map((x) => x.s)) : a[0].i - b[0].i));
    shown = ordered.flat().slice(0, 60).map((x) => x.it);
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    let lastGroup = null;
    list.innerHTML = shown.length ? shown.map((it, i) => {
      const g = it.group !== lastGroup ? `<div class="palette-group" role="presentation">${esc(it.group || "")}</div>` : "";
      lastGroup = it.group;
      return g + `<button type="button" class="palette-item ${i === sel ? "sel" : ""}" role="option" aria-selected="${i === sel}" id="pi-${i}" data-i="${i}">${it.icon ? `<span class="pi-ic">${icon(it.icon)}</span>` : ""}<span class="pi-main"><span class="pi-label">${esc(it.label)}</span>${it.sub ? `<span class="pi-sub">${esc(it.sub)}</span>` : ""}</span>${it.hint ? `<kbd>${esc(it.hint)}</kbd>` : ""}</button>`;
    }).join("") : '<div class="palette-empty">No matches. Try “create task: …” to start new work.</div>';
    input.setAttribute("aria-activedescendant", shown.length ? `pi-${sel}` : "");
    $$(".palette-item", list).forEach((b) => (b.onclick = () => run(Number(b.dataset.i))));
    const s = $(".palette-item.sel", list); s && s.scrollIntoView({ block: "nearest" });
  };
  const run = (i) => { const it = shown[i]; close(); it && it.onClick && it.onClick(); };
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, shown.length - 1); render(); }
    if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); render(); }
    if (e.key === "Enter") { e.preventDefault(); run(sel); }
  };
  document.addEventListener("keydown", onKey, true);
  input.addEventListener("input", () => { sel = 0; render(); });
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  render();
  setTimeout(() => { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }, 10);
}

export function copyText(text) {
  try { navigator.clipboard.writeText(text); toast("success", "Copied to clipboard"); } catch { toast("error", "Copy failed"); }
}

export function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function throttle(fn, ms) { let last = 0, t; return (...a) => { const n = Date.now(); if (n - last > ms) { last = n; fn(...a); } else { clearTimeout(t); t = setTimeout(() => { last = Date.now(); fn(...a); }, ms - (n - last)); } }; }

// diff → html
export function diffHtml(text) {
  if (!text) return '<div class="empty small">No textual diff.</div>';
  return `<pre class="diff">${text.split("\n").map((l) => {
    let cls = "";
    if (l.startsWith("+++") || l.startsWith("---")) cls = "meta";
    else if (l.startsWith("@@")) cls = "hunk";
    else if (l.startsWith("+")) cls = "add";
    else if (l.startsWith("-")) cls = "del";
    else if (l.startsWith("diff ") || l.startsWith("index ")) cls = "meta";
    return `<span class="dl ${cls}">${esc(l)}</span>`;
  }).join("\n")}</pre>`;
}
