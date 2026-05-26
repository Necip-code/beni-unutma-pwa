const CACHE = "beni-unutma-v1";
const ASSETS = ["/", "/static/index.html"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(clients.claim());
});

self.addEventListener("fetch", e => {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

// Push notification received
self.addEventListener("push", e => {
  if (!e.data) return;
  const data = e.data.json();

  let title, body, actions, tag;

  if (data.type === "alarm") {
    title = "🔔 " + data.title;
    body = "Alarm zamanı geldi! Haydi başla.";
    tag = "alarm-" + data.id;
    actions = [
      { action: "ok-" + data.id, title: "✅ Tamam, yapıyorum!" }
    ];
  } else if (data.type === "check") {
    title = "🤔 " + data.title;
    body = "Görevi yaptın mı?";
    tag = "check-" + data.id;
    actions = [
      { action: "done-" + data.id, title: "✅ Evet, yaptım!" },
      { action: "snooze-" + data.id, title: "⏰ Henüz yapmadım" }
    ];
  } else {
    return;
  }

  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      actions,
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      vibrate: [200, 100, 200, 100, 400],
      requireInteraction: true,
      data
    })
  );
});

// Notification click
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const action = e.action;
  const data = e.notification.data;

  if (!action || action.startsWith("ok-")) {
    // Open app
    e.waitUntil(clients.openWindow("/"));
    return;
  }

  if (action.startsWith("done-")) {
    const id = parseInt(action.replace("done-", ""));
    e.waitUntil(
      fetch("/api/tasks/" + id + "/done", { method: "POST" })
        .then(() => clients.openWindow("/"))
    );
  } else if (action.startsWith("snooze-")) {
    // Backend handles 15-min repeat automatically, just close
    e.waitUntil(clients.openWindow("/"));
  }
});
