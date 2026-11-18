// Service worker: cache the WASM build so returning visits load instantly.
//
// aoe2.wasm is 42 MB and changes only on rebuild. aoe2.js is its companion.
// Both use cache-first: a rebuild changes their content (ETag), so the page
// tells the SW to update (via message) after `make web`, or the SW's own
// byte-for-byte check on update picks it up within 24 h.
//
// Everything else (page.js, index.html, aoe2.json, api/, install/, relay/)
// goes network-first so changes are live at once.
//
// The cache name includes the build version from /api/version. When the
// build changes, the old cache is dropped on activation.
'use strict';

const CACHE_PREFIX = 'aoe2-';
const PRECACHE = ['aoe2.js'];
const CACHE_FIRST = new Set(['aoe2.js', 'aoe2.wasm']);

let cacheName = CACHE_PREFIX + 'v1';

async function getVersionedCacheName() {
  try {
    const r = await fetch('/api/version', { cache: 'no-store' });
    const j = await r.json();
    if (j.version) return CACHE_PREFIX + j.version;
  } catch {}
  return cacheName;
}

self.addEventListener('install', (e) => {
  e.waitUntil(
    getVersionedCacheName().then((name) => {
      cacheName = name;
      return caches.open(cacheName)
        .then((c) => c.addAll(PRECACHE))
        .then(() => self.skipWaiting());
    })
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    getVersionedCacheName().then((name) => {
      cacheName = name;
      return caches.keys()
        .then((keys) => Promise.all(keys.filter((k) => k.startsWith(CACHE_PREFIX) && k !== cacheName).map((k) => caches.delete(k))))
        .then(() => self.clients.claim());
    })
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/relay') ||
      url.pathname.startsWith('/install/') || url.pathname === '/health') return;

  const filename = url.pathname.split('/').pop();

  if (CACHE_FIRST.has(filename)) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        if (cached) return cached;
        return fetch(e.request).then((r) => {
          if (r.ok) {
            const clone = r.clone();
            caches.open(cacheName).then((c) => c.put(e.request, clone));
          }
          return r;
        });
      })
    );
    return;
  }

  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok) {
          const clone = r.clone();
          caches.open(cacheName).then((c) => c.put(e.request, clone));
        }
        return r;
      })
      .catch(() => caches.match(e.request))
  );
});

self.addEventListener('message', (e) => {
  if (e.data === 'update-cache') {
    getVersionedCacheName().then((name) => {
      cacheName = name;
      caches.open(cacheName).then((c) => Promise.all(PRECACHE.map((f) => c.add(f + '?_=' + Date.now()))));
    });
  }
});
