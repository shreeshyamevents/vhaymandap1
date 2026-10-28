self.addEventListener('install', function(e) {
    self.skipWaiting();
});

self.addEventListener('activate', function(e) {
    e.waitUntil(
        self.clients.claim().then(function() {
            // Web push was removed: drop any push subscription left from earlier versions
            if (self.registration.pushManager) {
                return self.registration.pushManager.getSubscription().then(function(sub) {
                    return sub ? sub.unsubscribe() : null;
                });
            }
        }).catch(function() {})
    );
});
