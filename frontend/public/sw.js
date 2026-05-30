const CACHE_VERSION = "v3";
const STATIC_CACHE = `static-${CACHE_VERSION}`;
const API_CACHE = `api-${CACHE_VERSION}`;

const STATIC_ASSETS = ["/", "/offline", "/manifest.json"];

const API_CACHE_PATTERNS = [
  /\/api\/calendar\/upcoming/,
  /\/api\/agent\/control-plane\/status/,
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(STATIC_CACHE)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== STATIC_CACHE && k !== API_CACHE)
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET and cross-origin
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  // API requests: network-first with short cache for select endpoints
  if (url.pathname.startsWith("/api/")) {
    const shouldCache = API_CACHE_PATTERNS.some((p) => p.test(url.pathname));
    if (shouldCache) {
      event.respondWith(
        fetch(request)
          .then((res) => {
            const clone = res.clone();
            caches.open(API_CACHE).then((c) => c.put(request, clone));
            return res;
          })
          .catch(() => caches.match(request)),
      );
    }
    return; // other API calls: let them fail normally (no offline fallback for mutations)
  }

  // Page navigations (HTML): NETWORK-FIRST so the app shell is never stale.
  // Falls back to cache, then the offline page, when the network is down.
  const isNavigation =
    request.mode === "navigate" ||
    (request.headers.get("accept") || "").includes("text/html");
  if (isNavigation) {
    event.respondWith(
      fetch(request)
        .then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(STATIC_CACHE).then((c) => c.put(request, clone));
          }
          return res;
        })
        .catch(() =>
          caches.match(request).then((c) => c || caches.match("/offline")),
        ),
    );
    return;
  }

  // Hashed/static assets (/_next/static, images, fonts, manifest): cache-first.
  event.respondWith(
    caches.match(request).then(
      (cached) =>
        cached ||
        fetch(request)
          .then((res) => {
            if (res.ok) {
              const clone = res.clone();
              caches.open(STATIC_CACHE).then((c) => c.put(request, clone));
            }
            return res;
          })
          .catch(() => caches.match("/offline")),
    ),
  );
});

// Receive queued uploads from the client
self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});
