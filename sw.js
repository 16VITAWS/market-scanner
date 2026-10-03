/* VISION AI service worker: app shell cached for offline start; data always network-first
   (falls back to the last copy only when offline, and the portal shows each file's own timestamp). */
const SHELL = 'vision-shell-v2.4';
const SHELL_FILES = ['./', './index.html', './manifest.webmanifest', './icon.svg', './icon-192.png', './icon-512.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL && k !== 'vision-data').map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  if (u.pathname.includes('/api/')) {
    e.respondWith(fetch(e.request).then(r => { if (r.ok) { const cp = r.clone(); caches.open('vision-data').then(c => c.put(e.request.url.split('?')[0], cp)); } return r; })
      .catch(() => caches.match(e.request.url.split('?')[0])));
    return;
  }
  // always ask the server if the page changed (no stale copy after an update); cached copy only when offline
  e.respondWith(fetch(e.request, { cache: 'no-cache' }).then(r => { if (r.ok) { const cp = r.clone(); caches.open(SHELL).then(c => c.put(e.request, cp)); } return r; }).catch(() => caches.match(e.request)));
});
