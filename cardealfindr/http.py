"""
One HTTP helper for every source: caching, throttling, retries.

Why this exists:
  * MarketCheck free plan = 500 calls/month and 5 calls/second. Re-running
    the tool while you tweak scoring should NOT burn quota, so every GET is
    cached in the SQLite `http_cache` table for a configurable TTL.
  * 429 (rate limited) and 5xx responses are retried with exponential
    backoff (2s, 4s, 8s, ...), honouring a Retry-After header if present.
  * Dealer sites get a polite fixed delay between requests.

Usage:
    http = CachedHttp(db)
    data = http.get_json("https://api...", params={...}, ttl_hours=6,
                         min_interval=0.4, headers={...})
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    """Raised after retries are exhausted or on a non-retryable 4xx."""


class CachedHttp:
    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(self, db, user_agent: str = "CarDealFindr/0.1 (+personal car-shopping tool)",
                 use_cache: bool = True):
        self.db = db
        self.use_cache = use_cache
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._last_call_at: dict[str, float] = {}   # host -> monotonic time of last request

    # ------------------------------------------------------------------ keys
    @staticmethod
    def cache_key(method: str, url: str, params: Optional[dict], body: Any = None,
                  secret_params: tuple[str, ...] = ("api_key", "token")) -> str:
        """
        Stable hash of the request. API keys are stripped from the key so the
        cache survives a key rotation and never stores secrets in plain text.
        """
        clean = {k: v for k, v in (params or {}).items() if k not in secret_params}
        payload = json.dumps([method.upper(), url, sorted(clean.items()), body],
                             sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    # -------------------------------------------------------------- throttle
    def _throttle(self, url: str, min_interval: float) -> None:
        host = url.split("/")[2] if "//" in url else url
        last = self._last_call_at.get(host)
        if last is not None and min_interval > 0:
            wait = min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call_at[host] = time.monotonic()

    # ------------------------------------------------------------------ core
    def request(self, method: str, url: str, *, params: Optional[dict] = None,
                json_body: Any = None, headers: Optional[dict] = None,
                ttl_hours: float = 0, min_interval: float = 0,
                max_retries: int = 5, timeout: int = 60) -> tuple[int, str, bool]:
        """
        Returns (status_code, body_text, from_cache).
        Only 2xx responses are cached.
        """
        key = self.cache_key(method, url, params, json_body)
        if self.use_cache and ttl_hours > 0:
            hit = self.db.cache_get(key)
            if hit is not None:
                log.debug("cache hit %s", url)
                return hit["status"], hit["body"], True

        attempt = 0
        delay = 2.0
        while True:
            attempt += 1
            self._throttle(url, min_interval)
            try:
                resp = self.session.request(method, url, params=params, json=json_body,
                                            headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                if attempt > max_retries:
                    raise HttpError(f"{method} {url} failed after {attempt} attempts: {exc}") from exc
                log.warning("network error on %s (%s); retry in %.0fs", url, exc, delay)
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code in self.RETRYABLE and attempt <= max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                log.warning("HTTP %s from %s; waiting %.0fs (attempt %d/%d)",
                            resp.status_code, url.split("?")[0], wait, attempt, max_retries)
                time.sleep(wait)
                delay *= 2
                continue

            if 200 <= resp.status_code < 300:
                if self.use_cache and ttl_hours > 0:
                    self.db.cache_put(key, url, resp.status_code, resp.text, ttl_hours)
                return resp.status_code, resp.text, False

            # Non-retryable (or retries exhausted)
            raise HttpError(f"HTTP {resp.status_code} from {url.split('?')[0]}: {resp.text[:300]}")

    def get_json(self, url: str, **kw) -> Any:
        status, text, _ = self.request("GET", url, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise HttpError(f"Non-JSON response from {url.split('?')[0]}: {text[:200]}") from exc

    def get_text(self, url: str, **kw) -> str:
        _, text, _ = self.request("GET", url, **kw)
        return text

    def post_json(self, url: str, json_body: Any, **kw) -> Any:
        status, text, _ = self.request("POST", url, json_body=json_body, **kw)
        return json.loads(text) if text else {}
