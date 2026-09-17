// The primary navigation: rail/expanded on the desktop, an overlay drawer on tablets and phones.
//
// One class on <html> drives the layout — `nav-rail` (icons only) — and one drives the overlay — `nav-open`.
// The width and the label opacity are custom properties (tokens.css) that interpolate, so collapsing is a
// single 200 ms animation of the shell rather than a snap between two layouts. The state is applied before
// the first paint by the inline script in index.html; this module only keeps it in sync.
import { $, $$ } from "./ui.js";
import { S, bus } from "./state.js";

const root = document.documentElement;
const nav = $(".nav");
const scrim = $("#navScrim");
const KEY = "relay.navCollapsed";
const DRAWER_MAX = 1240;   // at or below this the toggle opens an overlay instead of widening the column
const PHONE_MAX = 760;     // below this the navigation is off-canvas entirely

export const isPhone = () => innerWidth <= PHONE_MAX;
export const isOverlay = () => innerWidth <= DRAWER_MAX;
const collapsed = () => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } };
// The board wants six columns; it only folds the navigation when the window is too narrow for both.
const boardView = () => S.route?.view === "work" && innerWidth < 1560;

// The rail is the state of the column, not of the drawer: the drawer always shows the full navigation.
export function syncNav() {
  const rail = !isPhone() && (collapsed() || isOverlay() || boardView());
  root.classList.toggle("nav-rail", rail);
  const open = root.classList.contains("nav-open");
  const btn = $("#navCollapse");
  if (btn) {
    const expanded = open || !rail;
    btn.setAttribute("aria-expanded", String(expanded));
    const label = isOverlay() ? (open ? "Close navigation" : "Open navigation") : expanded ? "Collapse navigation" : "Expand navigation";
    btn.setAttribute("aria-label", label);
    btn.dataset.tip = label;
  }
  if (scrim) scrim.hidden = !open && !isOverlay();
}

// ---------------------------------------------------------------------------- drawer
let lastFocus = null;
export function openDrawer() {
  if (root.classList.contains("nav-open")) return;
  lastFocus = document.activeElement;
  root.classList.add("nav-open");
  if (scrim) scrim.hidden = false;
  syncNav();
  setTimeout(() => $(".nav-item", nav)?.focus(), 60);
}
export function closeDrawer({ restoreFocus = true } = {}) {
  if (!root.classList.contains("nav-open")) return;
  root.classList.remove("nav-open");
  syncNav();
  if (restoreFocus) { try { lastFocus?.focus?.(); } catch {} }
  setTimeout(() => { if (scrim && !root.classList.contains("nav-open")) scrim.hidden = !isOverlay() ? true : scrim.hidden; }, 220);
}
export const drawerOpen = () => root.classList.contains("nav-open");

// ---------------------------------------------------------------------------- toggle
export function toggleNav() {
  if (isOverlay()) { drawerOpen() ? closeDrawer() : openDrawer(); return; }
  const next = !collapsed();
  try { localStorage.setItem(KEY, next ? "1" : "0"); } catch {}
  syncNav();
}

// ---------------------------------------------------------------------------- rail tooltips
// The rail clips its own overflow, so the tip is drawn in the body next to the row it belongs to.
let tip = null, tipFor = null;
function showTip(target) {
  if (!root.classList.contains("nav-rail") || drawerOpen() || !target.dataset.tip) return;
  if (!tip) { tip = document.createElement("div"); tip.className = "nav-tip"; tip.setAttribute("role", "tooltip"); document.body.appendChild(tip); }
  tipFor = target;
  tip.innerHTML = `${target.dataset.tip.replace(/[<>&]/g, "")}${target.dataset.tipKbd ? `<kbd>${target.dataset.tipKbd.replace(/[<>&]/g, "")}</kbd>` : ""}`;
  const r = target.getBoundingClientRect();
  tip.style.left = `${Math.round(r.right + 10)}px`;
  tip.style.top = `${Math.round(r.top + r.height / 2 - tip.offsetHeight / 2)}px`;
  requestAnimationFrame(() => tip && tip.classList.add("in"));
}
function hideTip(target) {
  if (!tip || (target && target !== tipFor)) return;
  tip.classList.remove("in");
  tipFor = null;
}

export function mountNav() {
  // Native tooltips would double up with the rail's own; the wording lives on data-tip.
  $$("[data-tip]", nav).forEach((b) => { if (b.title) { b.dataset.title = b.title; b.removeAttribute("title"); } });
  $("#navCollapse").onclick = toggleNav;
  scrim?.addEventListener("click", () => closeDrawer());

  for (const b of $$("[data-tip]", nav)) {
    b.addEventListener("mouseenter", () => showTip(b));
    b.addEventListener("mouseleave", () => hideTip(b));
    b.addEventListener("focus", () => showTip(b));
    b.addEventListener("blur", () => hideTip(b));
    b.addEventListener("click", () => hideTip(b));
  }
  nav.addEventListener("scroll", () => hideTip(), { passive: true });

  // Escape closes the drawer; the focus stays inside it while it is open.
  nav.addEventListener("keydown", (e) => {
    if (!drawerOpen()) return;
    if (e.key === "Escape") { e.stopPropagation(); closeDrawer(); return; }
    if (e.key !== "Tab") return;
    const f = $$('a[href], button:not([disabled]), input, [tabindex]:not([tabindex="-1"])', nav).filter((x) => x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  // A swipe to the left closes it, the way a native drawer does.
  let x0 = null, y0 = null;
  nav.addEventListener("touchstart", (e) => { if (!drawerOpen()) return; x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, { passive: true });
  nav.addEventListener("touchmove", (e) => {
    if (x0 === null) return;
    const dx = e.touches[0].clientX - x0, dy = e.touches[0].clientY - y0;
    if (dx < -40 && Math.abs(dy) < 40) { x0 = null; closeDrawer(); }
  }, { passive: true });
  nav.addEventListener("touchend", () => { x0 = null; }, { passive: true });
  // Following a link inside the drawer closes it.
  nav.addEventListener("click", (e) => { if (drawerOpen() && e.target.closest("a[href], .nav-new")) closeDrawer({ restoreFocus: false }); });

  addEventListener("resize", () => { if (!isOverlay()) closeDrawer({ restoreFocus: false }); syncNav(); });
  bus.on("route", () => { syncNav(); hideTip(); });
  syncNav();
}
