"""Filesystem I/O and VirtualFS materialization for Contraband."""

import json
import os
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from contraband.core.errors import StorageError
from contraband.core.result import Err, Ok, Result
from contraband.core.types import (
    DEFAULT_CONTRABAND_LOGO,
    AddonConfig,
    JsonDict,
    VirtualFS,
)


def resolve_within_directory(
    base_dir: Path,
    relative_path: str | Path | PurePosixPath,
) -> Result[Path, StorageError]:
    """
    Validates that `relative_path` resolves strictly inside `base_dir` to prevent
    directory traversal (`../`) or symlink escape vulnerabilities.
    """
    raw_rel = str(relative_path)
    if "\x00" in raw_rel or raw_rel.startswith(("/", "\\")):
        return Err(
            StorageError(
                message=f"Absolute or null-byte path rejected: {raw_rel!r}",
                path=raw_rel,
            )
        )

    posix_parts = PurePosixPath(raw_rel.replace("\\", "/")).parts
    if ".." in posix_parts:
        return Err(
            StorageError(
                message=f"Parent directory traversal ('..') rejected: {raw_rel!r}",
                path=raw_rel,
            )
        )

    resolved_base = base_dir.resolve()
    resolved_target = (resolved_base / Path(*posix_parts)).resolve()
    if resolved_base != resolved_target and resolved_base not in resolved_target.parents:
        return Err(
            StorageError(
                message=f"Path escapes base directory {resolved_base}: {resolved_target}",
                path=raw_rel,
            )
        )
    return Ok(resolved_target)


def parse_env_text(content: str) -> dict[str, str]:
    """Parses `.env` file content into a dictionary."""
    env: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            env[key] = val
    return env


def load_env_with_fallback(addon_root: Path) -> dict[str, str]:
    """
    Loads `.env` from `addon_root/.env` (falling back to `../stream-weaver/.env` if present),
    overlaid with `os.environ`.
    """
    merged: dict[str, str] = {}
    sibling_env = addon_root.parent / "stream-weaver" / ".env"
    local_env = addon_root / ".env"

    for candidate in (sibling_env, local_env):
        if candidate.exists():
            try:
                merged.update(parse_env_text(candidate.read_text(encoding="utf-8")))
            except OSError:
                continue

    for k, v in os.environ.items():
        if k.startswith(("SECRET_", "ADDON_", "AIO", "DEBRID_")):
            merged[k] = v
    return merged


def _parse_bool_flag(raw: str) -> bool | None:
    cleaned = raw.strip().lower()
    if cleaned in ("1", "true", "yes", "on"):
        return True
    if cleaned in ("0", "false", "no", "off"):
        return False
    return None


def load_addon_config(
    addon_dir: Path,
    env: Mapping[str, str] | None = None,
) -> Result[AddonConfig, StorageError]:
    """
    Loads `AddonConfig` from an optional `addon.toml` file in `addon_dir`,
    overlaid with `ADDON_*` environment variables, falling back cleanly to
    `AddonConfig()` defaults when `addon.toml` is absent.
    """
    env_map = env or {}
    defaults = AddonConfig()
    addon_id = defaults.addon_id
    name = defaults.name
    version = defaults.version
    description = defaults.description
    logo = defaults.logo
    id_prefixes = defaults.id_prefixes
    emit_library_meta = defaults.emit_library_meta

    toml_path = addon_dir / "addon.toml"
    if toml_path.is_file():
        try:
            parsed_toml = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return Err(
                StorageError(
                    message=f"Failed to parse addon.toml at {toml_path}: {exc}",
                    path=str(toml_path),
                )
            )
        raw_section = parsed_toml.get("addon")
        section: Mapping[str, Any] = (
            raw_section if isinstance(raw_section, Mapping) else parsed_toml
        )
        raw_id = section.get("id") or section.get("addon_id")
        if isinstance(raw_id, str) and raw_id.strip():
            addon_id = raw_id.strip()
        if isinstance(raw_name := section.get("name"), str) and raw_name.strip():
            name = raw_name.strip()
        if isinstance(raw_ver := section.get("version"), str) and raw_ver.strip():
            version = raw_ver.strip()
        if isinstance(raw_desc := section.get("description"), str) and raw_desc.strip():
            description = raw_desc.strip()
        if isinstance(raw_logo := section.get("logo"), str) and raw_logo.strip():
            logo = raw_logo.strip()
        if isinstance(raw_prefixes := section.get("id_prefixes"), list):
            cleaned_prefixes = tuple(
                str(p).strip() for p in raw_prefixes if str(p).strip()
            )
            if cleaned_prefixes:
                id_prefixes = cleaned_prefixes
        if isinstance(raw_emit := section.get("emit_library_meta"), bool):
            emit_library_meta = raw_emit

    if env_id := env_map.get("ADDON_ID", "").strip():
        addon_id = env_id
    if env_name := env_map.get("ADDON_NAME", "").strip():
        name = env_name
    if env_ver := env_map.get("ADDON_VERSION", "").strip():
        version = env_ver
    if env_desc := env_map.get("ADDON_DESCRIPTION", "").strip():
        description = env_desc
    if env_logo := env_map.get("ADDON_LOGO_URL", "").strip():
        logo = env_logo
    if env_prefixes := env_map.get("ADDON_ID_PREFIXES", "").strip():
        parsed_prefixes = tuple(p.strip() for p in env_prefixes.split(",") if p.strip())
        if parsed_prefixes:
            id_prefixes = parsed_prefixes
    if (env_emit := _parse_bool_flag(env_map.get("ADDON_EMIT_LIBRARY_META", ""))) is not None:
        emit_library_meta = env_emit

    return Ok(
        AddonConfig(
            addon_id=addon_id,
            name=name,
            version=version,
            description=description,
            logo=logo or DEFAULT_CONTRABAND_LOGO,
            id_prefixes=id_prefixes,
            emit_library_meta=emit_library_meta,
        )
    )


def read_json_file(path: Path) -> Result[JsonDict, StorageError]:
    """Reads and parses a JSON object from `path`."""
    if not path.exists():
        return Err(StorageError(message=f"File not found: {path}", path=str(path)))
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Err(StorageError(message=f"Failed to read JSON from {path}: {exc}", path=str(path)))

    if not isinstance(parsed, dict):
        return Err(StorageError(message=f"Expected JSON object in {path}", path=str(path)))
    return Ok(parsed)


def read_addon_virtual_fs(addon_dir: Path) -> VirtualFS:
    """Reads existing `.json` files in `addon_dir` into an immutable `VirtualFS`."""
    if not addon_dir.exists():
        return VirtualFS(files={})

    resolved_root = addon_dir.resolve()
    files: dict[PurePosixPath, str] = {}
    for p in sorted(resolved_root.rglob("*.json")):
        if not p.is_file():
            continue
        rel = p.relative_to(resolved_root)
        if any(part.startswith(".") or part in ("src", "tests") for part in rel.parts):
            continue
        try:
            files[PurePosixPath(rel.as_posix())] = p.read_text(encoding="utf-8")
        except OSError:
            continue
    return VirtualFS(files=files)


def materialize_virtual_fs(
    addon_dir: Path,
    vfs: VirtualFS,
    *,
    clean_subdirs: tuple[str, ...] = (),
    prune_managed_subdirs: tuple[str, ...] = ("catalog", "meta", "stream"),
) -> Result[int, StorageError]:
    """
    Materializes an immutable `VirtualFS` into `addon_dir` after validating every relative path
    against directory traversal. Also prunes stale `.json` files inside `prune_managed_subdirs`.
    """
    try:
        addon_dir.mkdir(parents=True, exist_ok=True)
        resolved_root = addon_dir.resolve()

        for subdir in clean_subdirs:
            match resolve_within_directory(resolved_root, subdir):
                case Err(sec_err):
                    return Err(StorageError(message=sec_err.message, path=subdir))
                case Ok(target_dir):
                    if target_dir.exists():
                        shutil.rmtree(target_dir)

        expected_resolved_paths: set[Path] = set()
        written_count = 0

        for rel_posix, content in vfs.files.items():
            match resolve_within_directory(resolved_root, rel_posix):
                case Err(sec_err):
                    return Err(StorageError(message=sec_err.message, path=str(rel_posix)))
                case Ok(target_file):
                    expected_resolved_paths.add(target_file)
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    if not target_file.exists() or target_file.read_text(encoding="utf-8") != content:
                        target_file.write_text(content, encoding="utf-8")
                        written_count += 1

        for subdir in prune_managed_subdirs:
            match resolve_within_directory(resolved_root, subdir):
                case Ok(managed_dir) if managed_dir.exists():
                    for existing_json in list(managed_dir.rglob("*.json")):
                        if existing_json.resolve() not in expected_resolved_paths:
                            existing_json.unlink(missing_ok=True)
                    for child_dir in sorted(
                        (d for d in managed_dir.rglob("*") if d.is_dir()),
                        key=lambda p: len(p.parts),
                        reverse=True,
                    ):
                        if not any(child_dir.iterdir()):
                            child_dir.rmdir()
                case _:
                    continue

        return Ok(written_count)
    except OSError as exc:
        return Err(
            StorageError(message=f"Failed to materialize VirtualFS: {exc}", path=str(addon_dir))
        )
