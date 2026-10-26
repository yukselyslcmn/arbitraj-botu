import logging
import random
import time
from typing import Any, Optional
from urllib.parse import urlparse

import config

logger = logging.getLogger("arbitraj.http_client")

# Kütüphane varlık kontrolleri (Öncelik: curl_cffi -> cloudscraper -> httpx -> requests)
try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False

try:
    import cloudscraper

    HAS_CLOUDSCRAPER = True
except ImportError:
    cloudscraper = None
    HAS_CLOUDSCRAPER = False

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    httpx = None
    HAS_HTTPX = False

import requests as std_requests


class HttpResponseWrapper:
    """Farklı HTTP kütüphanelerinin yanıtlarını standartlaştıran sarmalayıcı sınıf."""

    def __init__(
        self,
        status_code: int,
        text: str,
        content: bytes,
        url: str,
        headers: dict[str, Any],
        raw_response: Any = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content
        self.url = str(url)
        self.headers = dict(headers) if headers else {}
        self._raw = raw_response

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> Any:
        if self._raw is not None and hasattr(self._raw, "json"):
            return self._raw.json()
        import json

        return json.loads(self.text)


class StealthHttpClient:
    """
    Cloudflare, Imperva ve Akinon WAF korumalarını aşmak için tasarlanmış,
    TLS (JA3) parmak izi taklit yetenekli, oturum (cookie) ve Exponential Backoff
    destekli HTTP istemcisi.
    """

    CHROME_USER_AGENTS = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
    ]

    IMPERSONATE_PROFILES = ["chrome124", "chrome120", "chrome116", "chrome110"]

    def __init__(
        self,
        min_delay: float = config.MIN_DELAY_SECONDS,
        max_delay: float = config.MAX_DELAY_SECONDS,
        max_retries: int = config.MAX_RETRIES,
        timeout: int = config.REQUEST_TIMEOUT,
        proxy: Optional[str] = None,
    ) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.proxy = proxy or config.HTTP_PROXY or None
        self._last_request_ts: float = 0.0
        self._user_agent: str = self.CHROME_USER_AGENTS[0]

        self._curl_session: Any = None
        self._cloudscraper_session: Any = None
        self._httpx_client: Any = None
        self._std_session: Optional[std_requests.Session] = None
        self._impersonate_target: str = "chrome124"

        self._init_sessions()

    def _default_headers(self, referer: Optional[str] = None, is_json: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": (
                "application/json, text/plain, */*"
                if is_json
                else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty" if is_json else "document",
            "Sec-Fetch-Mode": "cors" if is_json else "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Upgrade-Insecure-Requests": "1",
        }
        if is_json:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer
        return headers

    def _init_sessions(self) -> None:
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

        if HAS_CURL_CFFI and curl_requests is not None:
            for profile in self.IMPERSONATE_PROFILES:
                try:
                    self._curl_session = curl_requests.Session(
                        impersonate=profile,
                        proxies=proxies,
                        timeout=self.timeout,
                    )
                    self._impersonate_target = profile
                    logger.debug("curl_cffi oturumu başlatıldı (impersonate=%s)", profile)
                    break
                except Exception as exc:
                    logger.debug("curl_cffi profil %s başlatılamadı: %s", profile, exc)
                    self._curl_session = None

        if HAS_CLOUDSCRAPER and cloudscraper is not None:
            try:
                self._cloudscraper_session = cloudscraper.create_scraper(
                    browser={
                        "browser": "chrome",
                        "platform": "windows",
                        "desktop": True,
                    }
                )
                if proxies:
                    self._cloudscraper_session.proxies.update(proxies)
            except Exception as exc:
                logger.debug("cloudscraper başlatılamadı: %s", exc)
                self._cloudscraper_session = None

        if HAS_HTTPX and httpx is not None:
            try:
                self._httpx_client = httpx.Client(
                    headers=self._default_headers(),
                    timeout=self.timeout,
                    follow_redirects=True,
                    proxy=self.proxy,
                )
            except Exception as exc:
                logger.debug("httpx istemcisi başlatılamadı: %s", exc)
                self._httpx_client = None

        self._std_session = std_requests.Session()
        self._std_session.headers.update(self._default_headers())
        if proxies:
            self._std_session.proxies.update(proxies)

    def _throttle(self) -> None:
        """İstekler arası rastgele bekleme (rate-limit / WAF koruma)."""
        elapsed = time.time() - self._last_request_ts
        target_wait = random.uniform(self.min_delay, self.max_delay)
        if self._last_request_ts > 0 and elapsed < target_wait:
            sleep_duration = target_wait - elapsed
            time.sleep(sleep_duration)
        self._last_request_ts = time.time()

    def _execute_single_request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        data: Optional[Any] = None,
        json_payload: Optional[Any] = None,
        is_json: bool = False,
        engine_preference: Optional[str] = None,
    ) -> HttpResponseWrapper:
        parsed = urlparse(url)
        origin_referer = f"{parsed.scheme}://{parsed.netloc}/"
        merged_headers = self._default_headers(referer=origin_referer, is_json=is_json)
        if headers:
            merged_headers.update(headers)

        engines = ["curl_cffi", "cloudscraper", "httpx", "requests"]
        if engine_preference and engine_preference in engines:
            engines.remove(engine_preference)
            engines.insert(0, engine_preference)

        last_exc: Optional[Exception] = None

        for engine in engines:
            try:
                if engine == "curl_cffi" and self._curl_session is not None:
                    resp = self._curl_session.request(
                        method=method,
                        url=url,
                        params=params,
                        headers=merged_headers,
                        data=data,
                        json=json_payload,
                        timeout=self.timeout,
                        allow_redirects=True,
                    )
                    return HttpResponseWrapper(
                        status_code=resp.status_code,
                        text=resp.text,
                        content=resp.content,
                        url=str(resp.url),
                        headers=dict(resp.headers),
                        raw_response=resp,
                    )

                if engine == "cloudscraper" and self._cloudscraper_session is not None:
                    resp = self._cloudscraper_session.request(
                        method=method,
                        url=url,
                        params=params,
                        headers=merged_headers,
                        data=data,
                        json=json_payload,
                        timeout=self.timeout,
                        allow_redirects=True,
                    )
                    return HttpResponseWrapper(
                        status_code=resp.status_code,
                        text=resp.text,
                        content=resp.content,
                        url=str(resp.url),
                        headers=dict(resp.headers),
                        raw_response=resp,
                    )

                if engine == "httpx" and self._httpx_client is not None:
                    resp = self._httpx_client.request(
                        method=method,
                        url=url,
                        params=params,
                        headers=merged_headers,
                        data=data,
                        json=json_payload,
                    )
                    return HttpResponseWrapper(
                        status_code=resp.status_code,
                        text=resp.text,
                        content=resp.content,
                        url=str(resp.url),
                        headers=dict(resp.headers),
                        raw_response=resp,
                    )

                if engine == "requests" and self._std_session is not None:
                    resp = self._std_session.request(
                        method=method,
                        url=url,
                        params=params,
                        headers=merged_headers,
                        data=data,
                        json=json_payload,
                        timeout=self.timeout,
                        allow_redirects=True,
                    )
                    return HttpResponseWrapper(
                        status_code=resp.status_code,
                        text=resp.text,
                        content=resp.content,
                        url=str(resp.url),
                        headers=dict(resp.headers),
                        raw_response=resp,
                    )
            except Exception as exc:
                last_exc = exc
                logger.debug("Motor %s ile %s isteği başarısız oldu: %s", engine, url, exc)
                continue

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Hiçbir HTTP motoru isteği tamamlayamadı: {url}")

    def request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        data: Optional[Any] = None,
        json_payload: Optional[Any] = None,
        is_json: bool = False,
        skip_throttle: bool = False,
    ) -> Optional[HttpResponseWrapper]:
        """
        Exponential Backoff ve WAF / Cloudflare engel tespitli ana HTTP istek metodu.
        403 / 429 / 503 durumlarında motor değiştirerek ve artan bekleme süreleriyle tekrar dener.
        """
        if not skip_throttle:
            self._throttle()

        fallback_engines = ["curl_cffi", "cloudscraper", "httpx"]

        for attempt in range(1, self.max_retries + 1):
            engine_pref = fallback_engines[(attempt - 1) % len(fallback_engines)]
            try:
                response = self._execute_single_request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    headers=headers,
                    data=data,
                    json_payload=json_payload,
                    is_json=is_json,
                    engine_preference=engine_pref if attempt > 1 else "curl_cffi",
                )

                # Başarılı yanıt
                if response.status_code in (200, 201):
                    return response

                # 404 Not Found için yeniden denemeye gerek yok
                if response.status_code == 404:
                    logger.debug("404 Bulunamadı: %s", url)
                    return response

                # WAF / Rate Limit / Sunucu Hataları (403, 429, 500, 502, 503, 504)
                if response.status_code in (403, 429, 500, 502, 503, 504):
                    backoff = (2 ** attempt) + random.uniform(0.8, 2.2)
                    logger.warning(
                        "HTTP %d alındı (%s) [Deneme %d/%d]. %.1f sn bekleniyor...",
                        response.status_code,
                        url,
                        attempt,
                        self.max_retries,
                        backoff,
                    )
                    self._user_agent = random.choice(self.CHROME_USER_AGENTS)
                    time.sleep(backoff)
                    continue

                return response

            except Exception as exc:
                backoff = (2 ** attempt) + random.uniform(0.5, 1.8)
                logger.warning(
                    "İstek hatası (%s) [Deneme %d/%d]: %s. %.1f sn bekleniyor...",
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                    backoff,
                )
                if attempt < self.max_retries:
                    time.sleep(backoff)

        logger.error("Maksimum deneme sayısına ulaşıldı, istek başarısız: %s", url)
        return None

    def get(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        is_json: bool = False,
        skip_throttle: bool = False,
    ) -> Optional[HttpResponseWrapper]:
        return self.request(
            method="GET",
            url=url,
            params=params,
            headers=headers,
            is_json=is_json,
            skip_throttle=skip_throttle,
        )

    def get_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Optional[Any]:
        resp = self.get(url=url, params=params, headers=headers, is_json=True)
        if resp is None or not resp.ok:
            return None
        try:
            return resp.json()
        except Exception as exc:
            logger.debug("JSON ayrıştırma hatası (%s): %s", url, exc)
            return None

    def post(
        self,
        url: str,
        data: Optional[Any] = None,
        json_payload: Optional[Any] = None,
        headers: Optional[dict[str, str]] = None,
        skip_throttle: bool = True,
    ) -> Optional[HttpResponseWrapper]:
        return self.request(
            method="POST",
            url=url,
            data=data,
            json_payload=json_payload,
            headers=headers,
            skip_throttle=skip_throttle,
        )

    def close(self) -> None:
        for session_obj in (
            self._curl_session,
            self._cloudscraper_session,
            self._httpx_client,
            self._std_session,
        ):
            if session_obj is not None and hasattr(session_obj, "close"):
                try:
                    session_obj.close()
                except Exception as exc:
                    logger.debug("Oturum kapatılırken hata yoksayıldı: %s", exc)
