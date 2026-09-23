import time
import asyncio
import httpx
import logging
from urllib.parse import urlparse
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class CircuitBreakerOpenException(Exception):
    """Raised when the circuit breaker is open and requests are blocked to prevent cascading failures."""
    pass

class SimpleCircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    def record_success(self):
        if self.failure_count > 0 or self.state != "CLOSED":
            logger.info(f"[CircuitBreaker] State transitioning from {self.state} to CLOSED after success.")
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(f"[CircuitBreaker] State transitioned to OPEN after {self.failure_count} consecutive failures.")

    def can_execute(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                logger.info("[CircuitBreaker] Recovery timeout elapsed. Transitioning to HALF_OPEN.")
                return True
            return False
        # HALF_OPEN allows one trial request
        return True


class ResilientHttpClient:
    """
    Standard resilient HTTP client wrapper providing:
    - Configurable timeouts
    - Capped exponential backoff retries for idempotent requests (GET) and 5xx/network errors
    - Lightweight circuit breaker per host/domain
    """
    def __init__(self, default_timeout: float = 10.0, max_retries: int = 3, connect_timeout: float = 5.0):
        self.timeout = httpx.Timeout(default_timeout, connect=min(default_timeout, connect_timeout))
        self.max_retries = max_retries
        self.client = httpx.AsyncClient(timeout=self.timeout)
        self.breakers: Dict[str, SimpleCircuitBreaker] = {}

    def _get_breaker(self, url: str) -> SimpleCircuitBreaker:
        domain = urlparse(url).netloc or "default"
        if domain not in self.breakers:
            self.breakers[domain] = SimpleCircuitBreaker()
        return self.breakers[domain]

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        breaker = self._get_breaker(url)
        if not breaker.can_execute():
            raise CircuitBreakerOpenException(f"Circuit breaker is OPEN for host '{urlparse(url).netloc}'. Fast-failing request.")

        # Retry idempotent GET requests, or explicit retry flag
        retries = self.max_retries if method.upper() == "GET" or kwargs.pop("retry", False) else 1
        delay = 1.0

        for attempt in range(1, retries + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                if response.status_code >= 500:
                    if attempt < retries:
                        logger.warning(f"[ResilientHttpClient] {method} {url} returned {response.status_code}. Retrying in {delay}s (Attempt {attempt}/{retries})...")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 8.0)
                        continue
                    breaker.record_failure()
                    return response
                
                breaker.record_success()
                return response
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < retries:
                    logger.warning(f"[ResilientHttpClient] {method} {url} failed with {type(e).__name__}. Retrying in {delay}s (Attempt {attempt}/{retries})...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8.0)
                    continue
                breaker.record_failure()
                raise

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def close(self):
        await self.client.aclose()

# Global default instance
_default_resilient_client: Optional[ResilientHttpClient] = None

def get_resilient_client() -> ResilientHttpClient:
    global _default_resilient_client
    if _default_resilient_client is None:
        _default_resilient_client = ResilientHttpClient()
    return _default_resilient_client
