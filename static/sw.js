/**
 * AI Data Insight Builder - Service Worker (PWA)
 * 코어 정적 자원 캐싱 및 모바일 앱 셸 구동 지원
 */

const CACHE_NAME = 'insight-builder-pwa-v1';

// 오프라인 및 빠른 기동을 위한 필수 앱 셸 리소스
const PRECACHE_ASSETS = [
  '/',
  '/manifest.json',
  '/static/style.css',
  '/static/app.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon.svg',
  'https://cdn.jsdelivr.net/npm/chart.js',
  'https://cdn.jsdelivr.net/npm/marked/marked.min.js'
];

// 1. 설치(Install): 핵심 자원 프리캐싱
self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[SW] PWA 코어 자원 캐싱 완료');
      return cache.addAll(PRECACHE_ASSETS).catch((err) => {
        console.warn('[SW] 일부 외부 자원 프리캐싱 생략:', err);
      });
    })
  );
});

// 2. 활성화(Activate): 구버전 캐시 정리 및 즉시 제어
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => {
            console.log('[SW] 이전 캐시 삭제:', name);
            return caches.delete(name);
          })
      );
    }).then(() => self.clients.claim())
  );
});

// 3. 네트워크 요청 처리(Fetch)
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // POST 요청 및 분석/인증 API는 절대로 캐시하지 않고 항상 네트워크로 직접 전송
  if (
    req.method !== 'GET' ||
    url.pathname.startsWith('/api/') ||
    url.pathname === '/inspect' ||
    url.pathname === '/suggest' ||
    url.pathname === '/run' ||
    url.pathname === '/explain' ||
    url.pathname === '/followup' ||
    url.pathname === '/query_ai' ||
    url.pathname === '/health'
  ) {
    return; // 기본 네트워크 처리로 바이패스
  }

  // HTML 문서 탐색(Navigation): 네트워크 우선, 오프라인 시 캐시된 메인 화면 표시
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((networkRes) => {
          return caches.open(CACHE_NAME).then((cache) => {
            cache.put(req, networkRes.clone());
            return networkRes;
          });
        })
        .catch(() => {
          return caches.match('/');
        })
    );
    return;
  }

  // 정적 리소스(JS, CSS, 이미지, 폰트): Stale-While-Revalidate 전략
  event.respondWith(
    caches.match(req).then((cachedRes) => {
      const fetchPromise = fetch(req)
        .then((networkRes) => {
          if (networkRes && networkRes.status === 200) {
            const resClone = networkRes.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(req, resClone);
            });
          }
          return networkRes;
        })
        .catch(() => cachedRes);

      return cachedRes || fetchPromise;
    })
  );
});
