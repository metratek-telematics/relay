// Relay UI visual QA: every screen at 1440/1024/768/420 in both themes, with automated checks for horizontal
// scroll, clipped or hidden text, low contrast (WCAG AA), off-centre avatars/badges/icons and wrapped buttons.
//
//   BASE=http://127.0.0.1:8767/ node tools/ui_qa.mjs <out-dir> <routes.json>
//   routes.json: [{"name": "home", "hash": "/", "wait": 2200, "click": "#paletteBtn", "type": "create task: …"}]
//   SIZES=1440x900,420x860 THEMES=dark SHOTS=0 limit the run; one process per size and theme runs them in parallel.
//
// Writes <out-dir>/qa.json and a PNG per route, size and theme; prints a one-line summary.
import { chromium } from "playwright-core";
import fs from "fs";

const out = process.argv[2];
const routes = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const base = process.env.BASE || "http://127.0.0.1:8831/";
const sizes = (process.env.SIZES || "1440x900,1024x768,768x1024,420x860").split(",").map((s) => s.split("x").map(Number));
const themes = (process.env.THEMES || "light,dark").split(",");
const shots = process.env.SHOTS !== "0";
fs.mkdirSync(out, { recursive: true });

function audit() {
  const res = { hscroll: 0, clipped: [], lowContrast: [], offCenter: [], wrappedButtons: [], overflow: [] };
  res.hscroll = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth;
  const visible = (el) => { const r = el.getBoundingClientRect(); if (r.width < 1 || r.height < 1) return false; const cs = getComputedStyle(el); return cs.visibility !== "hidden" && cs.display !== "none" && Number(cs.opacity) > 0.05 && r.bottom > 0 && r.top < innerHeight * 3; };
  const sig = (el) => { let s = el.tagName.toLowerCase(); if (el.id) s += "#" + el.id; if (el.className && typeof el.className === "string") s += "." + el.className.trim().split(/\s+/).slice(0, 3).join("."); const p = el.parentElement; return (p && p.className && typeof p.className === "string" ? p.className.trim().split(/\s+/)[0] + " > " : "") + s; };
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const blend = (top, bottom) => ({ r: top.r * top.a + bottom.r * (1 - top.a), g: top.g * top.a + bottom.g * (1 - top.a), b: top.b * top.a + bottom.b * (1 - top.a), a: 1 });
  const bgOf = (el) => {
    const layers = [];
    for (let n = el; n; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== "none" && !n.classList.contains("av")) return null; // images or gradients: skip
      const c = parse(cs.backgroundColor);
      if (c && c.a > 0) { layers.push(c); if (c.a >= 0.99) break; }
    }
    let acc = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i--) acc = blend(layers[i], acc);
    return acc;
  };
  const textEls = [...document.querySelectorAll("body *")].filter((el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()) && visible(el));
  for (const el of textEls) {
    if (el.closest("pre, code, .diff, svg, .th-pane, .dx-view, .log-view, .code-editor, textarea, select, option, .sr-only, [aria-hidden='true']")) continue;
    const cs = getComputedStyle(el);
    // contrast
    const fg = parse(cs.color), bg = bgOf(el);
    if (fg && bg && fg.a > 0.05) {
      const f = fg.a < 1 ? blend(fg, bg) : fg;
      const L1 = lum(f), L2 = lum(bg), ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
      const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 600;
      const need = size >= 18 || (size >= 14 && bold) ? 3 : 4.5;
      const disabled = el.closest(":disabled, [disabled], .muted-disabled");
      if (!disabled && ratio < need - 0.25) res.lowContrast.push({ sig: sig(el), text: el.textContent.trim().slice(0, 40), ratio: Math.round(ratio * 100) / 100, need });
    }
    // clipped text (hidden without an ellipsis) and truncation
    if (el.scrollWidth > el.clientWidth + 2 && ["hidden", "clip"].includes(cs.overflowX)) {
      if (cs.textOverflow !== "ellipsis" && !el.closest(".act-text, .truncate")) res.clipped.push({ sig: sig(el), text: el.textContent.trim().slice(0, 50), sw: el.scrollWidth, cw: el.clientWidth });
    }
    if (el.scrollHeight > el.clientHeight + 3 && ["hidden", "clip"].includes(cs.overflowY) && cs.webkitLineClamp === "none" && cs.display !== "-webkit-box" && el.clientHeight > 0 && el.clientHeight < 30) {
      res.clipped.push({ sig: sig(el), text: el.textContent.trim().slice(0, 50), sh: el.scrollHeight, ch: el.clientHeight, axis: "y" });
    }
  }
  // text escaping its container visibly (overlapping neighbours)
  for (const el of [...document.querySelectorAll(".btn, .badge, .status-chip, .chip, .chip-toggle, .repo-chip, .pr-chip, .count-pill, .nav-item, .seg button, .tabbar-inline a, .tp-views button")].filter(visible)) {
    const r = el.getBoundingClientRect();
    if (el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX === "visible") res.overflow.push({ sig: sig(el), text: el.textContent.trim().slice(0, 40), sw: el.scrollWidth, cw: el.clientWidth });
    if (el.matches(".btn") && !el.matches(".nx-opt")) {
      const lh = parseFloat(getComputedStyle(el).lineHeight) || 18;
      if (r.height > 44 || (el.textContent.trim() && r.height > lh * 2 + 12)) res.wrappedButtons.push({ sig: sig(el), text: el.textContent.trim().slice(0, 40), h: Math.round(r.height), w: Math.round(r.width) });
    }
  }
  // centring of small round things
  for (const el of [...document.querySelectorAll(".av, .org-av, .count-pill, .nav-count, .ql-pos, .wc-pos, .dx-order, .ml-step, .mp-n, .ho-n, .pi-ic, .notif-ic, .wcol-ic, .st-dot, .ol-dot, .es-art, .empty-state > .ic, .fail-mark, .tv-badge, .step, .ring-score, .org-ch-ic")].filter(visible)) {
    const box = el.getBoundingClientRect();
    if (box.width > 90) continue;
    let inner = null;
    const kid = [...el.children].find((c) => c.tagName !== "IMG" && visible(c) && !c.classList.contains("sr-only") && getComputedStyle(c).position !== "absolute");
    if (kid) inner = kid.tagName.toLowerCase() === "svg" ? kid.getBoundingClientRect() : (() => { const rg = document.createRange(); rg.selectNodeContents(kid); const rr = rg.getBoundingClientRect(); return rr.width ? rr : kid.getBoundingClientRect(); })();
    else if ([...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) { const rg = document.createRange(); rg.selectNodeContents(el); inner = rg.getBoundingClientRect(); }
    if (!inner || !inner.width) continue;
    if (getComputedStyle(el).color === "rgba(0, 0, 0, 0)" || getComputedStyle(el).color.endsWith(", 0)")) continue; // logo avatars hide their initials
    const dx = (inner.left + inner.width / 2) - (box.left + box.width / 2);
    const dy = (inner.top + inner.height / 2) - (box.top + box.height / 2);
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) res.offCenter.push({ sig: sig(el), text: el.textContent.trim().slice(0, 10), dx: Math.round(dx * 10) / 10, dy: Math.round(dy * 10) / 10, w: Math.round(box.width), h: Math.round(box.height) });
    if (/[\u00c2\u00e2\u0080-\u009f\u200b-\u200f]/.test(el.textContent)) res.offCenter.push({ sig: sig(el), text: JSON.stringify(el.textContent), issue: "invisible or mojibake characters" });
  }
  const dedupe = (xs) => { const seen = new Map(); for (const x of xs) { const k = x.sig + "|" + (x.issue || ""); if (!seen.has(k)) seen.set(k, { ...x, n: 1 }); else seen.get(k).n++; } return [...seen.values()]; };
  for (const k of ["clipped", "lowContrast", "offCenter", "wrappedButtons", "overflow"]) res[k] = dedupe(res[k]);
  return res;
}

const browser = await chromium.launch();
const report = [];
for (const theme of themes) {
  for (const [w, h] of sizes) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
    const page = await ctx.newPage();
    const errors = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    for (const r of routes) {
      errors.length = 0;
      await page.keyboard.press("Escape").catch(() => {});
      await page.evaluate((hash) => { document.querySelectorAll(".palette-back, .modal-backdrop, .menu-pop, .org-pop").forEach((x) => x.remove()); location.hash = hash; }, "#" + r.hash);
      await page.waitForTimeout(r.wait || 2200);
      if (r.click) { try { await page.click(r.click, { timeout: 3000 }); await page.waitForTimeout(900); } catch { errors.push("click failed " + r.click); } }
      if (r.type) { await page.keyboard.type(r.type, { delay: 15 }); await page.waitForTimeout(500); }
      const res = await page.evaluate(audit);
      const file = `${out}/${r.name}-${w}-${theme}.png`;
      if (shots) await page.screenshot({ path: file });
      report.push({ route: r.name, w, theme, file, errors: errors.filter((e) => !/ERR_NETWORK_CHANGED|favicon/.test(e)), ...res });
    }
    await ctx.close();
  }
}
await browser.close();
fs.writeFileSync(`${out}/qa.json`, JSON.stringify(report, null, 1));
const sum = { pages: report.length, hscroll: report.filter((r) => r.hscroll > 0).length, errors: report.filter((r) => r.errors.length).length };
for (const k of ["clipped", "lowContrast", "offCenter", "wrappedButtons", "overflow"]) sum[k] = report.reduce((a, r) => a + r[k].length, 0);
console.log(JSON.stringify(sum));
