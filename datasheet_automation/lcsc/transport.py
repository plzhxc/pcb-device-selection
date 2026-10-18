"""Fetcher: the HTTP seam for LCSC access.

The LCSC logic (search ranking, record mapping, card fields, WAF fallback) is
pure but was welded to a self-built ``requests.Session``. Fetcher lifts that seam
out: production uses :class:`RequestsFetcher`; tests inject a recorded-response
adapter, so the logic runs offline. The fetcher owns the session, the retry
policy, and the cookie jar (challenge solvers return cookies; the fetcher stores
them).
"""

from __future__ import annotations

import time
from typing import Any, Protocol


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Referer": "https://www.szlcsc.com/",
}


class Fetcher(Protocol):
    """A GET-with-retry transport that also carries a cookie jar."""

    def get(self, url: str, *, stream: bool = False) -> Any: ...

    def set_cookie(self, name: str, value: str, *, domain: str, path: str = "/") -> None: ...

    @property
    def user_agent(self) -> str: ...


class RequestsFetcher:
    """Default Fetcher over ``requests.Session`` with a retry/backoff policy."""

    def __init__(self, timeout: int = 30, retries: int = 3):
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "requests is required for LCSC access"
            ) from exc
        self._requests = requests
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)

    @property
    def user_agent(self) -> str:
        return self.session.headers.get("User-Agent", "")

    def set_cookie(self, name: str, value: str, *, domain: str, path: str = "/") -> None:
        self.session.cookies.set(name, value, domain=domain, path=path)

    def get(self, url: str, *, stream: bool = False) -> Any:
        last_error: Exception | None = None
        last_response: Any = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request("GET", url, timeout=self.timeout, stream=stream)
                if response.status_code in (429, 500, 502, 503, 504):
                    last_response = response
                    time.sleep(min(2 * attempt, 8))
                    continue
                return response
            except self._requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2 * attempt, 8))
        if last_error is not None:
            raise RuntimeError(f"request failed for {url}: {last_error}") from last_error
        if last_response is not None:
            raise RuntimeError(f"request failed for {url}: HTTP {last_response.status_code}")
        raise RuntimeError(f"request failed for {url}")
