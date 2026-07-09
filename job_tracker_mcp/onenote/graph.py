"""Microsoft Graph HTTP layer: retry, backoff, paging, throttling, redaction.

No Microsoft SDK. msgraph-sdk drags in a Kiota runtime and a large async stack
for what is four HTTP calls. httpx is already a dependency.

`transport` is injectable, which is the entire test strategy: httpx.MockTransport
gives hermetic tests with zero network and needs no new dependency.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator

import httpx

log = logging.getLogger(__name__)

BASE = "https://graph.microsoft.com/v1.0"
MAX_ATTEMPTS = 5
MAX_BACKOFF = 60.0


class GraphError(Exception):
    """A Graph call failed in a way the caller must surface, not retry."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class AuthExpired(GraphError):
    """The token is gone or stale. The user must re-run onenote-auth.sh."""


def _redact(headers) -> dict:
    """Never let a bearer token reach a log line. Do not rely on discipline."""
    out = dict(headers)
    if "Authorization" in out:
        out["Authorization"] = "Bearer <redacted>"
    if "authorization" in out:
        out["authorization"] = "Bearer <redacted>"
    return out


def _graph_error(resp: httpx.Response) -> GraphError:
    """Graph's error bodies are genuinely informative. Surface them."""
    code = message = None
    try:
        err = resp.json().get("error", {})
        code, message = err.get("code"), err.get("message")
    except Exception:  # noqa: BLE001 -- a non-JSON body is itself the diagnosis
        message = (resp.text or "")[:200]
    return GraphError(
        f"Graph {resp.status_code}"
        + (f" {code}" if code else "")
        + (f": {message}" if message else ""),
        status=resp.status_code, code=code,
    )


class GraphClient:
    def __init__(self, token_provider: Callable[[], str],
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep):
        self._token = token_provider
        self._sleep = sleep                 # injectable so tests do not really wait
        self.throttled_retries = 0
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ transport

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{BASE}{path}"

    def _request(self, method: str, path: str, *, params=None, content=None,
                 headers=None, refreshed: bool = False) -> httpx.Response:
        url = self._url(path)
        hdrs = {"Authorization": f"Bearer {self._token()}", **(headers or {})}

        for attempt in range(MAX_ATTEMPTS):
            log.debug("graph %s %s headers=%s", method, url, _redact(hdrs))
            try:
                resp = self._client.request(method, url, params=params,
                                            content=content, headers=hdrs)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise GraphError(f"Network error after {MAX_ATTEMPTS} attempts: {exc}")
                self._sleep(self._backoff(attempt))
                continue

            if resp.status_code == 401:
                # Exactly one silent refresh, then give up. Never loop on 401.
                if refreshed:
                    raise AuthExpired(
                        "OneNote is not authenticated, or the saved sign-in expired."
                    )
                log.info("graph 401; retrying once with a refreshed token")
                return self._request(method, path, params=params, content=content,
                                     headers=headers, refreshed=True)

            if resp.status_code == 429:
                # Graph told you when to come back. Do not add your own jitter.
                delay = self._retry_after(resp, attempt)
                self.throttled_retries += 1
                log.warning("graph 429; sleeping %.1fs (Retry-After honored)", delay)
                self._sleep(delay)
                continue

            if resp.status_code in (503, 504):
                if attempt == MAX_ATTEMPTS - 1:
                    raise _graph_error(resp)
                self._sleep(self._backoff(attempt))
                continue

            if resp.status_code >= 400:
                raise _graph_error(resp)

            return resp

        raise GraphError(f"Graph {method} {path} failed after {MAX_ATTEMPTS} attempts")

    @staticmethod
    def _retry_after(resp: httpx.Response, attempt: int) -> float:
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return min(2.0 ** attempt, MAX_BACKOFF)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0 ** attempt + random.uniform(0, 1), MAX_BACKOFF)

    # ----------------------------------------------------------------- verbs

    def get_json(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params).json()

    def get_text(self, path: str, params: dict | None = None) -> str:
        return self._request("GET", path, params=params).text

    def get_all(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Follow @odata.nextLink until absent.

        The nextLink already embeds the full query. Re-appending $top/$select to
        it produces a 400, so it is requested verbatim and params are dropped
        after the first page.
        """
        page = self.get_json(path, params=params)
        while True:
            yield from page.get("value", [])
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return
            page = self.get_json(nxt)          # verbatim: no params

    def post_html(self, path: str, html: str) -> dict:
        resp = self._request("POST", path, content=html.encode("utf-8"),
                             headers={"Content-Type": "text/html"})
        return resp.json()
