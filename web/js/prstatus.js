// Live pull request status, shared by the task header and the Try it tab.
import { esc, icon, timeAgo } from "./ui.js";
import { api } from "./api.js";
import { bus } from "./state.js";

// The server already caches gh for a minute; this keeps both views on one request
// and lets a refresh in one of them repaint the other.
const cache = new Map(); // taskId -> { data, at, promise }
const FRESH_MS = 60000;

export function prCached(id) { return cache.get(id)?.data || null; }

export function prStatus(id, force = false) {
  const hit = cache.get(id);
  if (!force && hit?.promise) return hit.promise;
  if (!force && hit?.data && Date.now() - hit.at < FRESH_MS) return Promise.resolve(hit.data);
  const promise = api.pr(id, force)
    .catch((e) => ({ ok: false, error: e.message }))
    .then((data) => {
      cache.set(id, { data, at: Date.now(), promise: null });
      bus.emit("pr", id, data);
      return data;
    });
  cache.set(id, { data: hit?.data || null, at: hit?.at || 0, promise });
  return promise;
}

export const PR_STATE = {
  draft: { label: "Draft", tone: "muted" },
  open: { label: "Open", tone: "green" },
  merged: { label: "Merged", tone: "purple" },
  closed: { label: "Closed", tone: "red" },
};

export const REVIEW_LABEL = {
  APPROVED: ["Approved", "green"], CHANGES_REQUESTED: ["Changes requested", "red"], REVIEW_REQUIRED: ["Review required", "amber"],
};

export function checksLabel(c) {
  if (!c || !c.total) return "No checks";
  if (c.failing) return `${c.failing} failing`;
  if (c.pending) return `${c.pending} pending`;
  return `${c.passing} passing`;
}

export function checksDetail(c) {
  if (!c || !c.total) return "No checks reported on this pull request";
  return [c.passing && `${c.passing} passing`, c.failing && `${c.failing} failing`, c.pending && `${c.pending} pending`, c.skipped && `${c.skipped} skipped`]
    .filter(Boolean).join(" · ") + (c.failed_names?.length ? ` (${c.failed_names.join(", ")})` : "");
}

const CHECK_ICON = { passing: "check", failing: "x", pending: "clock" };

// Header pill. Before the first answer it keeps the same shape, so nothing jumps when it arrives.
export function prPill(t, pr) {
  const n = esc(t.pr_number || pr?.number || "");
  const href = esc(pr?.url || t.pr_url || "");
  if (!pr) return `<a class="pr-pill loading" href="${href}" target="_blank" rel="noopener" title="Checking pull request status…">${icon("github", "sm")}<span>PR #${n}</span><span class="pr-state">…</span></a>`;
  if (!pr.ok) return `<a class="pr-pill error" href="${href}" target="_blank" rel="noopener" title="${esc(pr.error || "Status unavailable")}">${icon("github", "sm")}<span>PR #${n}</span><span class="pr-state">${icon("alert", "sm")}unknown</span></a>`;
  const st = PR_STATE[pr.state] || PR_STATE.open;
  const c = pr.checks || {};
  const checks = c.total && pr.state !== "merged" && pr.state !== "closed"
    ? `<span class="pr-checks ${esc(c.state)}">${icon(CHECK_ICON[c.state] || "check", "sm")}${esc(checksLabel(c))}</span>` : "";
  const title = `${pr.title || ""}\n${st.label}${pr.merged_at ? ` ${timeAgo(pr.merged_at)}` : ""} · ${checksDetail(c)}${pr.stale ? `\nShowing the last known status: ${pr.error}` : ""}`;
  return `<a class="pr-pill ${esc(st.tone)}" href="${href}" target="_blank" rel="noopener" title="${esc(title)}">${icon("github", "sm")}<span>PR #${n}</span><span class="pr-state"><span class="dot"></span>${esc(st.label)}</span>${checks}</a>`;
}
