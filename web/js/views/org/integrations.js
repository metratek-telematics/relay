// Integrations (admins): Relay's public address, team Slack/Discord, the Telegram bot, SMTP, Web Push, signed webhooks, delivery policy and log.
import { $, $$, esc, icon, toast, confirm, timeAgo } from "../../ui.js";
import { ORG, loadMe, orgApi, can, xicon, copyButton, bindCopy, readOnlyNote } from "./org.js";

const MASK = "••••••••";
const EVENTS = [["needs_input", "Needs input"], ["approval", "Approval"], ["delivered", "Delivered"], ["failed", "Failure"], ["digest", "Digest"], ["budget", "Budget"]];
const TONE = { delivered: "green", failed: "red", dropped: "red", suppressed: "", retrying: "amber", rate_limited: "amber", queued: "blue", sending: "blue" };

export function mountIntegrations(body) {
  let alive = true, data = null, deliveries = [], timer = null, dirty = false;

  async function load() {
    if (!ORG.me) await loadMe();
    if (!can("admin")) { body.innerHTML = `<div class="page-head"><div><h1>Integrations</h1></div></div>${readOnlyNote("admin", "Managing integrations")}`; return; }
    try { data = await orgApi.settings(); deliveries = (await orgApi.deliveries()).deliveries || []; }
    catch (e) { body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (alive) draw();
  }

  const sec = (v, has) => (has ? MASK : v || "");
  const state = (ok, on, off) => `<span class="org-status" style="color:var(--${ok ? "green" : "text-3"})"><span class="dot" style="background:currentColor"></span>${esc(ok ? on : off)}</span>`;

  function draw() {
    const i = data.integrations, tg = i.telegram, sm = i.smtp, wp = i.webpush;
    const base = i.public_url || location.origin;
    const hook = `${base.replace(/\/$/, "")}/api/org/integrations/telegram/webhook`;
    body.innerHTML = `
      <div class="page-head"><div><h1>Integrations</h1><p>Where Relay can reach people. Each person picks their own events and destinations under Profile → Notifications.</p></div>
        <div class="page-actions"><span class="muted" id="iState" style="font-size:12px"></span><button class="btn primary" id="iSave">${icon("save")}Save</button></div></div>
      <div class="card"><div class="card-head"><h3>Relay's address</h3></div><div class="card-body">
        <div class="field"><label>Public URL</label><input data-k="public_url" value="${esc(i.public_url || "")}" placeholder="${esc(location.origin)}"><div class="help">Used for links and buttons in messages. Empty: the address people last used to open Relay.</div></div></div></div>
      <div class="org-channels">
        <div class="card org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("slack")}</span><strong>Slack · team channel</strong>${state(i.slack.has_webhook_url, "connected", "not set")}</div>
          <div class="field"><label>Incoming webhook URL</label><input data-k="slack.webhook_url" value="${esc(sec(i.slack.webhook_url, i.slack.has_webhook_url))}" placeholder="https://hooks.slack.com/services/…" autocomplete="off"></div>
          <div class="help">People who choose Slack without their own webhook get messages here, with buttons back to Relay.</div>
          <div><button class="btn xs" data-test="slack">${icon("send")}Send test</button></div></div>
        <div class="card org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("discord")}</span><strong>Discord · team channel</strong>${state(i.discord.has_webhook_url, "connected", "not set")}</div>
          <div class="field"><label>Webhook URL</label><input data-k="discord.webhook_url" value="${esc(sec(i.discord.webhook_url, i.discord.has_webhook_url))}" placeholder="https://discord.com/api/webhooks/…" autocomplete="off"></div>
          <div><button class="btn xs" data-test="discord">${icon("send")}Send test</button></div></div>
        <div class="card org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("telegram")}</span><strong>Telegram bot</strong>${state(tg.has_bot_token, "connected", "not set")}</div>
          <div class="field"><label>Bot token (from @BotFather)</label><input data-k="telegram.bot_token" value="${esc(sec(tg.bot_token, tg.has_bot_token))}" placeholder="123456:ABC…" autocomplete="off"></div>
          <div class="field"><label>API base</label><input data-k="telegram.api_base" value="${esc(tg.api_base || "")}" placeholder="https://api.telegram.org"></div>
          <div class="help">Buttons (Approve, Request changes, answer options) need Telegram to reach Relay: point the bot's webhook at <code class="org-inline-code">${esc(hook)}</code> with the secret below, and let that path through your sign-in proxy. Relay checks the secret on every call.</div>
          ${tg.has_webhook_secret ? `<div class="row wrap" style="gap:6px">${copyButton(hook, "Copy webhook URL")}<span class="muted" style="font-size:12px">secret is set · setWebhook with secret_token</span></div>` : ""}
          <div class="row" style="gap:6px"><input class="input" id="tgChat" placeholder="chat id for the test" style="flex:1"><button class="btn xs" data-test="telegram">${icon("send")}Send test</button></div></div>
        <div class="card org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("mail")}</span><strong>Email (SMTP)</strong>${state(!!sm.host, "configured", "not set")}</div>
          <div class="grid2"><div class="field"><label>Host</label><input data-k="smtp.host" value="${esc(sm.host || "")}" placeholder="smtp.example.com"></div><div class="field"><label>Port</label><input data-k="smtp.port" type="number" value="${esc(sm.port || 587)}"></div></div>
          <div class="grid2"><div class="field"><label>Security</label><select data-k="smtp.security">${["starttls", "ssl", "none"].map((x) => `<option ${sm.security === x ? "selected" : ""}>${x}</option>`).join("")}</select></div><div class="field"><label>From</label><input data-k="smtp.from" value="${esc(sm.from || "")}" placeholder="Relay &lt;relay@example.com&gt;"></div></div>
          <div class="grid2"><div class="field"><label>Username</label><input data-k="smtp.username" value="${esc(sm.username || "")}" autocomplete="off"></div><div class="field"><label>Password</label><input data-k="smtp.password" type="password" value="${esc(sec(sm.password, sm.has_password))}" autocomplete="new-password"></div></div>
          <div class="row" style="gap:6px"><input class="input" id="emTo" placeholder="send a test to…" style="flex:1"><button class="btn xs" data-test="email">${icon("send")}Send test</button></div></div>
        <div class="card org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("monitor")}</span><strong>Web Push</strong>${state(!!wp.public_key, "keys ready", data.webpush_available ? "no keys" : "unavailable")}</div>
          <div class="help">Browser notifications even when Relay is closed (HTTPS only). Relay signs pushes with its own VAPID key; no third-party service is involved beyond each browser's push service.</div>
          ${data.webpush_available ? `<div class="row wrap" style="gap:6px"><button class="btn xs" id="wpKeys">${icon("key")}${wp.public_key ? "Keys generated" : "Generate keys"}</button>${wp.public_key ? `<code class="org-inline-code" title="Public key">${esc(wp.public_key.slice(0, 22))}…</code>` : ""}</div>` : `<div class="org-note warn">${icon("alert", "sm")}<span>The server lacks the cryptography package.</span></div>`}</div>
      </div>
      <div class="card"><div class="card-head"><div><h3>Webhooks</h3><p class="card-sub">Every matching event as JSON, signed with HMAC-SHA256: <code class="org-inline-code">X-Relay-Signature: sha256=HMAC(secret, X-Relay-Timestamp + "." + body)</code>. <a href="#/org/api">Verification code</a></p></div><button class="btn sm" id="whAdd">${icon("plus")}Add webhook</button></div>
        <div class="card-body stack" id="whList" style="gap:10px">${(i.webhooks || []).map(webhookRow).join("") || '<div class="empty small">No webhooks. Add one to feed Relay events into another system.</div>'}</div></div>
      <div class="card"><div class="card-head"><h3>Delivery policy</h3></div><div class="card-body grid3">
        <div class="field"><label>Messages per minute, per destination</label><input data-k="rate_limit_per_minute" type="number" min="1" max="600" value="${esc(i.rate_limit_per_minute)}"></div>
        <div class="field"><label>Attempts before giving up</label><input data-k="max_attempts" type="number" min="1" max="10" value="${esc(i.max_attempts)}"><div class="help">Backoff 2 s, 4 s, 8 s… (honours Retry-After).</div></div>
        <div class="field inline" style="align-self:center"><label>Allow private network targets<div class="help">Webhooks to 10.x, 192.168.x, localhost. Off protects internal services from misuse.</div></label><span class="switch ${i.allow_private_targets ? "on" : ""}" data-sw="allow_private_targets"></span></div>
      </div></div>
      <div class="card"><div class="card-head"><div><h3>Delivery log</h3><p class="card-sub">Everything sent in the last while, for everyone.</p></div><button class="btn sm" id="dlRefresh">${icon("refresh")}Refresh</button></div>
        <div class="card-body org-table-wrap" style="padding:4px 8px">${deliveries.length ? `<table class="org-table"><thead><tr><th>When</th><th>Event</th><th>Channel</th><th class="org-hide-sm">To</th><th>Person</th><th>Status</th><th class="num">Tries</th><th class="org-hide-sm">Last error</th></tr></thead><tbody>
          ${deliveries.slice(0, 80).map((d) => `<tr><td title="${esc(d.time)}">${esc(timeAgo(d.time))}</td><td>${esc(d.title || d.event)}</td><td>${esc(d.channel)}</td><td class="org-hide-sm truncate" style="max-width:220px">${esc(d.target)}</td><td>${esc(d.username || "org")}</td><td><span class="badge ${TONE[d.status] || ""}">${esc(String(d.status).replace("_", " "))}</span></td><td class="num">${d.attempts}</td><td class="org-hide-sm muted" style="max-width:260px;overflow-wrap:anywhere">${esc(d.error || "")}</td></tr>`).join("")}</tbody></table>`
          : '<div class="empty small">Nothing delivered yet.</div>'}</div></div>`;
    bind();
  }

  const webhookRow = (w) => `<div class="org-channel" data-wh="${esc(w.id || "")}">
    <div class="org-channel-head"><span class="org-ch-ic">${xicon("webhook")}</span><input class="input" data-w="name" value="${esc(w.name || "")}" placeholder="Name" style="flex:1"><span class="switch ${w.enabled !== false ? "on" : ""}" data-w-enabled title="Enabled"></span><button class="btn xs ghost" data-w-del title="Remove">${icon("trash")}</button></div>
    <div class="grid2"><div class="field"><label>URL</label><input data-w="url" value="${esc(w.url || "")}" placeholder="https://example.com/hooks/relay"></div><div class="field"><label>Secret</label><input data-w="secret" value="${esc(w.has_secret ? MASK : w.secret || "")}" placeholder="long random string" autocomplete="off"></div></div>
    <div class="field"><label>Events (none = all)</label><div class="org-chips">${EVENTS.map(([k, l]) => `<label class="org-chip"><input type="checkbox" data-w-ev="${k}" ${(w.events || []).includes(k) ? "checked" : ""}>${l}</label>`).join("")}</div></div>
    <div class="field"><label>Projects (none = all)</label><div class="org-chips">${ORG.projects.map((p) => `<label class="org-chip"><input type="checkbox" data-w-proj="${esc(p.id)}" ${(w.projects || []).includes(p.id) ? "checked" : ""}>${esc(p.name)}</label>`).join("")}</div></div>
    ${w.id ? `<div><button class="btn xs" data-w-test="${esc(w.id)}">${icon("send")}Send test</button></div>` : '<div class="help">Save to test it.</div>'}</div>`;

  function collect() {
    const out = { slack: {}, discord: {}, telegram: {}, smtp: {} };
    $$("[data-k]", body).forEach((el) => {
      const [a, b] = el.dataset.k.split(".");
      const v = el.type === "number" ? Number(el.value) : el.value.trim();
      if (b) out[a][b] = v; else out[a] = v;
    });
    $$("[data-sw]", body).forEach((s) => (out[s.dataset.sw] = s.classList.contains("on")));
    out.webhooks = $$("[data-wh]", body).map((row) => {
      const w = { enabled: $("[data-w-enabled]", row).classList.contains("on") };
      if (row.dataset.wh) w.id = row.dataset.wh;
      $$("[data-w]", row).forEach((el) => (w[el.dataset.w] = el.value.trim()));
      w.events = $$("[data-w-ev]", row).filter((x) => x.checked).map((x) => x.dataset.wEv);
      w.projects = $$("[data-w-proj]", row).filter((x) => x.checked).map((x) => x.dataset.wProj);
      return w;
    }).filter((w) => w.url || w.name);
    return out;
  }
  const save = async (quiet) => {
    try { data = { ...data, ...(await orgApi.saveIntegrations(collect())) }; dirty = false; if (!quiet) toast("success", "Integrations saved"); return true; }
    catch (e) { toast("error", "Could not save", e.message); return false; }
  };

  function bind() {
    const mark = () => { dirty = true; $("#iState", body).textContent = "Unsaved changes"; };
    $$("input,select", body).forEach((el) => el.addEventListener("change", mark));
    $$("[data-sw],[data-w-enabled]", body).forEach((s) => (s.onclick = () => { s.classList.toggle("on"); mark(); }));
    $("#iSave", body).onclick = async () => { if (await save()) { data = await orgApi.settings(); draw(); } };
    $("#dlRefresh", body).onclick = async () => { deliveries = (await orgApi.deliveries()).deliveries || []; draw(); };
    $("#whAdd", body).onclick = () => { const list = $("#whList", body); if (!list.querySelector("[data-wh]")) list.innerHTML = ""; list.insertAdjacentHTML("beforeend", webhookRow({ enabled: true })); bind(); };
    $$("[data-w-del]", body).forEach((b) => (b.onclick = async () => { if (await confirm("Remove this webhook?", "Save to apply.", { danger: true, okLabel: "Remove" })) { b.closest("[data-wh]").remove(); mark(); } }));
    $("#wpKeys", body) && ($("#wpKeys", body).onclick = async () => { try { await orgApi.vapidKeys(); data = await orgApi.settings(); toast("success", "Web Push keys ready", "People can now enable push under Notifications."); draw(); } catch (e) { toast("error", "Could not create keys", e.message); } });
    const test = async (btn, payload) => {
      if (dirty && !(await save(true))) return;
      btn.disabled = true; btn.innerHTML = `${icon("spinner")}Sending`;
      try {
        const r = await orgApi.testIntegration(payload);
        toast(r.status === "delivered" ? "success" : "error", r.status === "delivered" ? "Test delivered" : `Test ${r.status}`, r.error || `${r.attempts} attempt(s)`);
      } catch (e) { toast("error", "Test failed", e.message); }
      deliveries = (await orgApi.deliveries()).deliveries || []; data = await orgApi.settings(); draw();
    };
    $$("[data-test]", body).forEach((b) => (b.onclick = () => test(b, { channel: b.dataset.test, chat_id: $("#tgChat", body)?.value.trim(), address: $("#emTo", body)?.value.trim() })));
    $$("[data-w-test]", body).forEach((b) => (b.onclick = () => test(b, { channel: "webhook", id: b.dataset.wTest })));
    bindCopy(body);
  }

  load();
  timer = setInterval(async () => { if (alive && !dirty && document.visibilityState === "visible" && data) { try { deliveries = (await orgApi.deliveries()).deliveries || []; } catch {} } }, 20000);
  return { destroy() { alive = false; clearInterval(timer); } };
}
