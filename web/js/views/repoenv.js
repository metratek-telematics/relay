// Repository environment editor: variables, local config files, setup, services and the checks that prove the work.
import { $, $$, esc, icon, toast, modal } from "../ui.js";
import { api } from "../api.js";
import { mountRepoConnectors } from "./connectors.js";

const MASK = "●●●●";
const secretName = (n) => /PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|AUTH|COOKIE|TOTP|PRIVATE/i.test(n || "");

export async function openRepoEnv(repo, { onSaved } = {}) {
  let env;
  try { env = await api.repoEnv(repo.path); } catch (e) { toast("error", "Could not load the environment", e.message); return; }
  const varRow = (v = { name: "", value: "", secret: false }) => `<div class="renv-var">
      <input data-vn value="${esc(v.name)}" placeholder="NAME" spellcheck="false" autocomplete="off">
      <input data-vv type="${v.secret ? "password" : "text"}" value="${esc(v.value)}" placeholder="${v.secret && v.has_value ? "saved (hidden)" : "value"}" spellcheck="false" autocomplete="new-password">
      <label class="renv-secret" title="Hidden in the browser and masked in every log"><input type="checkbox" data-vs ${v.secret ? "checked" : ""}>secret</label>
      <button class="btn xs ghost" data-del title="Remove">${icon("x", "sm")}</button></div>`;
  const fileRow = (f = { path: "", content: "", secret: true }) => `<div class="renv-file">
      <div class="row" style="gap:6px"><input data-fp value="${esc(f.path)}" placeholder="relative/path, e.g. config/local.json" spellcheck="false" style="flex:1">
        <label class="renv-secret"><input type="checkbox" data-fs ${f.secret ? "checked" : ""}>secret</label>
        <button class="btn xs ghost" data-del title="Remove">${icon("x", "sm")}</button></div>
      <textarea data-fc rows="4" spellcheck="false" placeholder="${f.secret && f.has_value ? "saved (hidden); type to replace" : "file contents"}">${esc(f.secret && f.content === MASK ? "" : f.content)}</textarea></div>`;
  const checkRow = (c = { command: "", required: true }) => `<div class="renv-check">
      <input data-cc value="${esc(c.command)}" placeholder="python -m pytest -q" spellcheck="false">
      <label class="renv-secret"><input type="checkbox" data-cr ${c.required ? "checked" : ""}>required</label>
      <button class="btn xs ghost" data-del title="Remove">${icon("x", "sm")}</button></div>`;

  const m = modal(`<h2>${icon("shield")} Environment · ${esc(repo.name)}</h2>
    <p class="hint">Everything a task in this repository needs to run and test the software. It is stored on this server, never committed, applied to every new task's worktree, and secret values are masked in logs and hidden here after saving.</p>
    <div class="renv-sec"><div class="row between"><h3>Variables</h3><span class="row" style="gap:6px"><button class="btn xs" id="rePaste">${icon("file", "sm")}Paste .env</button><button class="btn xs" id="reAddVar">${icon("plus", "sm")}Add</button></span></div>
      <div id="reVars" class="stack" style="gap:6px">${env.vars.map(varRow).join("")}</div>
      <label class="row" style="gap:6px;margin-top:6px"><input type="checkbox" id="reDotenv" ${env.write_dotenv ? "checked" : ""}> Also write them to <code>.env</code> in the worktree (for apps that load dotenv files)</label></div>
    <div class="renv-sec"><div class="row between"><h3>Local files</h3><button class="btn xs" id="reAddFile">${icon("plus", "sm")}Add file</button></div>
      <p class="hint">Config files that are not in git: cookies, <code>config.local.json</code>, certificates.</p>
      <div id="reFiles" class="stack" style="gap:8px">${env.files.map(fileRow).join("")}</div></div>
    <div class="renv-sec"><h3>Setup and services</h3>
      <div class="field"><label>Setup command <span class="muted">(replaces the detected install)</span></label><input id="reSetup" value="${esc(env.setup)}" placeholder="leave empty to detect: npm ci, .venv + requirements + pytest, …" spellcheck="false"></div>
      <div class="field"><label>Start services before the team works</label><input id="reUp" value="${esc(env.services_up)}" placeholder="docker compose up -d db" spellcheck="false"></div>
      <div class="field"><label>Stop services when the task ends</label><input id="reDown" value="${esc(env.services_down)}" placeholder="docker compose down" spellcheck="false"></div></div>
    <div class="renv-sec"><div class="row between"><h3>Checks that prove the work</h3><button class="btn xs" id="reAddCheck">${icon("plus", "sm")}Add check</button></div>
      <p class="hint">Relay runs these itself after each work package. Required checks block delivery; optional ones are reported. Leave empty to detect them.</p>
      <div id="reChecks" class="stack" style="gap:6px">${env.checks.map(checkRow).join("")}</div></div>
    <div class="renv-sec" id="reConnectors"></div>
    <div class="modal-actions"><span class="muted" style="margin-right:auto">${env.updated ? `Saved ${esc(new Date(env.updated).toLocaleString())}` : "Not saved yet"}</span><button class="btn" data-close>Cancel</button><button class="btn primary" id="reSave">Save</button></div>`, { wide: true });

  const bindDel = () => $$("[data-del]", m.body).forEach((b) => (b.onclick = () => b.closest(".renv-var,.renv-file,.renv-check").remove()));
  const bindSecret = () => $$(".renv-var", m.body).forEach((r) => {
    const n = $("[data-vn]", r), v = $("[data-vv]", r), s = $("[data-vs]", r);
    s.onchange = () => { v.type = s.checked ? "password" : "text"; };
    n.onblur = () => { if (!n.dataset.touched && secretName(n.value) && !s.checked) { s.checked = true; v.type = "password"; } n.dataset.touched = "1"; };
  });
  const add = (id, html) => { $(id, m.body).insertAdjacentHTML("beforeend", html); bindDel(); bindSecret(); };
  bindDel(); bindSecret();
  mountRepoConnectors($("#reConnectors", m.body), repo);
  $("#reAddVar", m.body).onclick = () => { add("#reVars", varRow()); $$("[data-vn]", m.body).pop().focus(); };
  $("#reAddFile", m.body).onclick = () => add("#reFiles", fileRow());
  $("#reAddCheck", m.body).onclick = () => add("#reChecks", checkRow());
  $("#rePaste", m.body).onclick = () => {
    const p = modal(`<h2>Paste a .env file</h2><p class="hint">Lines like <code>NAME=value</code>. Names that look secret are marked secret. Existing variables with the same name are replaced.</p>
      <div class="field"><textarea id="peText" rows="12" spellcheck="false" placeholder="DATABASE=...\nPASSWORD=..."></textarea></div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="peGo">Import</button></div>`, { wide: true });
    $("#peGo", p.body).onclick = async () => {
      try {
        const r = await api.repoEnvParse($("#peText", p.body).value);
        for (const v of r.vars) {
          const existing = $$(".renv-var", m.body).find((row) => $("[data-vn]", row).value.trim() === v.name);
          if (existing) existing.remove();
          add("#reVars", varRow(v));
        }
        toast("success", `Imported ${r.vars.length} variable(s)`, "Click Save to store them.");
        p.close();
      } catch (e) { toast("error", "Could not read that .env", e.message); }
    };
  };
  $("#reSave", m.body).onclick = async () => {
    const payload = {
      vars: $$(".renv-var", m.body).map((r) => ({ name: $("[data-vn]", r).value.trim(), value: $("[data-vv]", r).value, secret: $("[data-vs]", r).checked }))
        // A saved secret left untouched comes back as the mask, which keeps its stored value.
        .map((v) => { const old = env.vars.find((o) => o.name === v.name); return old && old.secret && old.has_value && v.value === "" ? { ...v, value: MASK } : v; })
        .filter((v) => v.name),
      files: $$(".renv-file", m.body).map((r) => ({ path: $("[data-fp]", r).value.trim(), content: $("[data-fc]", r).value, secret: $("[data-fs]", r).checked }))
        .map((f) => { const old = env.files.find((o) => o.path === f.path); return old && old.secret && old.has_value && f.content === "" ? { ...f, content: MASK } : f; })
        .filter((f) => f.path),
      write_dotenv: $("#reDotenv", m.body).checked,
      setup: $("#reSetup", m.body).value, services_up: $("#reUp", m.body).value, services_down: $("#reDown", m.body).value,
      checks: $$(".renv-check", m.body).map((r) => ({ command: $("[data-cc]", r).value.trim(), required: $("[data-cr]", r).checked })).filter((c) => c.command),
    };
    const btn = $("#reSave", m.body);
    btn.disabled = true;
    try {
      await api.saveRepoEnv(repo.path, payload);
      toast("success", `Environment saved for ${repo.name}`, "New tasks in this repository use it.");
      m.close();
      onSaved && onSaved();
    } catch (e) { toast("error", "Could not save", e.message); btn.disabled = false; }
  };
}
