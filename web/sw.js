// Relay service worker: Web Push without a payload. The push only wakes this worker; it asks Relay (with the
// person's own session) for the newest notification and shows it. Clicking opens the task or the inbox.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  event.waitUntil((async () => {
    let n = null;
    try {
      const r = await fetch("/api/org/me/notifications/latest", { credentials: "include", cache: "no-store" });
      if (r.ok) n = ((await r.json()).notifications || [])[0] || null;
    } catch {}
    const title = n ? `Relay · ${n.title}` : "Relay";
    const body = n ? (n.body || "") : "Something needs your attention.";
    const url = n && n.task_id ? `/#/task/${n.task_id}` : n && n.kind === "digest" ? "/#/digest" : "/#/inbox";
    await self.registration.showNotification(title, { body, tag: n ? n.id : "relay", data: { url }, icon: "/favicon.ico" });
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) { if ("focus" in w) { await w.focus(); w.navigate(url); return; } }
    await self.clients.openWindow(url);
  })());
});
