"""Unit tests for `addon.toml` configuration loading, `--dir` resolution, and `cb init`."""

import json
from pathlib import Path

from typer.testing import CliRunner

from contraband.cli import app, resolve_addon_dir
from contraband.core.result import Err, Ok
from contraband.effects.storage import load_addon_config

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
