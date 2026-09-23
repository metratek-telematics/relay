// Report an issue: files a bug report or feature request on the Relay repository
// without leaving the workspace, screenshots included.
import { api } from "../api.js";
import { S, agentLabel } from "../state.js";
import { $, $$, esc, icon, modal, toast } from "../ui.js";

const MAX_FILES = 5;
const MAX_BYTES = 5 * 1024 * 1024;

// "Claude 2.1.278, Codex codex-cli 0.155.1" — the CLI versions a maintainer needs to reproduce.
// Only agents this machine actually has: the ones it does not are noise in an issue.
function agentSummary() {
  return Object.entries(S.agents || {})
    .filter(([name, h]) => name !== "git" && name !== "gh" && h?.installed && h.version)
    .map(([name, h]) => `${agentLabel(name)} ${h.version}`)
    .join(", ") || "None detected";
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve({ name: file.name || "pasted-image.png", type: file.type, size: file.size, dataUrl: r.result });
    r.onerror = () => reject(new Error(`Could not read ${file.name || "that image"}.`));
    r.readAsDataURL(file);
  });
}

async function copyImage(file) {
  try {
    const blob = await (await fetch(file.dataUrl)).blob();
    if (!navigator.clipboard?.write || !window.ClipboardItem) throw new Error("This browser cannot copy images to the clipboard.");
    await navigator.clipboard.write([new ClipboardItem({ [blob.type || file.type]: blob })]);
    toast("success", "Image copied", "Paste it into the GitHub issue.");
  } catch (e) {
    toast("error", "Could not copy the image", e.message || "Clipboard access was denied.");
  }
}

export function openReportIssue() {
  const files = [];
  const m = modal(`<div class="report-dialog">
    <div class="report-head">
      <div><div class="eyebrow">GitHub</div><h2>Report an issue</h2><p class="hint">File a bug report or a feature request on the Relay repository.</p></div>
      <button class="btn icon ghost" type="button" data-close aria-label="Close">${icon("x")}</button>
    </div>
    <div class="report-kind" role="group" aria-label="Issue type">
      <button class="report-kind-btn active" type="button" data-kind="bug"><span class="report-kind-title">Bug</span><span>Something does not work as expected</span></button>
      <button class="report-kind-btn" type="button" data-kind="feature"><span class="report-kind-title">Feature</span><span>Suggest an improvement</span></button>
    </div>
    <form id="reportForm" novalidate>
      <div class="field"><label for="reportTitle">Title <span class="req">Required</span></label><input id="reportTitle" maxlength="180" placeholder="A short summary" autocomplete="off"></div>
      <div class="field"><label for="reportBody" id="reportBodyLabel">What happened <span class="req">Required</span></label><textarea id="reportBody" rows="5" placeholder="What you did, what you expected, and what happened instead."></textarea></div>
      <div class="field" id="reportIdeaField" hidden><label for="reportIdea">What you would like <span class="opt">Optional</span></label><textarea id="reportIdea" rows="3" placeholder="The behaviour you have in mind."></textarea></div>
      <div class="report-section">
        <div class="report-section-head"><div><h3>Environment</h3><p>Read from this session. Edit anything that helps reproduce it.</p></div>${icon("info", "sm")}</div>
        <div class="grid2">
          <div class="field"><label for="reportVersion">Relay version</label><input id="reportVersion" value="${esc(S.build ? `v${S.build}` : "")}"></div>
          <div class="field"><label for="reportAgents">Agent CLI versions</label><input id="reportAgents" value="${esc(agentSummary())}"></div>
        </div>
      </div>
      <div class="field" id="reportLogField"><label for="reportLog">Relevant log <span class="opt">Optional</span></label><textarea id="reportLog" rows="3" placeholder="The matching part of the run log. This repository is public — remove anything you cannot share."></textarea></div>
      <div class="field">
        <label>Images <span class="opt">Optional</span></label>
        <div class="report-drop" id="reportDrop" tabindex="0" role="button" aria-label="Add screenshots">
          <input id="reportFiles" type="file" accept="image/*" multiple hidden>
          <div class="report-drop-copy">${icon("image", "lg")}<strong>Paste, drop or browse for screenshots</strong><span>Images only · up to 5 files and 5 MB in total</span></div>
        </div>
        <div id="reportPreviews" class="report-previews" aria-live="polite"></div>
        <div id="reportAttachError" class="report-inline-error" role="alert" hidden></div>
      </div>
      <div id="reportError" class="modal-error" role="alert" hidden></div>
      <div class="modal-actions"><span class="report-footnote">Filed on the Relay GitHub repository.</span><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" id="reportSubmit" type="submit">${icon("send")}Submit report</button></div>
    </form>
  </div>`, { wide: true });

  const root = m.body;
  const form = $("#reportForm", root);
  const titleInput = $("#reportTitle", root);
  const bodyLabel = $("#reportBodyLabel", root);
  const bodyInput = $("#reportBody", root);
  const ideaField = $("#reportIdeaField", root);
  const ideaInput = $("#reportIdea", root);
  const logField = $("#reportLogField", root);
  const drop = $("#reportDrop", root);
  const fileInput = $("#reportFiles", root);
  const previews = $("#reportPreviews", root);
  const attachError = $("#reportAttachError", root);
  const errorBox = $("#reportError", root);
  const submit = $("#reportSubmit", root);
  let kind = "bug";

  const showError = (msg) => { errorBox.textContent = msg || ""; errorBox.hidden = !msg; };
  const showAttachError = (msg) => { attachError.textContent = msg || ""; attachError.hidden = !msg; };
  const renderPreviews = () => {
    previews.innerHTML = files.map((f, i) => `<div class="report-preview"><img src="${esc(f.dataUrl)}" alt=""><span title="${esc(f.name)}">${esc(f.name)}</span><button class="btn icon xs" type="button" data-remove="${i}" aria-label="Remove ${esc(f.name)}">${icon("x", "sm")}</button></div>`).join("");
    $$("[data-remove]", previews).forEach((b) => (b.onclick = () => { files.splice(Number(b.dataset.remove), 1); renderPreviews(); }));
  };
  const addFiles = async (incoming) => {
    showAttachError("");
    for (const f of [...incoming]) {
      if (files.length >= MAX_FILES) { showAttachError("You can add up to 5 images."); break; }
      if (!f.type?.startsWith("image/")) { showAttachError(`${f.name || "That file"} is not an image.`); continue; }
      if (files.reduce((n, x) => n + x.size, 0) + f.size > MAX_BYTES) { showAttachError("Images must fit within the 5 MB total size limit."); continue; }
      try { files.push(await readFile(f)); } catch (e) { showAttachError(e.message); }
    }
    renderPreviews();
  };
  const setKind = (next) => {
    kind = next;
    $$("[data-kind]", root).forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
    bodyLabel.firstChild.textContent = kind === "bug" ? "What happened " : "The problem ";
    bodyInput.placeholder = kind === "bug" ? "What you did, what you expected, and what happened instead." : "What are you trying to do, and what gets in the way today?";
    ideaField.hidden = kind !== "feature";
    logField.hidden = kind !== "bug";
  };
  $$("[data-kind]", root).forEach((b) => (b.onclick = () => setKind(b.dataset.kind)));
  drop.onclick = () => fileInput.click();
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } };
  fileInput.onchange = () => { addFiles(fileInput.files); fileInput.value = ""; };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("dragging"); };
  drop.ondragleave = () => drop.classList.remove("dragging");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("dragging"); addFiles(e.dataTransfer.files); };
  root.addEventListener("paste", (e) => {
    const pasted = [...(e.clipboardData?.items || [])].filter((i) => i.kind === "file").map((i) => i.getAsFile()).filter(Boolean);
    if (pasted.length) { e.preventDefault(); addFiles(pasted); }
  });

  form.onsubmit = async (e) => {
    e.preventDefault();
    showError("");
    if (!titleInput.value.trim() || !bodyInput.value.trim()) {
      showError("Add a title and a description before submitting.");
      (titleInput.value.trim() ? bodyInput : titleInput).focus();
      return;
    }
    submit.disabled = true;
    submit.innerHTML = `${icon("spinner", "spin")}Filing…`;
    try {
      const r = await api.reportIssue({
        kind, title: titleInput.value.trim(), body: bodyInput.value.trim(), idea: ideaInput.value.trim(),
        version: $("#reportVersion", root).value.trim(), agents: $("#reportAgents", root).value.trim(), log: $("#reportLog", root).value.trim(),
        attachments: files.map(({ name, dataUrl }) => ({ name, data_url: dataUrl })),
      });
      root.innerHTML = `<div class="report-success">
        <div class="report-success-mark">${icon("check", "lg")}</div>
        <div class="eyebrow">Filed on GitHub</div><h2>Thanks for the report</h2>
        <p class="hint">${files.length ? "GitHub only accepts images pasted into the issue itself, so copy each screenshot below and paste it into the issue after opening it." : "The issue is with the Relay maintainers."}</p>
        <a class="report-issue-link" href="${esc(r.url)}" target="_blank" rel="noopener">Open issue${r.number ? ` #${esc(r.number)}` : ""}${icon("external", "sm")}</a>
        ${files.length ? `<div class="report-success-files">${files.map((f, i) => `<div class="report-success-file"><img src="${esc(f.dataUrl)}" alt=""><span>${esc(f.name)}</span><button class="btn sm" type="button" data-copy-image="${i}">${icon("copy")}Copy image</button></div>`).join("")}</div>` : ""}
        <div class="modal-actions"><button class="btn" type="button" data-close>Done</button><a class="btn primary" href="${esc(r.url)}" target="_blank" rel="noopener">Open issue${icon("external")}</a></div>
      </div>`;
      $$("[data-close]", root).forEach((b) => (b.onclick = m.close));
      $$("[data-copy-image]", root).forEach((b) => (b.onclick = () => copyImage(files[Number(b.dataset.copyImage)])));
    } catch (err) {
      // The typed report and its images stay on screen: only a filed issue replaces the form.
      showError(err.message || "Could not file the issue.");
      submit.disabled = false;
      submit.innerHTML = `${icon("send")}Submit report`;
    }
  };
  return m;
}
