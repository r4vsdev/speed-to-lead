"""HTTP calls with retries for rate limits and server errors."""

import logging
import time

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    sleep=time.sleep,
    **kwargs,
) -> httpx.Response:
    """Send a request; retry 429/5xx and network errors with backoff (honours Retry-After).

    Other 4xx errors are raised at once: retrying a bad request never fixes it.
    """
    for attempt in range(1, attempts + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning("network error %s on %s %s, retry in %.1fs", e, method, url, delay)
            sleep(delay)
            continue
        if resp.status_code < 400:
            return resp
        if resp.status_code not in RETRY_STATUS or attempt == attempts:
            raise ApiError(resp.status_code, resp.text)
        delay = _retry_after(resp) or base_delay * 2 ** (attempt - 1)
        log.warning("HTTP %s on %s %s, retry %d/%d in %.1fs", resp.status_code, method, url, attempt, attempts - 1, delay)
        sleep(delay)
    raise AssertionError("unreachable")


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return min(float(value), 30.0) if value else None
    except ValueError:
        return None
