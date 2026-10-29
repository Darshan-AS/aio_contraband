"""Security and protocol conformance unit tests for `cb serve` (`contraband.effects.server`)."""

import http.client
import json
import socket
from pathlib import Path

from contraband.effects.server import (
    SlidingWindowRateLimiter,
    create_addon_http_server,
    resolve_cors_allow_origin,
    resolve_stremio_static_file,
)


def test_resolve_cors_allow_origin_blocks_spoofed_and_untrusted_origins() -> None:
    assert resolve_cors_allow_origin("https://web.stremio.com") == "https://web.stremio.com"
    assert resolve_cors_allow_origin("https://app.strem.io") == "https://app.strem.io"
    assert resolve_cors_allow_origin("http://localhost:3000") == "http://localhost:3000"
    assert resolve_cors_allow_origin("http://127.0.0.1:8787") == "http://127.0.0.1:8787"
    assert (
        resolve_cors_allow_origin(
            "https://streams.example.org",
            extra_origins=frozenset({"https://streams.example.org"}),
        )
        == "https://streams.example.org"
    )

    # Reject subdomain spoofing, untrusted origins, and CRLF header injection
    assert resolve_cors_allow_origin("http://localhost.attacker.example") is None
    assert resolve_cors_allow_origin("https://evil.example") is None
    assert resolve_cors_allow_origin("http://127.0.0.1:8080/extra-path") is None
    assert resolve_cors_allow_origin("https://web.stremio.com\r\nX-Injected: 1") is None


def test_resolve_stremio_static_file_blocks_sensitive_files_traversal_and_symlinks(
    tmp_path: Path,
) -> None:
    addon_dir = tmp_path / "addon"
    (addon_dir / "catalog" / "series").mkdir(parents=True)
    (addon_dir / "stream" / "series").mkdir(parents=True)
    (addon_dir / "manifest.json").write_text('{"id":"org.test"}\n', encoding="utf-8")
    (addon_dir / "catalog" / "series" / "open_seminars.json").write_text('{"metas":[]}\n', encoding="utf-8")
    (addon_dir / "stream" / "series" / "sw_demo_s1e1.json").write_text('{"streams":[]}\n', encoding="utf-8")

    # Non-public workspace files that MUST NEVER be served
    (addon_dir / ".env").write_text("SECRET_TORBOX_API_KEY=top-secret\n", encoding="utf-8")
    (addon_dir / "addon.toml").write_text('[addon]\nid="org.test"\n', encoding="utf-8")
    (addon_dir / "catalog_index.json").write_text('{"version":2}\n', encoding="utf-8")

    outside_secret = tmp_path / "outside.json"
    outside_secret.write_text('{"secret":true}\n', encoding="utf-8")
    (addon_dir / "catalog" / "series" / "escape.json").symlink_to(outside_secret)

    # Allowed Stremio routes (including Stremio `id:season:episode.json` -> `_sXeY.json` mapping)
    assert resolve_stremio_static_file(addon_dir, "/manifest.json") == (
        addon_dir / "manifest.json"
    ).resolve()
    assert resolve_stremio_static_file(addon_dir, "/catalog/series/open_seminars.json") == (
        addon_dir / "catalog" / "series" / "open_seminars.json"
    ).resolve()
    assert resolve_stremio_static_file(addon_dir, "/stream/series/sw_demo:1:1.json") == (
        addon_dir / "stream" / "series" / "sw_demo_s1e1.json"
    ).resolve()

    # Blocked routes
    for forbidden in (
        "/.env",
        "/addon.toml",
        "/catalog_index.json",
        "/../outside.json",
        "/catalog/%2e%2e/catalog_index.json",
        "/catalog/series/../../.env",
        "/catalog/series/escape.json",
        "/src/contraband/cli.py",
    ):
        assert resolve_stremio_static_file(addon_dir, forbidden) is None, forbidden


def test_sliding_window_rate_limiter() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=10.0)
    assert limiter.allow("127.0.0.1", now=100.0) is True
    assert limiter.allow("127.0.0.1", now=101.0) is True
    assert limiter.allow("127.0.0.1", now=102.0) is False
    assert limiter.allow("127.0.0.1", now=111.0) is True


def _send_raw_http_request(
    server: object,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Exercises the full `BaseHTTPRequestHandler` HTTP/1.1 wire parser over an in-process socketpair."""
    from contraband.effects.server import StremioAddonHttpServer

    assert isinstance(server, StremioAddonHttpServer)
    client_sock, server_sock = socket.socketpair()
    try:
        hdr_lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1:8787", "Connection: close"]
        for k, v in (headers or {}).items():
            hdr_lines.append(f"{k}: {v}")
        raw_req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8")
        client_sock.sendall(raw_req)
        server.finish_request(server_sock, ("127.0.0.1", 54321))
        server_sock.close()

        http_resp = http.client.HTTPResponse(client_sock)
        http_resp.begin()
        body = http_resp.read() if method != "HEAD" else b""
        resp_headers = {k.lower(): v for k, v in http_resp.getheaders()}
        return http_resp.status, resp_headers, body
    finally:
        client_sock.close()
        server_sock.close()


def test_http_server_loopback_headers_methods_and_rate_limit(tmp_path: Path) -> None:
    addon_dir = tmp_path / "served_addon"
    (addon_dir / "stream" / "series").mkdir(parents=True)
    (addon_dir / "manifest.json").write_text(
        json.dumps({"id": "org.loopback.test", "name": "Loopback Addon"}),
        encoding="utf-8",
    )
    (addon_dir / "stream" / "series" / "sw_item_s1e2.json").write_text(
        json.dumps({"streams": [{"infoHash": "a" * 40, "fileIdx": 1}]}),
        encoding="utf-8",
    )
    (addon_dir / ".env").write_text("SECRET_TORBOX_API_KEY=never-serve\n", encoding="utf-8")
    (addon_dir / "catalog_index.json").write_text('{"version":2}\n', encoding="utf-8")

    # Bind strictly to 127.0.0.1 on an ephemeral port (never 0.0.0.0)
    server = create_addon_http_server(
        addon_dir,
        host="127.0.0.1",
        port=0,
        rate_limit_per_minute=6,
    )
    try:
        assert server.server_address[0] == "127.0.0.1"

        # 1. GET /manifest.json with trusted Stremio Origin
        status, hdrs, raw_body = _send_raw_http_request(
            server,
            "GET",
            "/manifest.json",
            headers={"Origin": "https://web.stremio.com"},
        )
        body = json.loads(raw_body.decode("utf-8"))
        assert status == 200
        assert body["id"] == "org.loopback.test"
        assert hdrs.get("access-control-allow-origin") == "https://web.stremio.com"
        assert hdrs.get("x-content-type-options") == "nosniff"
        assert hdrs.get("x-frame-options") == "DENY"
        assert "default-src 'none'" in hdrs.get("content-security-policy", "")
        assert "camera=()" in hdrs.get("permissions-policy", "")
        assert hdrs.get("cache-control") == "no-store"

        # 2. Stremio colon episode route `/stream/series/sw_item:1:2.json` + untrusted Origin
        status, hdrs, raw_body = _send_raw_http_request(
            server,
            "GET",
            "/stream/series/sw_item:1:2.json",
            headers={"Origin": "https://attacker.example"},
        )
        s_body = json.loads(raw_body.decode("utf-8"))
        assert status == 200
        assert s_body["streams"][0]["fileIdx"] == 1
        assert "access-control-allow-origin" not in hdrs

        # 3. Sensitive files return 404
        for sec_path in ("/.env", "/catalog_index.json", "/%2e%2e/outside"):
            status, _, _ = _send_raw_http_request(server, "GET", sec_path)
            assert status == 404

        # 4. Non-allowlisted HTTP method (POST) returns 405 Method Not Allowed
        status, hdrs, _ = _send_raw_http_request(server, "POST", "/manifest.json")
        assert status == 405
        assert "GET" in hdrs.get("allow", "")

        # 5. Exceeding rate_limit_per_minute (5 consumed above; 6th succeeds, 7th returns 429)
        status, _, _ = _send_raw_http_request(server, "HEAD", "/manifest.json")
        assert status == 200

        status, hdrs, _ = _send_raw_http_request(server, "GET", "/manifest.json")
        assert status == 429
        assert hdrs.get("retry-after") == "60"
    finally:
        server.server_close()
