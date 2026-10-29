"""Unit tests for `addon.toml` configuration loading, `--dir` resolution, and `cb init`."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from contraband.cli import _try_optional_debrid_provider, app, resolve_addon_dir
from contraband.core.errors import NetworkError
from contraband.core.result import Err, Ok, Result
from contraband.effects.http import HttpResponse
from contraband.effects.storage import load_addon_config
from contraband.providers.realdebrid import RealDebridProvider, rd_id_to_int

runner = CliRunner()


def test_load_addon_config_defaults_and_toml_with_env_overrides(tmp_path: Path) -> None:
    # 1. Defaults when addon.toml is absent
    res_default = load_addon_config(tmp_path, {})
    assert isinstance(res_default, Ok)
    assert res_default.value.addon_id == "dev.darshanas.contraband"
    assert res_default.value.name == "Contraband"
    assert res_default.value.emit_library_meta is False

    # 2. addon.toml values
    (tmp_path / "addon.toml").write_text(
        '[addon]\n'
        'id = "org.indie.lectures"\n'
        'name = "Indie Lectures"\n'
        'version = "2.1.0"\n'
        'description = "Open university archives"\n'
        'logo = "https://example.org/logo.png"\n'
        'id_prefixes = ["sw_", "ia_"]\n'
        "emit_library_meta = true\n",
        encoding="utf-8",
    )
    res_toml = load_addon_config(tmp_path, {})
    assert isinstance(res_toml, Ok)
    assert res_toml.value.addon_id == "org.indie.lectures"
    assert res_toml.value.name == "Indie Lectures"
    assert res_toml.value.version == "2.1.0"
    assert res_toml.value.description == "Open university archives"
    assert res_toml.value.logo == "https://example.org/logo.png"
    assert res_toml.value.id_prefixes == ("sw_", "ia_")
    assert res_toml.value.emit_library_meta is True

    # 3. Environment variable overrides take precedence
    res_env = load_addon_config(
        tmp_path,
        {
            "ADDON_NAME": "Custom Override Name",
            "ADDON_EMIT_LIBRARY_META": "false",
        },
    )
    assert isinstance(res_env, Ok)
    assert res_env.value.addon_id == "org.indie.lectures"
    assert res_env.value.name == "Custom Override Name"
    assert res_env.value.emit_library_meta is False


def test_load_addon_config_returns_err_on_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "addon.toml").write_text("[addon\nid = broken", encoding="utf-8")
    res = load_addon_config(tmp_path, {})
    assert isinstance(res, Err)
    assert "Failed to parse addon.toml" in res.error.message


def test_resolve_addon_dir_priority(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    env_dir = tmp_path / "from_env"
    cwd_init = tmp_path / "cwd_init"
    cwd_empty = tmp_path / "cwd_empty"
    pkg_root = tmp_path / "pkg_root"
    for d in (explicit, env_dir, cwd_init, cwd_empty, pkg_root):
        d.mkdir()
    (cwd_init / "addon.toml").write_text('[addon]\nid = "org.test"\n', encoding="utf-8")

    # 1. Explicit --dir wins over everything
    assert (
        resolve_addon_dir(
            explicit,
            cwd=cwd_init,
            env={"ADDON_REPO_DIR": str(env_dir)},
            package_root=pkg_root,
        )
        == explicit.resolve()
    )

    # 2. ADDON_REPO_DIR wins over cwd and package_root
    assert (
        resolve_addon_dir(
            None,
            cwd=cwd_init,
            env={"ADDON_REPO_DIR": str(env_dir)},
            package_root=pkg_root,
        )
        == env_dir.resolve()
    )

    # 3. Initialized cwd (with addon.toml or catalog_index.json) wins over package_root
    assert (
        resolve_addon_dir(
            None,
            cwd=cwd_init,
            env={},
            package_root=pkg_root,
        )
        == cwd_init.resolve()
    )

    # 4. Uninitialized cwd falls back to package_root
    assert (
        resolve_addon_dir(
            None,
            cwd=cwd_empty,
            env={},
            package_root=pkg_root,
        )
        == pkg_root.resolve()
    )


def test_cb_init_scaffolds_workspace_and_preserves_env(tmp_path: Path) -> None:
    ws = tmp_path / "my_addon"
    ws.mkdir()
    env_file = ws / ".env"
    env_file.write_text("SECRET_TORBOX_API_KEY=preserve-this-token\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "init",
            str(ws),
            "--id",
            "org.example.archive",
            "--name",
            "Example Archive",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (ws / "addon.toml").is_file()
    assert (ws / ".env.example").is_file()
    assert (ws / ".gitignore").is_file()
    assert (ws / "manifest.json").is_file()
    assert (ws / "catalog_index.json").is_file()
    assert env_file.read_text(encoding="utf-8") == "SECRET_TORBOX_API_KEY=preserve-this-token\n"

    manifest = json.loads((ws / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "org.example.archive"
    assert manifest["name"] == "Example Archive"

    # Verify `cb doctor --offline --dir <ws>` succeeds cleanly on the newly initialized workspace
    doc_res = runner.invoke(app, ["doctor", "--offline", "--dir", str(ws)])
    assert doc_res.exit_code == 0, doc_res.output


class _FakeRdHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

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
        _ = headers, json_body, raw_body, timeout
        self.calls.append((method, url))
        if "/torrents/instantAvailability/" in url:
            return Ok(HttpResponse(status_code=403, data={"error_code": 37}, text=""))
        if url.endswith("/torrents?limit=250"):
            return Ok(
                HttpResponse(
                    status_code=200,
                    data=[
                        {
                            "id": "RDABC123XYZ",
                            "filename": "Systems Seminar",
                            "hash": "b" * 40,
                            "bytes": 4_000_000,
                            "progress": 100,
                            "status": "downloaded",
                        }
                    ],
                    text="",
                )
            )
        if url.endswith("/torrents/addMagnet"):
            return Ok(HttpResponse(status_code=201, data={"id": "RDABC123XYZ"}, text=""))
        if url.endswith("/torrents/selectFiles/RDABC123XYZ"):
            return Ok(HttpResponse(status_code=204, data={}, text=""))
        if url.endswith("/torrents/info/RDABC123XYZ"):
            return Ok(
                HttpResponse(
                    status_code=200,
                    data={
                        "id": "RDABC123XYZ",
                        "status": "downloaded",
                        "files": [
                            {"id": 1, "path": "/00_readme.txt", "bytes": 512, "selected": 1},
                            {"id": 2, "path": "/01 - Clocks.mp4", "bytes": 2_000_000, "selected": 1},
                            {"id": 3, "path": "/02 - Paxos.mp4", "bytes": 2_000_000, "selected": 1},
                        ],
                    },
                    text="",
                )
            )
        return Err(NetworkError(message=f"Unexpected URL {url}", status_code=404, url=url))

    def fetch_html(self, url: str, *, timeout: float = 15.0) -> str | None:
        _ = url, timeout
        return None


def test_realdebrid_provider_library_file_index_and_graceful_cache_check() -> None:
    fake_http = _FakeRdHttpClient()
    rd = RealDebridProvider(api_key="rd-test-token", http_client=fake_http)

    # 1. Disabled instantAvailability endpoint degrades cleanly to uncached
    cache_res = rd.check_cached_bulk(["b" * 40])
    assert isinstance(cache_res, Ok)
    assert cache_res.value["b" * 40].is_cached is False

    # 2. Library listing normalizes 1-based RD file IDs into 0-based Stremio fileIdx
    lib_res = rd.list_library()
    assert isinstance(lib_res, Ok)
    assert len(lib_res.value) == 1
    item = lib_res.value[0]
    assert item.id == rd_id_to_int("RDABC123XYZ")
    assert item.download_finished is True
    assert [(f.index, f.short_name) for f in item.files] == [
        (0, "00_readme.txt"),
        (1, "01 - Clocks.mp4"),
        (2, "02 - Paxos.mp4"),
    ]

    # 3. add_magnet selects all files and detects cached download state
    add_res = rd.add_magnet(f"magnet:?xt=urn:btih:{'b' * 40}")
    assert isinstance(add_res, Ok)
    assert add_res.value.was_cached is True
    assert add_res.value.torrent_id == rd_id_to_int("RDABC123XYZ")

    # 4. Auto-detection selects RealDebridProvider when SECRET_RD_API_KEY is set
    auto_provider = _try_optional_debrid_provider({"SECRET_RD_API_KEY": "rd-key"}, fake_http)
    assert isinstance(auto_provider, RealDebridProvider)


def test_cb_add_offline_weaves_multi_file_torrent_without_debrid_keys(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("SECRET_TORBOX_API_KEY", raising=False)
    monkeypatch.delenv("SECRET_RD_API_KEY", raising=False)

    ws = tmp_path / "offline_addon"
    init_res = runner.invoke(app, ["init", str(ws), "--id", "org.offline.demo", "--name", "Offline Demo"])
    assert init_res.exit_code == 0, init_res.output

    # Build a valid multi-file bencoded .torrent (readme at idx 0, Ep 1 at idx 1, Ep 2 at idx 2)
    info_bytes = (
        b"d5:filesl"
        b"d6:lengthi128e4:pathl10:readme.txtee"
        b"d6:lengthi1048576e4:pathl14:01 - Intro.mp4ee"
        b"d6:lengthi2097152e4:pathl15:02 - Finale.mp4ee"
        b"e4:name13:Demo Lectures12:piece lengthi16384e6:pieces20:01234567890123456789e"
    )
    expected_hash = hashlib.sha1(info_bytes).hexdigest().lower()
    torrent_path = tmp_path / "demo_lectures.torrent"
    torrent_path.write_bytes(b"d8:announce18:http://tracker/ann4:info" + info_bytes + b"e")

    sidecar = tmp_path / "demo.toml"
    sidecar.write_text(
        '[metadata]\n'
        'id = "sw_custom_demo"\n'
        'title = "Demo Lectures"\n'
        'collection = "Open Seminars"\n'
        "\n"
        "[[episodes]]\n"
        "number = 1\n"
        'title = "Introduction"\n'
        "\n"
        "[[episodes]]\n"
        "number = 2\n"
        'title = "Finale"\n',
        encoding="utf-8",
    )

    add_res = runner.invoke(
        app,
        [
            "add",
            str(torrent_path),
            "--offline",
            "--meta-file",
            str(sidecar),
            "--dir",
            str(ws),
        ],
    )
    assert add_res.exit_code == 0, add_res.output

    ep1_stream = json.loads(
        (ws / "stream" / "series" / "sw_custom_demo_s1e1.json").read_text(encoding="utf-8")
    )
    ep2_stream = json.loads(
        (ws / "stream" / "series" / "sw_custom_demo_s1e2.json").read_text(encoding="utf-8")
    )
    assert ep1_stream["streams"][0]["infoHash"] == expected_hash
    assert ep1_stream["streams"][0]["fileIdx"] == 1
    assert ep2_stream["streams"][0]["infoHash"] == expected_hash
    assert ep2_stream["streams"][0]["fileIdx"] == 2

