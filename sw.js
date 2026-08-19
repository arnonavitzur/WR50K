/* Vermont 50K dashboard — service worker
 *
 * Bump CACHE whenever you deploy. That is the only lever: a new cache name
 * forces a fresh precache and drops everything from the previous version.
 */
const CACHE = 'vt50k-v11';

/* All paths are relative to this file, so the worker works unchanged whether
 * the site is served from a user page (user.github.io) or a project page
 * (user.github.io/repo/). */
const PRECACHE = [
  './',
  './50k_dashboard.html',
  './combined_plan.json',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './icon-maskable-512.png',
  './apple-touch-icon.png',
  './favicon-32.png',
  './fit-analysis.js',
  'https://unpkg.com/react@18/umd/react.production.min.js',
  'https://unpkg.com/react-dom@18/umd/react-dom.production.min.js',
  'https://unpkg.com/@babel/standalone@7.23.10/babel.min.js',
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600;700&display=swap',
];

/* cache.addAll() is atomic — a single 404 aborts the whole install and the
 * worker never activates. Cache entries individually instead so one missing
 * file (or a flaky CDN) can't take the install down with it. */
async function precache() {
  const cache = await caches.open(CACHE);
  const results = await Promise.allSettled(
    PRECACHE.map(async (url) => {
      const res = await fetch(url, { cache: 'reload' });
      if (!res.ok && res.type !== 'opaque') throw new Error(url + ' → ' + res.status);
      await cache.put(url, res.clone());
    })
  );
  const failed = results
    .map((r, i) => (r.status === 'rejected' ? PRECACHE[i] : null))
    .filter(Boolean);
  if (failed.length) console.warn('[sw] not precached:', failed);
}

self.addEventListener('install', (event) => {
  event.waitUntil(precache());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)));
      if (self.registration.navigationPreload) {
        await self.registration.navigationPreload.enable();
      }
      await self.clients.claim();
    })()
  );
});

/* The page posts this when the user accepts an update. */
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

function isHTML(request) {
  return (
    request.mode === 'navigate' ||
    (request.headers.get('accept') || '').includes('text/html')
  );
}

/* The plan is the one thing here that changes on its own schedule, independent
 * of a code deploy. It must never be served cache-first or an edited plan would
 * never reach the device. */
function isPlan(request) {
  return new URL(request.url).pathname.endsWith('/combined_plan.json');
}

self.addEventListener('fetch', (event) => {
  const { request } = event;

  if (request.method !== 'GET') return;
  if (!/^https?:$/.test(new URL(request.url).protocol)) return;

  const html = isHTML(request);
  const plan = isPlan(request);

  /* HTML and plan data: network first, so a deploy or a plan edit shows up on
   * the next online load. Falls back to cache when offline. */
  if (html || plan) {
    event.respondWith(
      (async () => {
        try {
          const preload = html ? await event.preloadResponse : null;
          const res = preload || (await fetch(request));
          if (res && res.ok) {
            const cache = await caches.open(CACHE);
            cache.put(request, res.clone());
          }
          return res;
        } catch (err) {
          const cached =
            (await caches.match(request)) ||
            (plan ? await caches.match('./combined_plan.json') : null) ||
            (html ? await caches.match('./50k_dashboard.html') : null) ||
            (html ? await caches.match('./') : null);
          if (cached) {
            /* Tag cache fallbacks. Without this the page cannot tell a fresh
             * 200 from a cached one, so an update check made while the network
             * is down looks identical to "you already have the latest" — it
             * would report up-to-date having never reached the server. */
            const headers = new Headers(cached.headers);
            headers.set('X-Served-From', 'sw-cache');
            return new Response(cached.body, {
              status: cached.status,
              statusText: cached.statusText,
              headers,
            });
          }
          throw err;
        }
      })()
    );
    return;
  }

  /* Everything else — scripts, fonts, icons: cache first. These are either
   * version-pinned or static, so the network round trip buys nothing. */
  event.respondWith(
    (async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      const res = await fetch(request);
      if (res.ok || res.type === 'opaque') {
        const cache = await caches.open(CACHE);
        cache.put(request, res.clone());
      }
      return res;
    })()
  );
});
