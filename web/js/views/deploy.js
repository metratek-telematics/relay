// Deploy: what "deployed" means for each repository, and the switches that let a merge do it.
// The map itself is owner configuration on the server (orchestrator/deploy.py, DATA_DIR/deploy.json):
// this page shows every target and posts back only `enabled` and `auto_on_merge`. Commands are never
// sent from the browser.
import { $, $$, esc, icon, toast, confirm } from "../ui.js";
import { api } from "../api.js";

const METHOD_LABEL = {
  actions_watch: "Follow the repository's own run",
  actions_dispatch: "Dispatch a workflow",
  image_pull: "Pull the image, recreate the service",
  compose_build: "Build on the host, recreate the service",
  manual: "A person does it",
};
const METHOD_SHORT = {
  actions_watch: "actions · watch", actions_dispatch: "actions · dispatch",
  image_pull: "image pull", compose_build: "host build", manual: "manual",
};
const TONE = { succeeded: "green", failed: "red", refused: "amber", blocked: "amber", manual: "outline", skipped: "outline", running: "amber" };

const when = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");

function lastRun(t) {
  const r = t.last_run;
  if (!r) return `<span class="muted">never run</span>`;
  return `<span class="badge ${TONE[r.status] || "outline"}">${esc(r.status)}</span> <span class="muted">${esc(when(r.finished_at || r.started_at))}${r.duration ? ` · ${Math.round(r.duration)}s` : ""}</span>`;
}

function flags(t) {
  const out = [];
  if (t.critical) out.push(`<span class="badge red" title="Never deployed automatically: only from the Run now button.">critical</span>`);
  if (t.never_pull) out.push(`<span class="badge amber" title="This image is not on Docker Hub. A pull is refused.">never pull</span>`);
  return out.join(" ");
}

function targetRow(t) {
  const auto = t.enabled && t.auto_on_merge && !t.critical;
  return `<div class="dep-row" data-target="${esc(t.id)}">
    <div class="dep-main">
      <div class="row" style="gap:8px;align-items:baseline"><strong class="truncate">${esc(t.service || t.workflow || t.site || t.id)}</strong>
        <span class="badge outline" title="${esc(METHOD_LABEL[t.method] || t.method)}">${esc(METHOD_SHORT[t.method] || t.method)}</span>
        <span class="badge outline">${esc(t.host)}</span>${flags(t)}</div>
      <div class="muted small truncate" title="${esc(t.risk || t.note || "")}">${esc(t.risk || t.note || "")}</div>
      ${(t.commands || []).length ? `<details class="dep-cmds"><summary class="muted small">${(t.commands || []).length} command(s)</summary><pre class="mono small">${esc((t.commands || []).join("\n"))}</pre></details>` : ""}
    </div>
    <div class="dep-run">${lastRun(t)}</div>
    <div class="dep-switches">
      <label class="row" style="gap:6px"><span class="switch ${t.enabled ? "on" : ""}" data-sw="enabled" role="switch" tabindex="0" aria-checked="${!!t.enabled}" aria-label="Enabled"></span><span class="muted small">enabled</span></label>
      <label class="row" style="gap:6px"><span class="switch ${auto ? "on" : ""} ${t.critical ? "disabled" : ""}" data-sw="auto_on_merge" role="switch" tabindex="0" aria-checked="${auto}" aria-label="Automatic on merge"></span><span class="muted small">${t.critical ? "automatic (blocked: critical)" : "on merge"}</span></label>
    </div>
    <div class="dep-actions"><button type="button" class="btn sm" data-run ${t.enabled ? "" : "disabled"} title="${t.enabled ? "Run this recipe now" : "Enable this target first"}">${icon("play", "sm")}Run now</button></div>
  </div>`;
}

export function mountDeploy(body) {
  const load = async () => {
    let d;
    try { d = await api.deployTargets(); } catch (e) { body.innerHTML = `<div class="card"><div class="card-body"><div class="empty small">${icon("alert")} ${esc(e.message)}</div></div></div>`; return; }
    draw(d);
  };

  const draw = (d) => {
    const targets = d.targets || [];
    const repos = [...new Set(targets.map((t) => t.repo))].sort();
    const on = targets.filter((t) => t.enabled).length;
    const auto = targets.filter((t) => t.enabled && t.auto_on_merge && !t.critical).length;
    const hosts = Object.entries(d.hosts || {}).map(([k, v]) => `${k}: ${v.ssh ? esc(v.ssh) : "no SSH host configured — nothing runs there"}`).join(" · ");
    body.innerHTML = `
      <div class="card"><div class="card-head"><div><h3>Deploy</h3><p class="card-sub">What a merge should make live, per repository. Seeded from the deployment scans in Knowledge (<span class="mono small">knowledge/deploy</span>) and edited on the server; the browser only flips the two switches.</p></div></div>
        <div class="card-body">
          <div class="row wrap" style="gap:14px"><span class="muted small">${targets.length} targets · ${on} enabled · ${auto} automatic on merge${d.seeded_at ? ` · seeded ${esc(when(d.seeded_at))}` : ""}</span></div>
          <p class="hint">A target only runs when you enable it. <strong>Critical</strong> targets (the laser and berthing services, the databases, the routers) never run automatically — only from Run now. A <strong>never pull</strong> image exists on the host only, so pulling it is refused rather than attempted. One deployment runs at a time.</p>
          <p class="hint">${hosts || "No hosts configured."}</p>
        </div></div>
      ${repos.length ? repos.map((r) => `<div class="card"><div class="card-head"><h3>${esc(r)}</h3><span class="muted small">${targets.filter((t) => t.repo === r).length} target(s)</span></div>
        <div class="card-body dep-list">${targets.filter((t) => t.repo === r).map(targetRow).join("")}</div></div>`).join("")
        : `<div class="card"><div class="card-body"><div class="empty small">No deployment map yet. Seed it with <span class="mono">python3 tools/seed_deploy_map.py</span>.</div></div></div>`}`;
    bind(targets);
  };

  const bind = (targets) => {
    $$(".dep-row", body).forEach((row) => {
      const id = row.dataset.target;
      const t = targets.find((x) => x.id === id) || {};
      $$("[data-sw]", row).forEach((sw) => {
        const flip = async () => {
          const key = sw.dataset.sw;
          if (key === "auto_on_merge" && t.critical) { toast("info", "Critical target", "A critical target is never deployed automatically. Use Run now."); return; }
          const next = !sw.classList.contains("on");
          sw.classList.toggle("on", next);
          sw.setAttribute("aria-checked", String(next));
          try {
            Object.assign(t, await api.saveDeployTarget(id, { [key]: next }));
            const run = $("[data-run]", row);   // Run now follows the enabled switch without a reload
            if (run) { run.disabled = !t.enabled; run.title = t.enabled ? "Run this recipe now" : "Enable this target first"; }
          } catch (e) { sw.classList.toggle("on", !next); sw.setAttribute("aria-checked", String(!next)); toast("error", "Could not save", e.message); }
        };
        sw.onclick = flip;
        sw.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } };
      });
      const btn = $("[data-run]", row);
      if (btn) btn.onclick = async () => {
        if (t.critical && !(await confirm("Deploy a critical target?", `${t.service || t.id} on ${t.host}. ${t.risk || ""}`, { danger: true, okLabel: "Run it" }))) return;
        btn.disabled = true;
        btn.innerHTML = `${icon("spinner", "sm spin")}Running`;
        try {
          const r = await api.runDeployTarget(id);
          toast(r.status === "succeeded" ? "success" : r.status === "manual" ? "info" : "error", `Deploy ${r.status}`, `${r.detail || ""}`);
        } catch (e) { toast("error", "Deploy failed", e.message); }
        await load();
      };
    });
  };

  load();
  return { destroy() {} };
}
