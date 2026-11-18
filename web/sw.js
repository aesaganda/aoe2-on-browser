// Service worker: cache the WASM build so returning visits load instantly.
//
// aoe2.wasm is 42 MB and changes only on rebuild. aoe2.js is its companion.
// Both use cache-first: a rebuild changes their content (ETag), so the page
// tells the SW to update (via message) after `make web`, or the SW's own
// byte-for-byte check on update picks it up within 24 h.
//
// Everything else (page.js, index.html, aoe2.json, api/, install/, relay/)
// goes network-first so changes are live at once.
'use strict';

const CACHE = 'aoe2-v1';
const PRECACHE = ['aoe2.js', 'aoe2.wasm'];
const CACHE_FIRST = new Set(PRECACHE);

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Only handle same-origin GET requests
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Never cache API, relay, or install requests
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/relay') ||
      url.pathname.startsWith('/install/') || url.pathname === '/health') return;

  const filename = url.pathname.split('/').pop();

  if (CACHE_FIRST.has(filename)) {
    // Cache-first for the big build outputs
    e.respondWith(
      caches.match(e.request).then((cached) => {
        if (cached) return cached;
        return fetch(e.request).then((r) => {
          if (r.ok) {
            const clone = r.clone();
            caches.open(CACHE).then((c) => c.put(e.request, clone));
          }
          return r;
        });
      })
    );
    return;
  }

  // Network-first for page files: fast when online, cached fallback offline
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok) {
          const clone = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return r;
      })
      .catch(() => caches.match(e.request))
  );
});

// The page can ask us to refresh the cached build after `make web`
self.addEventListener('message', (e) => {
  if (e.data === 'update-cache') {
    caches.open(CACHE).then((c) => Promise.all(PRECACHE.map((f) => c.add(f + '?_=' + Date.now()))));
  }
});
