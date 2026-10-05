self.addEventListener('install', function(e) {
    self.skipWaiting();
});

self.addEventListener('activate', function(e) {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('push', function(e) {
    var data = {};
    try { data = e.data ? e.data.json() : {}; } catch (err) {}
    var title = data.title || 'VyahMandap';
    var body = data.body || 'New update';
    var url = data.url || '/';
    var options = {
        body: body,
        icon: '/static/images/logo.png',
        badge: '/static/images/logo.png',
        vibrate: [200, 100, 200],
        tag: 'vm-push',
        renotify: true,
        data: { url: url }
    };
    e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(e) {
    e.notification.close();
    var url = (e.notification.data && e.notification.data.url) || '/';
    e.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
            for (var i = 0; i < list.length; i++) {
                var c = list[i];
                if (c.url.indexOf(self.location.origin) === 0 && 'focus' in c) {
                    c.navigate(url);
                    return c.focus();
                }
            }
            if (clients.openWindow) return clients.openWindow(url);
        })
    );
});
