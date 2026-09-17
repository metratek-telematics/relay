// You: profile and notification preferences (per event, per channel), with test-send and the delivery log.
import { $, $$, esc, icon, toast, timeAgo, fmtDateTime } from "../../ui.js";
import { bus } from "../../state.js";
import { ORG, loadMe, orgApi, avatar, roleBadge, xicon, can, copyButton, bindCopy } from "./org.js";

const MASK = "••••••••";
const SOURCE = { manual: "assigned by an owner", groups: "from your groups", bootstrap: "first person to sign in", env: "set by the server", default: "default role", local: "local mode" };

export function mountProfile(body) {
  let alive = true;
  async function draw() {
    const me = ORG.me || (await loadMe());
    if (!alive || !me) return;
    const u = me.user, p = u.prefs || {};
    body.innerHTML = `
      <div class="page-head"><div><h1>Profile</h1><p>How you appear to others in Relay, and how Relay looks for you.</p></div></div>
      <div class="card"><div class="card-body"><div class="org-profile">${avatar(u, 72)}
        <div class="min0"><h2>${esc(u.name)}</h2><div class="muted">${esc(u.email || u.username)}</div>
          <div class="row wrap" style="gap:6px;margin-top:8px">${roleBadge(me.role)}<span class="muted" style="font-size:12px">${esc(SOURCE[u.role_source] || "")}</span>${(u.groups || []).map((g) => `<span class="org-chip">${esc(g)}</span>`).join("")}</div></div></div>
        <dl class="org-kv" style="margin:16px 0 0">
          <div><dt>Username</dt><dd>${esc(u.username)}</dd></div>
          <div><dt>Signed in</dt><dd>${esc(me.via === "proxy" ? "Identity provider (SSO)" : me.via === "local" ? "Local mode" : me.via || "—")}</dd></div>
          <div><dt>First seen</dt><dd>${u.first_seen ? esc(fmtDateTime(u.first_seen)) : "—"}</dd></div>
          <div><dt>Last seen</dt><dd>${u.last_seen ? esc(timeAgo(u.last_seen)) : "—"}</dd></div>
        </dl>
        ${me.via === "proxy" ? `<p class="hint" style="margin:12px 0 0">Name, email and groups come from your identity provider and refresh each time you sign in.</p>` : ""}
      </div></div>
      <div class="card"><div class="card-head"><h3>Avatar</h3></div><div class="card-body">
        <div class="field inline"><label>Use Gravatar for ${esc(u.email || "your email")}<div class="help">Sends a hash of your email address to gravatar.com when pages load. Off: your initials.</div></label><span class="switch ${p.gravatar ? "on" : ""}" id="pGrav" role="switch" aria-checked="${!!p.gravatar}" tabindex="0"></span></div>
        <div class="field"><label>Or an image address</label><input id="pAvatar" placeholder="https://…" value="${esc(u.avatar_url && !u.avatar_url.includes("gravatar.com") ? u.avatar_url : "")}"></div>
      </div></div>
      <div class="card"><div class="card-head"><h3>Appearance</h3></div><div class="card-body grid2">
        <div class="field"><label>Theme</label><select id="pTheme">${["system", "light", "dark"].map((x) => `<option ${p.theme === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
        <div class="field"><label>Density</label><select id="pDensity">${["comfortable", "compact"].map((x) => `<option ${p.density === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
      </div></div>`;
    const save = async (b, msg = "Saved") => { try { ORG.me = await orgApi.saveMe(b); bus.emit("org"); toast("success", msg); draw(); } catch (e) { toast("error", "Could not save", e.message); } };
    $("#pGrav", body).onclick = () => save({ prefs: { gravatar: !p.gravatar } });
    $("#pAvatar", body).onchange = (e) => save({ avatar_url: e.target.value.trim() });
    const theme = () => { const t = $("#pTheme", body).value, d = $("#pDensity", body).value; bus.emit("theme", { theme: t, density: d }); save({ prefs: { theme: t, density: d } }); };
    $("#pTheme", body).onchange = theme;
    $("#pDensity", body).onchange = theme;
  }
  draw();
  return { destroy() { alive = false; } };
}

const CH = [
  ["inapp", "In Relay", "bell"], ["browser", "Browser", "monitor"], ["email", "Email", "mail"], ["slack", "Slack", "slack"],
  ["telegram", "Telegram", "telegram"], ["discord", "Discord", "discord"], ["webhook", "Webhook", "webhook"],
];
const EVENT_HELP = {
  needs_input: "An agent asks a question or the judge needs a decision.", approval: "A delivery waits for approval.",
  delivered: "A task delivered its branch or opened a pull request.", failed: "A task failed or ran out of budget.",
  digest: "Your daily digest, at your digest time.", budget: "A budget crossed an alert threshold (admins).",
};
const STATUS_TONE = { delivered: "green", failed: "red", suppressed: "", dropped: "red", retrying: "amber", rate_limited: "amber", queued: "blue", sending: "blue" };

export function mountNotifications(body) {
  let alive = true, deliveries = [], draft = null, timer = null;

  async function load() {
    const me = await loadMe();
    try { deliveries = (await orgApi.myDeliveries()).deliveries || []; } catch { deliveries = []; }
    if (!alive || !me) return;
    draft = JSON.parse(JSON.stringify(me.user.prefs.notifications));
    draw();
  }

  function channelReady(ch) {
    const me = ORG.me, av = me.channels_available || {}, c = draft.channels;
    if (ch === "inapp") return [true, ""];
    if (ch === "browser") return [true, ""];
    if (ch === "email") return av.email ? [!!(c.email.address || me.user.email), "Add an address below"] : [false, "An admin has not set up email (SMTP) yet"];
    if (ch === "slack") return [!!(c.slack.webhook_url || av.team_slack), "Add a Slack webhook below"];
    if (ch === "discord") return [!!(c.discord.webhook_url || av.team_discord), "Add a Discord webhook below"];
    if (ch === "telegram") return av.telegram ? [!!c.telegram.chat_id, "Add your chat id below"] : [false, "An admin has not added a Telegram bot yet"];
    if (ch === "webhook") return [!!c.webhook.url, "Add a webhook address below"];
    return [true, ""];
  }

  function draw() {
    const me = ORG.me, av = me.channels_available || {}, c = draft.channels;
    const perm = "Notification" in window ? Notification.permission : "unsupported";
    const events = me.events || [];
    const matrix = `<table class="org-matrix"><thead><tr><th>Event</th>${CH.map(([k, l, i]) => `<th title="${esc(l)}">${xicon(i, "sm")}${esc(l)}</th>`).join("")}</tr></thead><tbody>
      ${events.filter((e) => e.id !== "budget" || can("admin")).map((e) => `<tr><td><strong>${esc(e.label)}</strong><span>${esc(EVENT_HELP[e.id] || "")}</span></td>
        ${CH.map(([k]) => { const [ready, why] = channelReady(k); const on = (draft.events[e.id] || []).includes(k);
          return `<td><input type="checkbox" class="org-tick" data-ev="${esc(e.id)}" data-ch="${esc(k)}" ${on ? "checked" : ""} ${k === "inapp" ? "checked disabled title=\"Always on\"" : ""} ${!ready && !on ? `disabled title="${esc(why)}"` : ""} aria-label="${esc(e.label)} by ${esc(k)}"></td>`; }).join("")}</tr>`).join("")}
      </tbody></table>`;
    const status = (ok, text) => `<span class="org-status" style="color:var(--${ok ? "green" : "text-3"})"><span class="dot" style="background:currentColor"></span>${esc(text)}</span>`;
    const testBtn = (ch) => `<button class="btn xs" data-test="${ch}">${icon("send")}Test</button>`;
    const secretField = (label, ch, key, placeholder, hasKey) => `<div class="field"><label>${esc(label)}</label><input data-chan="${ch}.${key}" placeholder="${esc(placeholder)}" value="${esc(c[ch][hasKey] ? MASK : c[ch][key] || "")}" autocomplete="off"></div>`;
    body.innerHTML = `
      <div class="page-head"><div><h1>Notifications</h1><p>Choose what reaches you and where. Relay retries failed deliveries with backoff and rate-limits each destination.</p></div>
        <div class="page-actions"><span class="muted" id="nState" style="font-size:12px"></span><button class="btn primary" id="nSave">${icon("save")}Save</button></div></div>
      <div class="card"><div class="card-head"><div><h3>What reaches you</h3><p class="card-sub">In Relay notifications always show. Viewers are not asked to answer questions or approve deliveries.</p></div></div><div class="card-body org-table-wrap">${matrix}</div></div>
      <div class="card"><div class="card-head"><h3>Destinations</h3></div><div class="card-body"><div class="org-channels">
        <div class="org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("monitor")}</span><strong>Browser</strong>${status(perm === "granted", perm === "granted" ? "allowed" : perm)}</div>
          <div class="help">Desktop alerts while Relay is open in a tab.${me.webpush?.available ? " Push also reaches this browser when Relay is closed." : " An admin can enable Web Push for alerts while Relay is closed."}</div>
          <div class="row wrap" style="gap:6px"><button class="btn sm" id="nPerm">${xicon("monitor")}${perm === "granted" ? "Allowed" : "Allow in this browser"}</button>
          ${me.webpush?.available ? `<button class="btn sm" id="nPush">${icon("bell")}${(me.webpush.subscriptions || []).length ? "Re-subscribe this device" : "Enable push on this device"}</button>` : ""}${testBtn("browser")}</div>
          ${(me.webpush?.subscriptions || []).length ? `<div class="help">${me.webpush.subscriptions.length} device(s) subscribed.</div>` : ""}</div>
        <div class="org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("mail")}</span><strong>Email</strong>${status(av.email, av.email ? "SMTP ready" : "not set up")}</div>
          <div class="field"><label>Address</label><input data-chan="email.address" placeholder="${esc(me.user.email || "you@company.com")}" value="${esc(c.email.address || "")}"></div>
          <div class="help">Empty uses your account email${me.user.email ? ` (${esc(me.user.email)})` : ""}.</div><div>${testBtn("email")}</div></div>
        <div class="org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("slack")}</span><strong>Slack</strong>${status(c.slack.has_webhook_url || av.team_slack, c.slack.has_webhook_url ? "personal webhook" : av.team_slack ? "team channel" : "not set")}</div>
          ${secretField("Incoming webhook (optional)", "slack", "webhook_url", "https://hooks.slack.com/services/…", "has_webhook_url")}
          <div class="help">Messages link back to the item in Relay${av.team_slack ? "; without your own webhook they go to the team channel" : ""}.</div><div>${testBtn("slack")}</div></div>
        <div class="org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("telegram")}</span><strong>Telegram</strong>${status(av.telegram && c.telegram.chat_id, !av.telegram ? "no bot yet" : me.user.telegram_linked ? "linked" : c.telegram.chat_id ? "chat set" : "not set")}</div>
          <div class="field"><label>Chat id</label><input data-chan="telegram.chat_id" placeholder="123456789" value="${esc(c.telegram.chat_id || "")}" ${av.telegram ? "" : "disabled"}></div>
          <div class="help">${av.telegram ? (me.user.telegram_linked ? `Linked. Reply to Relay's messages in Telegram to answer or steer tasks, or ask the assistant; send <b>/help</b> to the bot for commands.` : `Open the bot in Telegram, send <b>/start</b>, then send this line (it links your account, fills in the chat and turns on Telegram for questions, approvals, deliveries and failures):`) : "Ask an admin to add the team bot under Integrations."}</div>
          ${av.telegram && !me.user.telegram_linked && me.telegram_link_code ? `<div class="row" style="gap:6px"><code class="org-inline-code" style="flex:1">/link ${esc(me.telegram_link_code)}</code>${copyButton(`/link ${me.telegram_link_code}`, "Copy")}</div>` : ""}<div>${testBtn("telegram")}</div></div>
        <div class="org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("discord")}</span><strong>Discord</strong>${status(c.discord.has_webhook_url || av.team_discord, c.discord.has_webhook_url ? "personal webhook" : av.team_discord ? "team channel" : "not set")}</div>
          ${secretField("Webhook (optional)", "discord", "webhook_url", "https://discord.com/api/webhooks/…", "has_webhook_url")}<div>${testBtn("discord")}</div></div>
        <div class="org-channel"><div class="org-channel-head"><span class="org-ch-ic">${xicon("webhook")}</span><strong>Webhook</strong>${status(!!c.webhook.url, c.webhook.url ? "set" : "not set")}</div>
          <div class="field"><label>Address</label><input data-chan="webhook.url" placeholder="https://example.com/relay-events" value="${esc(c.webhook.url || "")}"></div>
          <div class="field"><label>Signing secret</label><input data-chan="webhook.secret" placeholder="a long random string" value="${esc(c.webhook.has_secret ? MASK : c.webhook.secret || "")}" autocomplete="off"></div>
          <div class="help">JSON body, signed: <code class="org-inline-code">X-Relay-Signature: sha256=HMAC(secret, timestamp + "." + body)</code>. <a href="#/org/api">Verification examples</a>.</div><div>${testBtn("webhook")}</div></div>
      </div></div></div>
      <div class="grid2" style="gap:18px">
        <div class="card"><div class="card-head"><h3>Quiet hours</h3><span class="switch ${draft.quiet_hours.enabled ? "on" : ""}" id="nQuiet" role="switch" aria-checked="${!!draft.quiet_hours.enabled}" tabindex="0"></span></div><div class="card-body">
          <div class="grid2"><div class="field"><label>From</label><input type="time" id="nQs" value="${esc(draft.quiet_hours.start)}"></div><div class="field"><label>Until</label><input type="time" id="nQe" value="${esc(draft.quiet_hours.end)}"></div></div>
          <div class="help">Outside Relay nothing reaches you in this window (server time); it still shows in Relay and the digest.</div></div></div>
        <div class="card"><div class="card-head"><h3>Digest &amp; projects</h3></div><div class="card-body">
          <div class="field"><label>Digest time</label><input type="time" id="nDigest" value="${esc(draft.digest_time)}"></div>
          <div class="field"><label>Only these projects</label><div class="org-chips">${ORG.projects.map((p) => `<label class="org-chip"><input type="checkbox" data-proj-filter="${esc(p.id)}" ${(draft.projects || []).includes(p.id) ? "checked" : ""}> ${esc(p.name)}</label>`).join("")}</div>
            <div class="help">None ticked: every project.</div></div></div></div>
      </div>
      <div class="card"><div class="card-head"><div><h3>Recent deliveries</h3><p class="card-sub">What Relay sent you, with attempts and the last error.</p></div><button class="btn sm" id="nRefresh">${icon("refresh")}Refresh</button></div>
        <div class="card-body org-table-wrap">${deliveries.length ? `<table class="org-table"><thead><tr><th>When</th><th>Event</th><th>Channel</th><th>Status</th><th class="num">Attempts</th><th class="org-hide-sm">Detail</th></tr></thead><tbody>
          ${deliveries.slice(0, 40).map((d) => `<tr><td title="${esc(d.time)}">${esc(timeAgo(d.time))}</td><td>${esc(d.title || d.event)}</td><td>${esc(d.channel)}</td><td><span class="badge ${STATUS_TONE[d.status] || ""}">${esc(d.status.replace("_", " "))}</span></td><td class="num">${d.attempts}</td><td class="org-hide-sm muted">${esc(d.error || "")}</td></tr>`).join("")}</tbody></table>`
          : `<div class="empty small">Nothing sent yet. Use Test on a destination above.</div>`}</div></div>`;
    bind();
  }

  const collect = () => {
    $$("[data-ev]", body).forEach((cb) => {
      const list = new Set(draft.events[cb.dataset.ev] || []);
      if (cb.dataset.ch === "inapp") list.add("inapp");
      else cb.checked ? list.add(cb.dataset.ch) : list.delete(cb.dataset.ch);
      draft.events[cb.dataset.ev] = [...list];
    });
    $$("[data-chan]", body).forEach((i) => { const [ch, k] = i.dataset.chan.split("."); draft.channels[ch][k] = i.value.trim(); });
    draft.quiet_hours.start = $("#nQs", body).value || "22:00";
    draft.quiet_hours.end = $("#nQe", body).value || "07:00";
    draft.digest_time = $("#nDigest", body).value || "08:00";
    draft.projects = $$("[data-proj-filter]", body).filter((x) => x.checked).map((x) => x.dataset.projFilter);
  };
  const save = async (quiet = false) => {
    collect();
    try {
      ORG.me = await orgApi.saveMe({ prefs: { notifications: draft } });
      draft = JSON.parse(JSON.stringify(ORG.me.user.prefs.notifications));
      if (!quiet) toast("success", "Notification preferences saved");
      return true;
    } catch (e) { toast("error", "Could not save", e.message); return false; }
  };

  function bind() {
    bindCopy(body);
    $("#nSave", body).onclick = async () => { if (await save()) draw(); };
    $("#nRefresh", body).onclick = load;
    $("#nQuiet", body).onclick = () => { collect(); draft.quiet_hours.enabled = !draft.quiet_hours.enabled; draw(); };
    $$("[data-chan]", body).forEach((i) => i.addEventListener("change", () => { collect(); $("#nState", body).textContent = "Unsaved changes"; }));
    $$("[data-ev],[data-proj-filter]", body).forEach((i) => i.addEventListener("change", () => { $("#nState", body).textContent = "Unsaved changes"; }));
    $("#nPerm", body).onclick = async () => { if ("Notification" in window) { await Notification.requestPermission(); draw(); } };
    $("#nPush", body) && ($("#nPush", body).onclick = subscribePush);
    $$("[data-test]", body).forEach((b) => (b.onclick = async () => {
      if (!(await save(true))) return;
      if (b.dataset.test === "browser" && !ORG.me.webpush?.subscriptions?.length) {
        if ("Notification" in window && Notification.permission === "granted") { new Notification("Relay · Test notification", { body: "Browser alerts work in this tab." }); return; }
        return toast("info", "Allow browser notifications first");
      }
      b.disabled = true; b.innerHTML = `${icon("spinner")}Sending`;
      try {
        const r = await orgApi.testChannel(b.dataset.test);
        if (r.status === "delivered") toast("success", "Test delivered", `${r.channel} · ${r.attempts} attempt${r.attempts === 1 ? "" : "s"}`);
        else toast(r.status === "failed" ? "error" : "warning", `Test ${String(r.status || "pending").replace("_", " ")}`, r.error || "Still retrying; see Recent deliveries.");
      } catch (e) { toast("error", "Test failed", e.message); }
      load();
    }));
  }

  async function subscribePush() {
    try {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) throw new Error("This browser does not support Web Push (it needs HTTPS).");
      if (Notification.permission !== "granted" && (await Notification.requestPermission()) !== "granted") throw new Error("Notifications are blocked for this site.");
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const key = ORG.me.webpush.public_key;
      const raw = Uint8Array.from(atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4)), (ch) => ch.charCodeAt(0));
      const old = await reg.pushManager.getSubscription();
      if (old) await old.unsubscribe();
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: raw });
      await orgApi.pushSubscribe(sub.toJSON());
      toast("success", "Push enabled on this device");
      await loadMe(); draft = JSON.parse(JSON.stringify(ORG.me.user.prefs.notifications)); draw();
    } catch (e) { toast("error", "Could not enable push", e.message); }
  }

  load();
  timer = setInterval(() => { if (alive && document.visibilityState === "visible") orgApi.myDeliveries().then((r) => { deliveries = r.deliveries || []; }).catch(() => {}); }, 15000);
  return { destroy() { alive = false; clearInterval(timer); } };
}
