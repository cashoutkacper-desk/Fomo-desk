/* STEP 8 PUSH · service worker. Shows the desk's web push (plan A only) and opens
   the push's FOMO link (links.coin_url, sent by webpush.py) when tapped. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(d.title || "Grok Bot · now", {
    body: d.body || "", icon: d.icon || "icons/apple-touch-icon.png", data: { url: d.url || null }, tag: d.tag || undefined,
  }));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url;
  if (url && url.startsWith("https://") && url.includes("r=savipww")) event.waitUntil(self.clients.openWindow(url));
});
