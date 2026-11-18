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
const CACHE_FIRST = new Set(['aoe2.js', 'aoe2.wasm']);

let cacheNamePromise = null;

async function getVersionedCacheName() {
  try {
    const r = await fetch('api/version', { cache: 'no-store' });
    if (r.ok) {
      const j = await r.json();
      if (j.version) return CACHE_PREFIX + j.version;
    }
  } catch {}
  return CACHE_PREFIX + 'v1';
}

function getCacheName(forceRefresh = false) {
  if (forceRefresh || !cacheNamePromise) {
    cacheNamePromise = getVersionedCacheName();
  }
  return cacheNamePromise;
}

self.addEventListener('install', (e) => {
  e.waitUntil(getCacheName(true).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    getCacheName(true).then((name) =>
      caches.keys()
        .then((keys) => Promise.all(keys.filter((k) => k.startsWith(CACHE_PREFIX) && k !== name).map((k) => caches.delete(k))))
        .then(() => self.clients.claim())
    )
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (e.request.headers.has('range')) return;

  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/relay') ||
      url.pathname.startsWith('/install/') || url.pathname === '/health' || url.pathname === '/health/deep') return;

  const filename = url.pathname.split('/').pop();

  if (CACHE_FIRST.has(filename)) {
    e.respondWith((async () => {
      const cached = await caches.match(e.request);
      if (cached) return cached;
      const r = await fetch(e.request);
      if (r && r.status === 200) {
        const clone = r.clone();
        const name = await getCacheName();
        caches.open(name).then((c) => c.put(e.request, clone));
      }
      return r;
    })());
    return;
  }

  e.respondWith((async () => {
    try {
      const r = await fetch(e.request);
      if (r && r.status === 200) {
        const clone = r.clone();
        const name = await getCacheName();
        caches.open(name).then((c) => c.put(e.request, clone));
      }
      return r;
    } catch (err) {
      const cached = await caches.match(e.request);
      if (cached) return cached;
      throw err;
    }
  })());
});

self.addEventListener('message', (e) => {
  if (e.data === 'update-cache') {
    e.waitUntil(
      getCacheName(true).then((name) =>
        caches.keys()
          .then((keys) => Promise.all(keys.filter((k) => k.startsWith(CACHE_PREFIX) && k !== name).map((k) => caches.delete(k))))
      )
    );
  }
});
