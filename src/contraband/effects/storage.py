"""Filesystem I/O and VirtualFS materialization for Contraband."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from contraband.core.errors import StorageError
from contraband.core.result import Err, Ok, Result
from contraband.core.types import JsonDict, VirtualFS


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
        if k.startswith(("SECRET_", "ADDON_", "AIO")):
            merged[k] = v
    return merged


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
                case _:
                    continue

        return Ok(written_count)
    except OSError as exc:
        return Err(
            StorageError(message=f"Failed to materialize VirtualFS: {exc}", path=str(addon_dir))
        )
