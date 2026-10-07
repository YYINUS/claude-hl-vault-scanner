"""HTTP client for Hyperliquid: weight-based rate limiting, retries, backoff."""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Base weights per /info request type (docs: rate-limits-and-user-limits).
BASE_WEIGHT = {
    "clearinghouseState": 2,
    "spotClearinghouseState": 2,
    "l2Book": 2,
    "allMids": 2,
    "exchangeStatus": 2,
    "orderStatus": 2,
    "userRole": 60,
}
DEFAULT_WEIGHT = 20
# Extra weight per N items returned, for list-returning endpoints.
EXTRA_PER_ITEMS = {
    "userFillsByTime": 20,
    "userFills": 20,
    "userFunding": 20,
    "userNonFundingLedgerUpdates": 20,
    "candleSnapshot": 60,
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def request_weight(req_type: str, n_items: int = 0) -> int:
    w = BASE_WEIGHT.get(req_type, DEFAULT_WEIGHT)
    per = EXTRA_PER_ITEMS.get(req_type)
    if per and n_items:
        w += n_items // per
    return w


class WeightLimiter:
    """Token bucket over request weight, shared across worker threads.

    Budget refills continuously at `per_minute * utilization / 60` per second.
    Callers `acquire(base_weight)` before a request and `charge(extra)` after
    learning the response size, which can push the bucket negative (debt).
    """

    def __init__(self, per_minute: int, utilization: float = 0.8):
        self.capacity = per_minute * utilization
        self.rate = self.capacity / 60.0
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()
        self.total_weight = 0

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def acquire(self, weight: float) -> None:
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= weight:
                    self.tokens -= weight
                    self.total_weight += weight
                    return
                wait = (weight - self.tokens) / self.rate
            time.sleep(min(wait, 5.0))

    def charge(self, extra: float) -> None:
        if extra <= 0:
            return
        with self.lock:
            self._refill()
            self.tokens -= extra
            self.total_weight += extra

    def penalize(self, seconds: float) -> None:
        """After a 429, drain the bucket so all workers pause."""
        with self.lock:
            self._refill()
            self.tokens = min(self.tokens, -self.rate * seconds)


@dataclass
class HLClient:
    info_url: str
    vault_list_url: str
    limiter: WeightLimiter
    timeout_s: float = 30
    max_retries: int = 6
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60
    stats: dict = field(default_factory=lambda: {"requests": 0, "retries": 0, "errors": 0})

    def __post_init__(self) -> None:
        self.http = httpx.Client(timeout=self.timeout_s, headers={"Content-Type": "application/json"})

    def close(self) -> None:
        self.http.close()

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self.backoff_max_s, self.backoff_base_s * 2 ** attempt)
        time.sleep(delay * (0.5 + random.random() / 2))

    def info(self, payload: dict[str, Any]) -> Any:
        req_type = payload["type"]
        base = request_weight(req_type)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire(base)
            self.stats["requests"] += 1
            try:
                r = self.http.post(self.info_url, json=payload)
                if r.status_code in RETRYABLE_STATUS:
                    if r.status_code == 429:
                        self.limiter.penalize(10 * self.backoff_base_s * (attempt + 1))
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    self.limiter.charge(request_weight(req_type, len(data)) - base)
                return data
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if isinstance(e, httpx.HTTPStatusError) and status not in RETRYABLE_STATUS:
                    self.stats["errors"] += 1
                    raise
                last_err = e
                self.stats["retries"] += 1
                log.warning("retry %s/%s %s: %s", attempt + 1, self.max_retries, req_type, e)
                self._sleep_backoff(attempt)
        self.stats["errors"] += 1
        raise RuntimeError(f"{req_type} failed after {self.max_retries} retries: {last_err}")

    def vault_list(self) -> Any:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.get(self.vault_list_url, timeout=max(self.timeout_s, 120))
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
                last_err = e
                # Proxy/policy denials won't heal with retries.
                if "403" in str(e) or "CONNECT" in str(e):
                    break
                self._sleep_backoff(attempt)
        raise RuntimeError(f"vault list unavailable: {last_err}")
