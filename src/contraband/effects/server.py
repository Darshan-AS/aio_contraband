"""Security-hardened local Stremio v3 static addon HTTP server (`cb serve`)."""

import collections
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

from contraband.core.result import Err, Ok
from contraband.effects.storage import resolve_within_directory

DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8787

DEFAULT_TRUSTED_STREMIO_ORIGINS: frozenset[str] = frozenset(
    {
        "https://web.stremio.com",
        "https://app.strem.io",
    }
)

_ALLOWED_SUBDIRS: frozenset[str] = frozenset({"catalog", "meta", "stream"})
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._:=+-]{1,180}$")
_COLON_SERIES_RE = re.compile(r"^([A-Za-z0-9._-]+):(\d+):(\d+)\.json$")
_MAX_SERVED_JSON_BYTES = 10 * 1024 * 1024  # 10 MiB safety ceiling


@dataclass(frozen=True, slots=True)
class AddonServerConfig:
    """Immutable configuration for the local Contraband Stremio HTTP server."""

    addon_dir: Path
    host: str = DEFAULT_SERVE_HOST
    port: int = DEFAULT_SERVE_PORT
    extra_cors_origins: frozenset[str] = frozenset()
    # TODO(security): Wildcard CORS (`allow_any_origin=True`) is disabled by default and
    # only available via explicit user opt-in for native TV WebViews sending `Origin: null`.
    allow_any_origin: bool = False
    rate_limit_per_minute: int = 300
    # TODO(security): Stremio v3 addon endpoints (`/manifest.json`, `/catalog/*`, `/meta/*`,
    # `/stream/*`) are unauthenticated read-only JSON routes by protocol specification; bind
    # to `127.0.0.1` by default and add an optional path-prefix bearer token before exposing
    # on untrusted networks.


class SlidingWindowRateLimiter:
    """Thread-safe in-memory per-IP sliding-window request rate limiter."""

    def __init__(self, max_requests: int = 300, window_seconds: float = 60.0) -> None:
        self._max_requests = max(1, max_requests)
        self._window_seconds = max(1.0, window_seconds)
        self._hits: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client_ip: str, *, now: float | None = None) -> bool:
        ts = time.monotonic() if now is None else now
        cutoff = ts - self._window_seconds
        with self._lock:
            bucket = self._hits.get(client_ip)
            if bucket is None:
                bucket = collections.deque()
                self._hits[client_ip] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                return False
            bucket.append(ts)
            return True


def resolve_cors_allow_origin(
    origin_header: str | None,
    *,
    extra_origins: frozenset[str] = frozenset(),
    allow_any_origin: bool = False,
) -> str | None:
    """
    Validates an incoming `Origin` header against a strict allow-list of Stremio web
    clients and local loopback origins (`localhost` / `127.0.0.1`), preventing subdomain
    spoofing such as `http://localhost.attacker.example`.
    """
    if allow_any_origin:
        return "*"
    if not origin_header:
        return None
    cleaned = origin_header.strip()
    if not cleaned or "\r" in cleaned or "\n" in cleaned:
        return None
    if cleaned in DEFAULT_TRUSTED_STREMIO_ORIGINS or cleaned in extra_origins:
        return cleaned

    parsed = urllib.parse.urlsplit(cleaned)
    if (
        parsed.scheme in ("http", "https")
        and parsed.hostname in ("localhost", "127.0.0.1")
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    ):
        return cleaned
    return None


def resolve_stremio_static_file(addon_dir: Path, raw_request_path: str) -> Path | None:
    """
    Strictly resolves a Stremio v3 request path to an allow-listed static `.json` file
    inside `addon_dir`, blocking `.env`, `catalog_index.json`, `addon.toml`, `.git/`,
    symlinks, and directory traversal (`../` or URL-encoded variants).
    """
    raw_url_path = urllib.parse.urlsplit(raw_request_path).path
    unquoted = urllib.parse.unquote(raw_url_path)
    if "\x00" in unquoted or "\\" in unquoted:
        return None

    stripped = unquoted.strip("/")
    if not stripped:
        return None

    parts = stripped.split("/")
    if any(not p or p.startswith(".") or p == ".." or not _SAFE_SEGMENT_RE.match(p) for p in parts):
        return None

    candidate_rel_paths: list[PurePosixPath] = []
    if len(parts) == 1 and parts[0] == "manifest.json":
        candidate_rel_paths.append(PurePosixPath("manifest.json"))
    elif 2 <= len(parts) <= 4 and parts[0] in _ALLOWED_SUBDIRS and parts[-1].endswith(".json"):
        candidate_rel_paths.append(PurePosixPath(*parts))
        if parts[0] == "stream" and len(parts) == 3 and parts[1] == "series":
            colon_m = _COLON_SERIES_RE.match(parts[2])
            if colon_m:
                sid, season_str, ep_str = colon_m.group(1), colon_m.group(2), colon_m.group(3)
                candidate_rel_paths.append(
                    PurePosixPath("stream", "series", f"{sid}_s{season_str}e{ep_str}.json")
                )
    else:
        return None

    resolved_root = addon_dir.resolve()
    for rel_posix in candidate_rel_paths:
        raw_unvalidated = resolved_root.joinpath(*rel_posix.parts)
        if raw_unvalidated.is_symlink():
            continue
        match resolve_within_directory(resolved_root, rel_posix):
            case Err():
                continue
            case Ok(resolved_target):
                try:
                    if (
                        resolved_target.is_file()
                        and not resolved_target.is_symlink()
                        and resolved_target.stat().st_size <= _MAX_SERVED_JSON_BYTES
                    ):
                        return resolved_target
                except OSError:
                    continue
    return None


class StremioAddonHttpServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying immutable `AddonServerConfig` and rate-limiter state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: AddonServerConfig) -> None:
        self.addon_config = config
        self.rate_limiter = SlidingWindowRateLimiter(
            max_requests=config.rate_limit_per_minute,
            window_seconds=60.0,
        )
        super().__init__((config.host, config.port), _StremioStaticRequestHandler)


class _StremioStaticRequestHandler(BaseHTTPRequestHandler):
    """Read-only HTTP request handler enforcing method/path allow-lists and security headers."""

    server: StremioAddonHttpServer

    def version_string(self) -> str:
        return "Contraband"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default noisy stderr logging (prevents leaking headers/query params to logs).
        _ = format, args

    def _write_security_and_cors_headers(self) -> None:
        cfg = self.server.addon_config
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store")

        allowed_origin = resolve_cors_allow_origin(
            self.headers.get("Origin"),
            extra_origins=cfg.extra_cors_origins,
            allow_any_origin=cfg.allow_any_origin,
        )
        if allowed_origin is not None:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
            if allowed_origin != "*":
                self.send_header("Vary", "Origin")

    def _send_json_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        include_body: bool = True,
        extra_headers: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for hk, hv in extra_headers:
            self.send_header(hk, hv)
        self._write_security_and_cors_headers()
        self.end_headers()
        if include_body:
            self.wfile.write(payload)

    def _check_rate_limit(self) -> bool:
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        if not self.server.rate_limiter.allow(client_ip):
            err_bytes = json.dumps({"error": "Too Many Requests"}).encode("utf-8")
            self._send_json_bytes(
                HTTPStatus.TOO_MANY_REQUESTS,
                err_bytes,
                extra_headers=(("Retry-After", "60"),),
            )
            return False
        return True

    def _serve_static_route(self, *, include_body: bool) -> None:
        if not self._check_rate_limit():
            return
        target = resolve_stremio_static_file(self.server.addon_config.addon_dir, self.path)
        if target is None:
            not_found = json.dumps({"error": "Not Found"}).encode("utf-8")
            self._send_json_bytes(
                HTTPStatus.NOT_FOUND,
                not_found,
                include_body=include_body,
            )
            return
        try:
            body = target.read_bytes()
        except OSError:
            not_found = json.dumps({"error": "Not Found"}).encode("utf-8")
            self._send_json_bytes(
                HTTPStatus.NOT_FOUND,
                not_found,
                include_body=include_body,
            )
            return
        self._send_json_bytes(HTTPStatus.OK, body, include_body=include_body)

    def _reject_method_not_allowed(self) -> None:
        err_bytes = json.dumps({"error": "Method Not Allowed"}).encode("utf-8")
        self._send_json_bytes(
            HTTPStatus.METHOD_NOT_ALLOWED,
            err_bytes,
            extra_headers=(("Allow", "GET, HEAD, OPTIONS"),),
        )

    def do_GET(self) -> None:
        self._serve_static_route(include_body=True)

    def do_HEAD(self) -> None:
        self._serve_static_route(include_body=False)

    def do_OPTIONS(self) -> None:
        if not self._check_rate_limit():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self._write_security_and_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        self._reject_method_not_allowed()

    def do_PUT(self) -> None:
        self._reject_method_not_allowed()

    def do_DELETE(self) -> None:
        self._reject_method_not_allowed()

    def do_PATCH(self) -> None:
        self._reject_method_not_allowed()

    def do_TRACE(self) -> None:
        self._reject_method_not_allowed()


def create_addon_http_server(
    addon_dir: Path,
    *,
    host: str = DEFAULT_SERVE_HOST,
    port: int = DEFAULT_SERVE_PORT,
    extra_cors_origins: Sequence[str] = (),
    allow_any_origin: bool = False,
    rate_limit_per_minute: int = 300,
) -> StremioAddonHttpServer:
    """Creates a bound `StremioAddonHttpServer` ready for `serve_forever()`."""
    cleaned_origins = frozenset(o.strip() for o in extra_cors_origins if o.strip())
    cfg = AddonServerConfig(
        addon_dir=addon_dir.resolve(),
        host=host,
        port=port,
        extra_cors_origins=cleaned_origins,
        allow_any_origin=allow_any_origin,
        rate_limit_per_minute=rate_limit_per_minute,
    )
    return StremioAddonHttpServer(cfg)
