"""Typed HTTP client adapter using `httpx` with connection pooling and explicit `Result` returns."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from contraband.core.errors import NetworkError
from contraband.core.result import Err, Ok, Result

DEFAULT_USER_AGENT = "Contraband-CLI/2.0"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Immutable HTTP response wrapper."""

    status_code: int
    data: Any
    text: str


class HttpClientProtocol(Protocol):
    """Protocol for making HTTP requests from the imperative shell."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        raw_body: bytes | None = None,
        timeout: float = 30.0,
    ) -> Result[HttpResponse, NetworkError]: ...

    def fetch_html(self, url: str, *, timeout: float = 15.0) -> str | None: ...


class HttpxClient:
    """Production `HttpClientProtocol` implementation backed by a pooled `httpx.Client`."""

    def __init__(self) -> None:
        self._client = httpx.Client(follow_redirects=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Closes the underlying connection pool."""
        self._client.close()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        raw_body: bytes | None = None,
        timeout: float = 30.0,
    ) -> Result[HttpResponse, NetworkError]:
        if not url.startswith(("https://", "http://")):
            return Err(NetworkError(message=f"Unsupported URL scheme for {url}", url=url))

        req_headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if headers:
            req_headers.update(headers)

        content_bytes: bytes | None = raw_body
        if json_body is not None:
            content_bytes = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")

        try:
            resp = self._client.request(
                method=method,
                url=url,
                headers=req_headers,
                content=content_bytes,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            return Err(
                NetworkError(
                    message=f"Network error connecting to {url}: {exc}",
                    url=url,
                )
            )

        raw_text = resp.text
        parsed: Any
        if raw_text.strip():
            try:
                parsed = resp.json()
            except ValueError:
                parsed = {"error": raw_text}
        else:
            parsed = {}

        return Ok(
            HttpResponse(
                status_code=resp.status_code,
                data=parsed,
                text=raw_text,
            )
        )

    def fetch_html(self, url: str, *, timeout: float = 15.0) -> str | None:
        if not url.startswith(("https://", "http://")):
            return None
        try:
            resp = self._client.get(
                url,
                headers={
                    "User-Agent": BROWSER_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.text
            return None
        except httpx.HTTPError:
            return None
