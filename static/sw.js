// Service Worker para Blackburn-Pulse PWA
const CACHE_NAME = 'blackburn-pulse-v2';
const ASSETS_TO_CACHE = [
  '/',
  '/manifest.json',
  '/manifest.json?v=2',
  '/static/manifest.json',
  '/static/icon-192.svg',
  '/static/icon-192.svg?v=2',
  '/static/icon-512.svg',
  '/static/icon-512.svg?v=2'
];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(ASSETS_TO_CACHE).catch(() => {});
    })
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.map(key => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Estratégia Network First (prioriza dados em tempo real da rede, usa cache se offline)
self.addEventListener('fetch', event => {
  // Apenas métodos GET e ignora requisições de API em tempo real
  if (event.request.method !== 'GET' || event.request.url.includes('/api/')) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Se a resposta for válida, clona para o cache
        if (response && response.status === 200) {
          const responseToCache = response.clone();
          caches.open(CACHE_NAME).then(cache => {
            cache.put(event.request, responseToCache);
          });
        }
        return response;
      })
      .catch(() => {
        return caches.match(event.request);
      })
  );
});

