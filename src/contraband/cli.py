"""Contraband CLI (`cb`) — Static Stremio Addon Compiler & TorBox Cloud Ingestor."""

import concurrent.futures
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from contraband.core.bencode import (
    extract_magnets_from_text,
    parse_magnet_uri,
    parse_torrent_bytes,
)
from contraband.core.compiler import (
    DEFAULT_ADDON_CONFIG,
    audit_contraband_addon,
    compile_contraband_addon,
    remove_media_items,
    seed_index_from_existing_files,
    upsert_media_item,
)
from contraband.core.errors import ContrabandError
from contraband.core.matcher import format_bytes, match_files_to_episodes
from contraband.core.result import Err, Ok, Result
from contraband.core.types import (
    DEFAULT_CONTRABAND_LOGO,
    AddonConfig,
    BoundMediaItem,
    CatalogIndex,
    DebridCacheStatus,
    DebridQueuedItem,
    DebridTorrentItem,
    EpisodeFileMatch,
    MediaItemMetadata,
    MediaType,
    ParsedTorrent,
    TorrentFileEntry,
    VirtualFS,
)
from contraband.effects.git import publish_git_repo
from contraband.effects.http import HttpClientProtocol, HttpxClient
from contraband.effects.storage import (
    load_addon_config,
    load_env_with_fallback,
    materialize_virtual_fs,
    read_addon_virtual_fs,
    read_json_file,
)
from contraband.providers.metadata import resolve_media_metadata
from contraband.providers.protocols import (
    DebridProviderProtocol,
    HtmlFetcher,
    MetadataProviderProtocol,
)
from contraband.providers.torbox import MAX_ESSENTIAL_BYTES, TorBoxProvider

app = typer.Typer(
    name="cb",
    help="Contraband — Pure Functional Static Stremio Addon Compiler & TorBox Cloud Ingestor.",
    no_args_is_help=True,
)

console = Console(markup=False, highlight=False)
err_console = Console(stderr=True, markup=False, highlight=False)


@dataclass(frozen=True, slots=True)
class WeaveMediaOutcome:
    """Summary of weaving a media item into the Contraband static addon."""

    metadata: MediaItemMetadata
    episode_matches: tuple[EpisodeFileMatch, ...]
    files_written: int


_SECRET_PLACEHOLDER_RE = re.compile(r"\{\{(SECRET_[A-Z0-9_]+)\}\}")

AddonDirOption = Annotated[
    Path | None,
    typer.Option(
        "--dir",
        "-C",
        help="Addon workspace directory (defaults to ADDON_REPO_DIR, current directory if initialized, or package root).",
    ),
]

_ACTIVE_ADDON_DIR: Path | None = None


@app.callback()
def _main_callback(addon_dir: AddonDirOption = None) -> None:
    """Configure global CLI options for Contraband."""
    global _ACTIVE_ADDON_DIR
    _ACTIVE_ADDON_DIR = addon_dir


@dataclass(frozen=True, slots=True)
class _PreparedAddTarget:
    """Pre-parsed CLI target ready for bulk cache lookup and Debrid ingestion."""

    info_hash: str
    magnet_uri: str
    display_title: str
    dn: str | None = None
    torrent_path: Path | None = None
    torrent_bytes: bytes | None = None
    parsed_torrent: ParsedTorrent | None = None


def resolve_addon_dir(
    explicit_dir: Path | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    package_root: Path | None = None,
) -> Path:
    """
    Resolves the active Contraband static addon directory with priority:
    1. Explicit `--dir` / `-C` CLI flag (`explicit_dir` or global callback)
    2. `ADDON_REPO_DIR` environment variable
    3. `cwd` if it contains `addon.toml` or `catalog_index.json`
    4. Fallback to `package_root` (`Path(__file__).resolve().parents[2]`)
    """
    chosen = explicit_dir if explicit_dir is not None else _ACTIVE_ADDON_DIR
    if chosen is not None:
        return chosen.expanduser().resolve()

    env_lookup = env if env is not None else os.environ
    if env_dir := env_lookup.get("ADDON_REPO_DIR", "").strip():
        return Path(env_dir).expanduser().resolve()

    effective_cwd = (cwd if cwd is not None else Path.cwd()).resolve()
    if (effective_cwd / "addon.toml").is_file() or (
        effective_cwd / "catalog_index.json"
    ).is_file():
        return effective_cwd

    fallback_root = (
        package_root.resolve()
        if package_root is not None
        else Path(__file__).resolve().parents[2]
    )
    return fallback_root


def _resolve_context(
    explicit_dir: Path | None = None,
) -> tuple[Path, dict[str, str], AddonConfig]:
    addon_dir = resolve_addon_dir(explicit_dir)
    env = load_env_with_fallback(addon_dir)
    if (
        explicit_dir is None
        and _ACTIVE_ADDON_DIR is None
        and not os.environ.get("ADDON_REPO_DIR", "").strip()
        and (override_dir := env.get("ADDON_REPO_DIR", "").strip())
    ):
        addon_dir = Path(override_dir).expanduser().resolve()
        env = load_env_with_fallback(addon_dir)

    match load_addon_config(addon_dir, env):
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)
        case Ok(addon_cfg):
            return addon_dir, env, addon_cfg


def _parse_media_type_option(raw_type: str | None) -> MediaType | None:
    if not raw_type or raw_type.strip().lower() == "auto":
        return None
    cleaned = raw_type.strip().lower()
    if cleaned in ("movie", "series"):
        return "movie" if cleaned == "movie" else "series"
    err_console.print(f"❌ Invalid --type '{raw_type}'. Choose from: auto, series, movie")
    raise typer.Exit(code=1)


def _require_torbox_provider(
    env: dict[str, str],
    http_client: HttpxClient,
    *,
    max_active: int = 3,
) -> TorBoxProvider:
    api_key = env.get("SECRET_TORBOX_API_KEY", "")
    if not api_key:
        err_console.print("❌ Error: SECRET_TORBOX_API_KEY is missing in .env")
        raise typer.Exit(code=1)
    return TorBoxProvider(api_key=api_key, http_client=http_client, max_active=max_active)


def _fetch_library_and_queue_parallel(
    debrid: DebridProviderProtocol,
) -> tuple[Result[tuple[DebridTorrentItem, ...]], Result[tuple[DebridQueuedItem, ...]]]:
    """Concurrently fetches the TorBox cloud library and queued items."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        lib_fut = pool.submit(debrid.list_library)
        q_fut = pool.submit(debrid.list_queued)
        return lib_fut.result(), q_fut.result()


def resolve_bound_media_item(
    *,
    display_title: str,
    info_hash: str,
    torrent_id: int,
    files: Sequence[TorrentFileEntry],
    fetch_html: HtmlFetcher,
    parsers: Sequence[MetadataProviderProtocol] | None = None,
    explicit_url: str | None = None,
    explicit_slug: str | None = None,
    collection_override: str | None = None,
    media_type_override: MediaType | None = None,
    allow_fallback: bool = False,
) -> BoundMediaItem | None:
    """Purely resolves metadata, applies overrides, and binds episode file indices."""
    effective_fallback = bool(allow_fallback or collection_override or media_type_override)
    meta = resolve_media_metadata(
        display_title=display_title,
        files=files,
        fetch_html=fetch_html,
        parsers=parsers,
        torrent_id=torrent_id,
        explicit_url=explicit_url,
        explicit_slug=explicit_slug,
        allow_fallback=effective_fallback,
    )
    if meta is None:
        return None

    if collection_override:
        meta = meta.with_collection(collection_override)
    if media_type_override:
        meta = meta.with_media_type(media_type_override)

    ep_matches = match_files_to_episodes(files, meta.episodes)
    return BoundMediaItem(
        metadata=meta,
        info_hash=info_hash.lower(),
        torrent_id=torrent_id,
        episode_matches=ep_matches,
    )


def load_catalog_index(addon_dir: Path, *, clean: bool = False) -> CatalogIndex:
    """Loads the existing `CatalogIndex` from `addon_dir` (or returns an empty index if `clean`)."""
    existing_vfs = read_addon_virtual_fs(addon_dir) if not clean else VirtualFS()
    return seed_index_from_existing_files(existing_vfs.files)


def commit_catalog_index(
    addon_dir: Path,
    index: CatalogIndex,
    *,
    addon_config: AddonConfig = DEFAULT_ADDON_CONFIG,
    clean: bool = False,
) -> Result[int]:
    """Compiles `index` into a `VirtualFS` and materializes it to `addon_dir` in a single pass."""
    compiled_vfs = compile_contraband_addon(index, addon_config=addon_config)
    clean_subdirs = ("catalog", "meta", "stream") if clean else ()
    return materialize_virtual_fs(addon_dir, compiled_vfs, clean_subdirs=clean_subdirs)


def weave_media_into_addon(
    *,
    display_title: str,
    info_hash: str,
    torrent_id: int,
    files: Sequence[TorrentFileEntry],
    addon_dir: Path,
    fetch_html: HtmlFetcher,
    parsers: Sequence[MetadataProviderProtocol] | None = None,
    explicit_url: str | None = None,
    explicit_slug: str | None = None,
    collection_override: str | None = None,
    media_type_override: MediaType | None = None,
    allow_fallback: bool = False,
    clean_before_build: bool = False,
    addon_config: AddonConfig = DEFAULT_ADDON_CONFIG,
) -> Result[WeaveMediaOutcome | None]:
    """Resolves metadata, matches episodes/files, updates `CatalogIndex`, and materializes `VirtualFS`."""
    bound = resolve_bound_media_item(
        display_title=display_title,
        info_hash=info_hash,
        torrent_id=torrent_id,
        files=files,
        fetch_html=fetch_html,
        parsers=parsers,
        explicit_url=explicit_url,
        explicit_slug=explicit_slug,
        collection_override=collection_override,
        media_type_override=media_type_override,
        allow_fallback=allow_fallback,
    )
    if bound is None:
        return Ok(None)

    current_index = load_catalog_index(addon_dir, clean=clean_before_build)
    next_index = upsert_media_item(current_index, bound)
    match commit_catalog_index(
        addon_dir, next_index, addon_config=addon_config, clean=clean_before_build
    ):
        case Err() as err:
            return err
        case Ok(written_count):
            return Ok(
                WeaveMediaOutcome(
                    metadata=bound.metadata,
                    episode_matches=bound.episode_matches,
                    files_written=written_count,
                )
            )


def _print_weave_outcome(bound: BoundMediaItem, addon_dir: Path) -> None:
    meta = bound.metadata
    status_note = (
        f"{len(bound.episode_matches)}/{len(meta.episodes) or len(bound.episode_matches)} files linked"
        if bound.episode_matches
        else "queued (run `cb sync` once download finishes)"
    )
    console.print(
        f"   🎨 [Metadata] Matched collection '{meta.collection}' ({meta.media_type}) for '{meta.title}' — weaving artwork & synopses..."
    )
    console.print(
        f"   ✨ [Metadata] Built '{meta.title}' in {addon_dir.name}/ ({meta.stremio_id}, {status_note})"
    )


def _weave_into_index_and_log(
    *,
    index: CatalogIndex,
    display_title: str,
    info_hash: str,
    torrent_id: int,
    files: Sequence[TorrentFileEntry],
    addon_dir: Path,
    http_client: HttpxClient,
    url: str | None = None,
    slug: str | None = None,
    collection: str | None = None,
    media_type: MediaType | None = None,
    allow_fallback: bool = False,
) -> tuple[CatalogIndex, bool]:
    """Resolves `BoundMediaItem` and upserts it into `index` in memory without disk I/O."""
    bound = resolve_bound_media_item(
        display_title=display_title,
        info_hash=info_hash,
        torrent_id=torrent_id,
        files=files,
        fetch_html=http_client.fetch_html,
        explicit_url=url,
        explicit_slug=slug,
        collection_override=collection,
        media_type_override=media_type,
        allow_fallback=allow_fallback,
    )
    if bound is None:
        return index, False
    _print_weave_outcome(bound, addon_dir)
    return upsert_media_item(index, bound), True


def _maybe_publish_addon(addon_dir: Path, message: str) -> None:
    match publish_git_repo(addon_dir, commit_message=message):
        case Ok(True):
            console.print(f"🚀 [Contraband] Published updated addon files in {addon_dir.name} to GitHub!")
        case Ok(False):
            pass
        case Err(err):
            err_console.print(f"⚠️  [Contraband] Auto-publish warning: {err}")


def _refresh_aiostreams_cache(
    addon_dir: Path,
    http_client: HttpClientProtocol,
    env: Mapping[str, str] | None = None,
) -> bool:
    env_map = env or {}
    install_url = env_map.get("AIOSTREAMS_INSTALL_URL", "").strip().rstrip("/")

    if not install_url:
        aiomanager_path = addon_dir.parent / "stream-weaver" / "configs" / "aiomanager.json"
        if not aiomanager_path.exists():
            return False
        match read_json_file(aiomanager_path):
            case Err():
                return False
            case Ok(mgr):
                pass

        addons_sec = mgr.get("addons")
        if isinstance(addons_sec, dict) and isinstance(addons_sec.get("savedAddons"), list):
            for sa in addons_sec["savedAddons"]:
                if not isinstance(sa, dict):
                    continue
                raw_mf = sa.get("manifest")
                mf: dict[str, Any] = raw_mf if isinstance(raw_mf, dict) else {}
                if sa.get("name") == "AIOStreams" or str(mf.get("id", "")).startswith("aiostreams"):
                    install_url = str(sa.get("installUrl", "")).rstrip("/")
                    break

    if not install_url:
        return False

    install_url = _SECRET_PLACEHOLDER_RE.sub(
        lambda m: env_map.get(m.group(1)) or m.group(0),
        install_url,
    )
    if "{{SECRET_" in install_url:
        return False

    if install_url.endswith("/manifest.json"):
        install_url = install_url[: -len("/manifest.json")]

    refresh_url = f"{install_url}/stream/movie/aiostreams::library.torbox.action.refresh.json"
    return isinstance(http_client.request_json("GET", refresh_url, timeout=15.0), Ok)


def _expand_cli_targets(
    raw_targets: Sequence[str],
    from_file: Path | None = None,
) -> Result[tuple[tuple[str, ...], list[tuple[str, int]]]]:
    expanded: list[str] = []
    file_extractions: list[tuple[str, int]] = []

    if from_file is not None:
        fpath = from_file.expanduser()
        if not fpath.exists():
            return Err(ContrabandError(f"File not found: {fpath}"))
        text = fpath.read_text(encoding="utf-8", errors="replace")
        found = extract_magnets_from_text(text)
        file_extractions.append((fpath.name, len(found)))
        expanded.extend(m.magnet_uri for m in found)

    for target in raw_targets:
        stripped = target.strip()
        if not stripped.lower().startswith("magnet:?") and len(stripped) < 255:
            try:
                cand = Path(stripped).expanduser()
                if cand.exists() and cand.is_file() and cand.suffix.lower() in (".md", ".txt", ".csv"):
                    text = cand.read_text(encoding="utf-8", errors="replace")
                    found = extract_magnets_from_text(text)
                    file_extractions.append((cand.name, len(found)))
                    expanded.extend(m.magnet_uri for m in found)
                    continue
            except OSError:
                pass
        expanded.append(stripped)

    return Ok((tuple(expanded), file_extractions))


def _promote_queued_slots(
    debrid: DebridProviderProtocol,
    active_count: int,
    queued_items: Sequence[DebridQueuedItem],
) -> tuple[int, tuple[DebridQueuedItem, ...], tuple[DebridQueuedItem, ...]]:
    remaining = list(queued_items)
    promoted: list[DebridQueuedItem] = []
    current_active = active_count

    while current_active < debrid.max_active_downloads and remaining:
        next_q = remaining.pop(0)
        match debrid.start_queued_item(next_q.id):
            case Ok(True):
                current_active += 1
                promoted.append(next_q)
            case _:
                remaining.insert(0, next_q)
                break

    return current_active, tuple(promoted), tuple(remaining)


def _discover_synced_torrent_ids(addon_dir: Path) -> set[int]:
    idx = load_catalog_index(addon_dir)
    return {
        item.torrent_id
        for item in idx.items
        if item.torrent_id > 0 and item.episode_matches
    }


# ============================================================================
# CLI Commands (`cb init`, `cb list`, `cb sync`, `cb watch`, `cb add`, `cb build`, `cb publish`)
# ============================================================================

_INIT_ENV_EXAMPLE = """# Contraband Static Addon Environment
DEBRID_PROVIDER=torbox
SECRET_TORBOX_API_KEY=
SECRET_RD_API_KEY=
AIOSTREAMS_INSTALL_URL=
"""

_INIT_GITIGNORE = """.env
.DS_Store
__pycache__/
.venv/
"""


@app.command("init")
def cmd_init(
    target_dir: Annotated[
        Path | None,
        typer.Argument(
            help="Target directory to initialize as a Contraband static addon workspace (defaults to --dir or current directory)."
        ),
    ] = None,
    addon_id: Annotated[
        str,
        typer.Option("--id", help="Reverse-DNS Stremio addon ID"),
    ] = "org.community.contraband",
    addon_name: Annotated[
        str,
        typer.Option("--name", help="Display name of your static Stremio addon"),
    ] = "My Contraband",
    description: Annotated[
        str,
        typer.Option("--description", help="Short Stremio manifest description"),
    ] = "Curated niche courses, rare lectures, and unlisted specials",
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing scaffold files (never overwrites .env)"),
    ] = False,
    workspace_dir: AddonDirOption = None,
) -> None:
    """Scaffold a standalone Contraband static Stremio addon workspace (`addon.toml`, `.env.example`, `manifest.json`)."""
    dest = (
        target_dir
        if target_dir is not None
        else (workspace_dir if workspace_dir is not None else (_ACTIVE_ADDON_DIR or Path.cwd()))
    ).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    escaped_id = addon_id.replace('"', '\\"').strip() or "org.community.contraband"
    escaped_name = addon_name.replace('"', '\\"').strip() or "My Contraband"
    escaped_desc = description.replace('"', '\\"').strip()
    addon_toml_content = (
        "[addon]\n"
        f'id = "{escaped_id}"\n'
        f'name = "{escaped_name}"\n'
        'version = "0.1.0"\n'
        f'description = "{escaped_desc}"\n'
        f'logo = "{DEFAULT_CONTRABAND_LOGO}"\n'
        'id_prefixes = ["sw_"]\n'
        "emit_library_meta = false\n"
    )

    created_files: list[str] = []
    for fname, content in (
        ("addon.toml", addon_toml_content),
        (".env.example", _INIT_ENV_EXAMPLE),
        (".gitignore", _INIT_GITIGNORE),
    ):
        fpath = dest / fname
        if not fpath.exists() or force:
            fpath.write_text(content, encoding="utf-8")
            created_files.append(fname)

    env = load_env_with_fallback(dest)
    match load_addon_config(dest, env):
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)
        case Ok(addon_cfg):
            pass

    existing_index = load_catalog_index(dest)
    match commit_catalog_index(dest, existing_index, addon_config=addon_cfg):
        case Err(commit_err):
            err_console.print(f"❌ {commit_err}")
            raise typer.Exit(code=1)
        case Ok(written):
            if written > 0:
                created_files.extend(["manifest.json", "catalog_index.json"])

    console.print(f"✨ Initialized Contraband workspace in {dest}")
    if created_files:
        console.print(f"   • Scaffolded: {', '.join(dict.fromkeys(created_files))}")
    if (dest / ".env").exists():
        console.print("   • Preserved existing .env")


@app.command("list")
def cmd_list(
    workspace_dir: AddonDirOption = None,
) -> None:
    """List all active, completed, and queued torrents in your TorBox Cloud Library."""
    _, env, _ = _resolve_context(workspace_dir)
    with HttpxClient() as http_client:
        debrid = _require_torbox_provider(env, http_client)
        with console.status("⏳ Fetching TorBox Cloud Library & Queue in parallel..."):
            lib_res, q_res = _fetch_library_and_queue_parallel(debrid)

        match q_res:
            case Ok(q_items) if q_items:
                q_table = Table(
                    title=f"⏳ TorBox Cloud Queue ({len(q_items)} items)",
                    box=box.ROUNDED,
                    header_style="bold",
                )
                q_table.add_column("#", justify="right")
                q_table.add_column("Status")
                q_table.add_column("Queue ID", justify="right")
                q_table.add_column("Name")
                for idx, qitem in enumerate(q_items, start=1):
                    q_table.add_row(f"Q{idx:02d}", "🕒 Queued", str(qitem.id), qitem.name)
                console.print(q_table)
                console.print()
            case _:
                q_items = ()

        match lib_res:
            case Err(err):
                err_console.print(f"❌ {err}")
                raise typer.Exit(code=1)
            case Ok(items):
                if not items and not q_items:
                    console.print("📭 Your TorBox Cloud Library is currently empty.")
                    return
                lib_table = Table(
                    title=f"📚 TorBox Cloud Library ({len(items)} items — searchable in Nuvio)",
                    box=box.ROUNDED,
                    header_style="bold",
                )
                lib_table.add_column("#", justify="right")
                lib_table.add_column("Status")
                lib_table.add_column("Size", justify="right")
                lib_table.add_column("Videos", justify="right")
                lib_table.add_column("ID", justify="right")
                lib_table.add_column("Name")
                for idx, item in enumerate(items, start=1):
                    size_str = format_bytes(item.size_bytes)
                    badge = (
                        "✅ Ready"
                        if item.download_finished
                        else f"⏳ {item.download_state} ({item.progress * 100.0:.1f}%)"
                    )
                    lib_table.add_row(
                        str(idx),
                        badge,
                        size_str,
                        str(item.video_count),
                        str(item.id),
                        item.name,
                    )
                console.print(lib_table)


@app.command("sync")
def cmd_sync(
    weave_all: Annotated[
        bool,
        typer.Option("--weave", help="Force weaving structured Contraband metadata for all finished downloads"),
    ] = False,
    workspace_dir: AddonDirOption = None,
) -> None:
    """Scan TorBox Cloud Library, weave Contraband metadata for finished downloads, and promote queued slots."""
    addon_dir, env, addon_cfg = _resolve_context(workspace_dir)
    with HttpxClient() as http_client:
        debrid = _require_torbox_provider(env, http_client)
        with console.status("⏳ Scanning TorBox Cloud Library & Queue in parallel..."):
            lib_res, q_res = _fetch_library_and_queue_parallel(debrid)

        match lib_res:
            case Err(err):
                err_console.print(f"❌ {err}")
                raise typer.Exit(code=1)
            case Ok(library_items):
                pass

        q_items: tuple[DebridQueuedItem, ...] = (
            q_res.value if isinstance(q_res, Ok) else ()
        )

        catalog_index = load_catalog_index(addon_dir)
        built_count = 0
        active_count = 0

        for item in library_items:
            if not item.id or not item.name or not item.info_hash:
                continue
            if not item.download_finished:
                active_count += 1
                continue
            catalog_index, woven = _weave_into_index_and_log(
                index=catalog_index,
                display_title=item.name,
                info_hash=item.info_hash,
                torrent_id=item.id,
                files=item.files,
                addon_dir=addon_dir,
                http_client=http_client,
                allow_fallback=weave_all,
            )
            if woven:
                built_count += 1

        if built_count > 0 and isinstance(
            commit_err := commit_catalog_index(addon_dir, catalog_index, addon_config=addon_cfg),
            Err,
        ):
            err_console.print(f"❌ {commit_err.error}")
            raise typer.Exit(code=1)

        new_active, promoted, remaining_q = _promote_queued_slots(debrid, active_count, q_items)
        for prom in promoted:
            console.print(
                f"🚀 [TorBox Queue] Promoted queued download to active slot: {prom.name} (Queue ID: {prom.id})"
            )
        if built_count > 0:
            _maybe_publish_addon(addon_dir, f"feat(catalog): Sync {built_count} completed item(s) from TorBox")
        console.print(
            f"\n✅ Synced metadata for {built_count} completed item(s) in Contraband "
            f"({new_active} actively downloading, {len(remaining_q)} waiting in TorBox Cloud Queue)."
        )


@app.command("watch")
def cmd_watch(
    weave_all: Annotated[
        bool,
        typer.Option("--weave", help="Force weaving structured Contraband metadata for all finished downloads"),
    ] = False,
    poll_seconds: Annotated[
        int,
        typer.Option("--poll-seconds", help="Polling interval in seconds"),
    ] = 20,
    max_active: Annotated[
        int,
        typer.Option("--max-active", help="Maximum concurrent downloads to keep active"),
    ] = 3,
    workspace_dir: AddonDirOption = None,
) -> None:
    """Continuously drain the TorBox queue, keeping parallel slots active and publishing finished items."""
    addon_dir, env, addon_cfg = _resolve_context(workspace_dir)
    with HttpxClient() as http_client:
        debrid = _require_torbox_provider(env, http_client, max_active=max_active)

        synced_ids = _discover_synced_torrent_ids(addon_dir)
        console.print(
            f"👀 Watching TorBox Queue — keeping {max_active} parallel slots active & auto-syncing finished downloads..."
        )

        while True:
            lib_res, q_res = _fetch_library_and_queue_parallel(debrid)
            lib_items: tuple[DebridTorrentItem, ...] = (
                lib_res.value if isinstance(lib_res, Ok) else ()
            )
            q_items: tuple[DebridQueuedItem, ...] = (
                q_res.value if isinstance(q_res, Ok) else ()
            )

            active_items: list[DebridTorrentItem] = []
            newly_synced = 0
            catalog_index: CatalogIndex | None = None

            for item in lib_items:
                if not item.id or not item.name or not item.info_hash:
                    continue
                if not item.download_finished or not item.files:
                    active_items.append(item)
                elif item.id not in synced_ids:
                    if catalog_index is None:
                        catalog_index = load_catalog_index(addon_dir)
                    catalog_index, woven = _weave_into_index_and_log(
                        index=catalog_index,
                        display_title=item.name,
                        info_hash=item.info_hash,
                        torrent_id=item.id,
                        files=item.files,
                        addon_dir=addon_dir,
                        http_client=http_client,
                        allow_fallback=weave_all,
                    )
                    if woven:
                        newly_synced += 1
                    synced_ids.add(item.id)

            if newly_synced > 0 and catalog_index is not None:
                commit_catalog_index(addon_dir, catalog_index, addon_config=addon_cfg)
                _maybe_publish_addon(
                    addon_dir, f"feat(catalog): Auto-sync {newly_synced} newly completed download(s)"
                )

            active_count, promoted, remaining_q = _promote_queued_slots(
                debrid, len(active_items), q_items
            )
            for prom in promoted:
                console.print(
                    f"🚀 [TorBox Queue] Started next download ({active_count}/{max_active} active, "
                    f"{len(remaining_q)} queued): {prom.name}"
                )

            if active_count == 0 and not remaining_q:
                _maybe_publish_addon(addon_dir, "feat(catalog): Finalize Contraband queue sync")
                if _refresh_aiostreams_cache(addon_dir, http_client, env=env):
                    console.print("🔄 [AIOStreams] Refreshed TorBox Cloud Library cache — ready in Nuvio!")
                console.print("\n🎉 All queued TorBox downloads have finished and published to Contraband!")
                return

            prog_summary = ", ".join(f"{a.name[:28]} ({a.progress * 100:.0f}%)" for a in active_items)
            console.print(
                f"⏳ Active: {active_count}/{max_active} [{prog_summary or 'starting...'}] | Queued: {len(remaining_q)}"
            )
            time.sleep(poll_seconds)


@app.command("add")
def cmd_add(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="Magnet links, 40-char infoHashes, .md/.txt magnet lists, or .torrent files"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", help="Path to a Markdown (.md) or text file containing magnet links"),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Custom title override for the torrent in your TorBox Library"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Webpage URL to scrape metadata from (Archive.org, courses, specials, IMDb/TMDB, YouTube)"),
    ] = None,
    meta_file: Annotated[
        Path | None,
        typer.Option("--meta-file", help="Path to a local .toml or .json metadata sidecar file"),
    ] = None,
    slug: Annotated[
        str | None,
        typer.Option("--slug", help="Item slug override (e.g. 'napoleon' or 'ia:night_of_the_living_dead')"),
    ] = None,
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Custom Contraband catalog row name (e.g. 'Stand-Up Specials')"),
    ] = None,
    media_type: Annotated[
        str,
        typer.Option("--type", help="Stremio media type override: auto, series, or movie"),
    ] = "auto",
    weave_all: Annotated[
        bool,
        typer.Option("--weave", help="Force weaving structured Contraband metadata from filenames"),
    ] = False,
    workspace_dir: AddonDirOption = None,
) -> None:
    """Add magnet links, .md lists, or .torrent files to TorBox and weave Contraband metadata."""
    addon_dir, env, addon_cfg = _resolve_context(workspace_dir)
    resolved_mtype = _parse_media_type_option(media_type)
    if meta_file is not None:
        resolved_meta_path = meta_file.expanduser().resolve()
        if not resolved_meta_path.is_file():
            err_console.print(f"❌ Metadata sidecar file not found: {resolved_meta_path}")
            raise typer.Exit(code=1)
        url = str(resolved_meta_path)

    match _expand_cli_targets(targets or [], from_file=from_file):
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)
        case Ok((expanded_targets, extractions)):
            for fname, count in extractions:
                console.print(f"📄 Extracted {count} magnet links from {fname}")

    if not expanded_targets:
        err_console.print("❌ No magnet targets or .torrent files specified.")
        raise typer.Exit(code=1)

    with HttpxClient() as http_client:
        debrid = _require_torbox_provider(env, http_client)

        library_by_hash: dict[str, DebridTorrentItem] = {}
        queued_by_hash: dict[str, DebridQueuedItem] = {}
        active_downloads = 0

        with console.status("⏳ Checking TorBox Cloud Library & Queue in parallel..."):
            lib_res, q_res = _fetch_library_and_queue_parallel(debrid)

        if isinstance(lib_res, Ok):
            for item in lib_res.value:
                if item.info_hash:
                    library_by_hash[item.info_hash] = item
                if not item.download_finished:
                    active_downloads += 1

        if isinstance(q_res, Ok):
            for qitem in q_res.value:
                if qitem.info_hash:
                    queued_by_hash[qitem.info_hash] = qitem

        prepared_items: list[
            tuple[str, Path | None, Result[_PreparedAddTarget, ContrabandError]]
        ] = []
        for idx, raw_target in enumerate(expanded_targets, start=1):
            prefix_label = f"[{idx}/{len(expanded_targets)}] " if len(expanded_targets) > 1 else ""
            t_path: Path | None = None
            if not raw_target.lower().startswith("magnet:?") and len(raw_target) < 255:
                try:
                    cand = Path(raw_target).expanduser()
                    if cand.exists() and cand.is_file():
                        t_path = cand
                except OSError:
                    t_path = None

            if t_path is not None:
                raw_bytes = t_path.read_bytes()
                match parse_torrent_bytes(raw_bytes, fallback_name=t_path.stem):
                    case Err(bencode_err):
                        prepared_items.append((prefix_label, t_path, Err(bencode_err)))
                    case Ok(parsed_t):
                        prepared_items.append(
                            (
                                prefix_label,
                                t_path,
                                Ok(
                                    _PreparedAddTarget(
                                        info_hash=parsed_t.info_hash,
                                        magnet_uri=parsed_t.magnet_uri,
                                        display_title=name or parsed_t.name,
                                        dn=parsed_t.name,
                                        torrent_path=t_path,
                                        torrent_bytes=raw_bytes,
                                        parsed_torrent=parsed_t,
                                    )
                                ),
                            )
                        )
            else:
                match parse_magnet_uri(raw_target):
                    case Err(mag_err):
                        prepared_items.append((prefix_label, None, Err(mag_err)))
                    case Ok(mag):
                        prepared_items.append(
                            (
                                prefix_label,
                                None,
                                Ok(
                                    _PreparedAddTarget(
                                        info_hash=mag.info_hash,
                                        magnet_uri=mag.magnet_uri,
                                        display_title=name or mag.display_name or mag.info_hash,
                                        dn=mag.display_name,
                                    )
                                ),
                            )
                        )

        hashes_to_check = [
            prep.value.info_hash.lower()
            for _, _, prep in prepared_items
            if isinstance(prep, Ok)
            and prep.value.info_hash.lower() not in library_by_hash
            and prep.value.info_hash.lower() not in queued_by_hash
        ]
        bulk_cache: Mapping[str, DebridCacheStatus] = {}
        if hashes_to_check and isinstance(
            bulk_res := debrid.check_cached_bulk(hashes_to_check), Ok
        ):
            bulk_cache = bulk_res.value

        catalog_index = load_catalog_index(addon_dir)
        index_dirty = False
        added_any = False
        cached_count = 0
        queued_count = 0
        failed_count = 0

        for prefix_label, t_path, prep_res in prepared_items:
            if t_path is not None:
                console.print(f"\n📦 {prefix_label}Inspecting .torrent file: {t_path.name}")
            match prep_res:
                case Err(err):
                    err_console.print(f"❌ {err}")
                    failed_count += 1
                    continue
                case Ok(target_item):
                    info_hash = target_item.info_hash
                    magnet_uri = target_item.magnet_uri
                    dn = target_item.dn
                    display_title = target_item.display_title
                    if target_item.parsed_torrent is not None:
                        parsed_t = target_item.parsed_torrent
                        console.print(f"   • Name:       {parsed_t.name}")
                        console.print(f"   • InfoHash:   {info_hash}")
                        console.print(
                            f"   • Total Size: {format_bytes(parsed_t.total_bytes)} ({len(parsed_t.video_files)} video files)"
                        )
                        if parsed_t.total_bytes > MAX_ESSENTIAL_BYTES:
                            console.print(
                                f"   ⚠️  Warning: Torrent size ({format_bytes(parsed_t.total_bytes)}) exceeds the 200 GB Essential plan limit!"
                            )
                    else:
                        console.print(f"\n🧲 {prefix_label}Inspecting magnet: {display_title}")
                        console.print(f"   • InfoHash:   {info_hash}")

            ih_lower = info_hash.lower()
            if ih_lower in library_by_hash:
                lib_item = library_by_hash[ih_lower]
                if lib_item.download_finished:
                    cached_count += 1
                    console.print(f"   • Status:     ⚡ Already in TorBox Library & Ready (ID: {lib_item.id})")
                else:
                    queued_count += 1
                    console.print(
                        f"   • Status:     ⏳ Actively Downloading on TorBox ({lib_item.progress * 100:.1f}%, ID: {lib_item.id})"
                    )
                catalog_index, woven = _weave_into_index_and_log(
                    index=catalog_index,
                    display_title=display_title,
                    info_hash=info_hash,
                    torrent_id=lib_item.id if lib_item.download_finished else 0,
                    files=lib_item.files if lib_item.download_finished else (),
                    addon_dir=addon_dir,
                    http_client=http_client,
                    url=url,
                    slug=slug,
                    collection=collection,
                    media_type=resolved_mtype,
                    allow_fallback=weave_all,
                )
                index_dirty = index_dirty or woven
                added_any = True
                continue

            if ih_lower in queued_by_hash:
                q_item = queued_by_hash[ih_lower]
                queued_count += 1
                console.print(f"   • Status:     ⏳ Already Queued on TorBox (Queue ID: {q_item.id})")
                catalog_index, woven = _weave_into_index_and_log(
                    index=catalog_index,
                    display_title=display_title,
                    info_hash=info_hash,
                    torrent_id=0,
                    files=(),
                    addon_dir=addon_dir,
                    http_client=http_client,
                    url=url,
                    slug=slug,
                    collection=collection,
                    media_type=resolved_mtype,
                    allow_fallback=weave_all,
                )
                index_dirty = index_dirty or woven
                added_any = True
                continue

            is_cached = False
            c_info = bulk_cache.get(ih_lower)
            if c_info is not None and c_info.is_cached:
                is_cached = True
                console.print(
                    f"   • Status:     ⚡ INSTANTLY CACHED on TorBox ({format_bytes(c_info.size_bytes)}, {len(c_info.files)} files)"
                )
                if c_info.size_bytes > MAX_ESSENTIAL_BYTES:
                    console.print(
                        f"   ⚠️  Warning: Torrent size ({format_bytes(c_info.size_bytes)}) exceeds the 200 GB Essential plan limit!"
                    )
                should_queue = False
            else:
                console.print("   • Status:     ⏳ Uncached (TorBox will queue & download it in the cloud)")
                should_queue = active_downloads >= debrid.max_active_downloads

            add_res = (
                debrid.add_torrent_file(
                    target_item.torrent_path,
                    target_item.torrent_bytes,
                    as_queued=should_queue,
                )
                if target_item.torrent_path is not None and target_item.torrent_bytes is not None
                else debrid.add_magnet(magnet_uri, name=name or dn, as_queued=should_queue)
            )

            match add_res:
                case Err(err):
                    failed_count += 1
                    err_console.print(f"   ❌ [TorBox] {err}")
                case Ok(add_outcome):
                    if is_cached or add_outcome.was_cached:
                        cached_count += 1
                    else:
                        queued_count += 1
                        if not should_queue and add_outcome.torrent_id is not None:
                            active_downloads += 1
                    id_display = add_outcome.torrent_id or add_outcome.queued_id
                    console.print(
                        f"   ✅ [TorBox] {add_outcome.detail}"
                        + (f" (ID: {id_display})" if id_display else "")
                    )

                    real_tid = add_outcome.torrent_id if (add_outcome.torrent_id and is_cached) else 0
                    fetched_files: tuple[TorrentFileEntry, ...] = ()
                    if real_tid and isinstance(f_res := debrid.fetch_torrent_files(real_tid), Ok):
                        fetched_files = f_res.value

                    catalog_index, woven = _weave_into_index_and_log(
                        index=catalog_index,
                        display_title=display_title,
                        info_hash=info_hash,
                        torrent_id=real_tid,
                        files=fetched_files,
                        addon_dir=addon_dir,
                        http_client=http_client,
                        url=url,
                        slug=slug,
                        collection=collection,
                        media_type=resolved_mtype,
                        allow_fallback=weave_all,
                    )
                    index_dirty = index_dirty or woven
                    added_any = True

        if index_dirty and isinstance(
            commit_err := commit_catalog_index(addon_dir, catalog_index, addon_config=addon_cfg),
            Err,
        ):
            err_console.print(f"❌ {commit_err.error}")
            raise typer.Exit(code=1)

        if len(expanded_targets) > 1:
            console.print(
                f"\n📊 Batch Summary: {cached_count} instantly cached, {queued_count} queued on TorBox, {failed_count} failed."
            )

        if added_any:
            _maybe_publish_addon(addon_dir, "feat(catalog): Add new Contraband metadata and stream links")
            if _refresh_aiostreams_cache(addon_dir, http_client, env=env):
                console.print("🔄 [AIOStreams] Refreshed TorBox Cloud Library cache — ready in Nuvio!")
            console.print("\n🎬 Done! Open Nuvio or Stremio on Home to watch.")
            return

    raise typer.Exit(code=1)


@app.command("build")
def cmd_build(
    info_hash: Annotated[
        str,
        typer.Option("--hash", help="40-char torrent infoHash"),
    ],
    title: Annotated[
        str,
        typer.Option("--title", help="Torrent or media display title"),
    ] = "",
    slug: Annotated[
        str | None,
        typer.Option("--slug", help="Optional item slug (e.g. 'napoleon' or 'ia:night_of_the_living_dead')"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Optional webpage URL (Archive.org, MasterClass, Wondrium, IMDb, YouTube, etc.)"),
    ] = None,
    meta_file: Annotated[
        Path | None,
        typer.Option("--meta-file", help="Path to a local .toml or .json metadata sidecar file"),
    ] = None,
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Optional custom catalog row name (e.g. 'Stand-Up Specials')"),
    ] = None,
    media_type: Annotated[
        str,
        typer.Option("--type", help="Stremio media type override: auto, series, or movie"),
    ] = "auto",
    torrent_id: Annotated[
        int,
        typer.Option("--torrent-id", help="TorBox torrent ID"),
    ] = 0,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Clean catalog/meta/stream directories before building"),
    ] = False,
    publish: Annotated[
        bool,
        typer.Option("--publish", help="Commit and push addon repo changes to GitHub"),
    ] = False,
    workspace_dir: AddonDirOption = None,
) -> None:
    """Build static Stremio metadata for Contraband (courses, specials, documentaries, series, or movies)."""
    addon_dir, env, addon_cfg = _resolve_context(workspace_dir)
    resolved_mtype = _parse_media_type_option(media_type)
    if meta_file is not None:
        resolved_meta_path = meta_file.expanduser().resolve()
        if not resolved_meta_path.is_file():
            err_console.print(f"❌ Metadata sidecar file not found: {resolved_meta_path}")
            raise typer.Exit(code=1)
        url = str(resolved_meta_path)

    with HttpxClient() as http_client:
        api_key = env.get("SECRET_TORBOX_API_KEY", "")
        tb_files: tuple[TorrentFileEntry, ...] = ()
        if torrent_id and api_key:
            debrid = TorBoxProvider(api_key=api_key, http_client=http_client)
            if isinstance(f_res := debrid.fetch_torrent_files(torrent_id), Ok):
                tb_files = f_res.value

        display_title = title or slug or url or info_hash
        build_res = weave_media_into_addon(
            display_title=display_title,
            info_hash=info_hash,
            torrent_id=torrent_id,
            files=tb_files,
            addon_dir=addon_dir,
            fetch_html=http_client.fetch_html,
            explicit_url=url,
            explicit_slug=slug,
            collection_override=collection,
            media_type_override=resolved_mtype,
            allow_fallback=True,
            clean_before_build=clean,
            addon_config=addon_cfg,
        )

    match build_res:
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)
        case Ok(None):
            err_console.print(f"❌ Could not build metadata for '{display_title}'")
            raise typer.Exit(code=1)
        case Ok(outcome):
            assert outcome is not None
            meta = outcome.metadata
            console.print(
                f"✅ Built {meta.collection} ({meta.media_type}) metadata for '{meta.title}' ({meta.stremio_id})"
            )
            console.print(f"   Poster:   {meta.poster}")
            console.print(f"   Backdrop: {meta.background}")
            console.print(f"   Items:    {len(meta.episodes) or 1} mapped in {addon_dir}/")
            if publish:
                _maybe_publish_addon(addon_dir, f"feat(catalog): Add {meta.title} ({meta.collection})")


@app.command("remove")
def cmd_remove(
    target: Annotated[
        str,
        typer.Argument(
            help="Stremio ID (e.g. 'sw_mc_999'), slug, infoHash, torrent ID, magnet URI, or title substring"
        ),
    ],
    publish: Annotated[
        bool,
        typer.Option("--publish", help="Commit and push addon repo changes to GitHub after removal"),
    ] = False,
    workspace_dir: AddonDirOption = None,
) -> None:
    """Remove a media item from the Contraband catalog and prune its static meta/stream/catalog files."""
    addon_dir, _, addon_cfg = _resolve_context(workspace_dir)
    current_index = load_catalog_index(addon_dir)
    updated_index, removed_items = remove_media_items(current_index, target)

    if not removed_items:
        err_console.print(f"❌ No Contraband catalog item matched '{target}'.")
        raise typer.Exit(code=1)

    match commit_catalog_index(addon_dir, updated_index, addon_config=addon_cfg, clean=True):
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)
        case Ok():
            pass

    for item in removed_items:
        meta = item.metadata
        console.print(
            f"🗑️  Removed '{meta.title}' ({meta.stremio_id}) from collection '{meta.collection}'"
        )

    console.print(
        f"✅ Pruned {len(removed_items)} item(s) — {len(updated_index.items)} item(s) remaining in {addon_dir.name}/."
    )
    if publish:
        removed_titles = ", ".join(i.metadata.title for i in removed_items[:3])
        _maybe_publish_addon(addon_dir, f"chore(catalog): Remove {removed_titles}")


@app.command("doctor")
def cmd_doctor(
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Recompile and prune orphaned/missing/stale static addon files"),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Skip live TorBox library/queue cross-check"),
    ] = False,
    publish: Annotated[
        bool,
        typer.Option("--publish", help="Commit and push repaired addon files to GitHub (with --fix)"),
    ] = False,
    workspace_dir: AddonDirOption = None,
) -> None:
    """Audit Contraband static addon integrity, episode bindings, and live TorBox cloud status."""
    addon_dir, env, addon_cfg = _resolve_context(workspace_dir)
    actual_vfs = read_addon_virtual_fs(addon_dir)
    catalog_index = seed_index_from_existing_files(actual_vfs.files)

    active_hashes: set[str] | None = None
    api_key = env.get("SECRET_TORBOX_API_KEY", "")
    if not offline and api_key:
        with (
            HttpxClient() as http_client,
            console.status("⏳ Cross-checking TorBox Cloud Library & Queue in parallel..."),
        ):
            debrid = TorBoxProvider(api_key=api_key, http_client=http_client)
            lib_res, q_res = _fetch_library_and_queue_parallel(debrid)
            if isinstance(lib_res, Ok):
                hashes: set[str] = {
                    item.info_hash.lower() for item in lib_res.value if item.info_hash
                }
                if isinstance(q_res, Ok):
                    hashes.update(q.info_hash.lower() for q in q_res.value if q.info_hash)
                active_hashes = hashes

    report = audit_contraband_addon(
        catalog_index,
        actual_vfs,
        addon_config=addon_cfg,
        active_hashes=active_hashes,
    )

    console.print(
        f"🩺 Contraband Addon Audit ({addon_dir.name}/): "
        f"{report.total_items} item(s) ({report.series_count} series, {report.movie_count} movies)"
    )

    summary_table = Table(box=box.ROUNDED, header_style="bold")
    summary_table.add_column("Check")
    summary_table.add_column("Count", justify="right")
    summary_table.add_column("Status")
    summary_table.add_row(
        "Catalog Items",
        f"{report.total_items} ({report.series_count}S / {report.movie_count}M)",
        "✅ Indexed",
    )
    summary_table.add_row(
        "Unlinked / Queued Items",
        str(len(report.unlinked_items)),
        "✅ All Linked" if not report.unlinked_items else "⚠️  Needs `cb sync`",
    )
    if active_hashes is not None:
        summary_table.add_row(
            "Missing on TorBox",
            str(len(report.missing_on_debrid)),
            "✅ All Present" if not report.missing_on_debrid else "⚠️  Missing Hashes",
        )
    summary_table.add_row(
        "Static File Drift",
        str(len(report.orphaned_files) + len(report.missing_files) + len(report.stale_files)),
        "✅ In Sync" if not report.has_fs_drift else "⚠️  Needs `cb doctor --fix`",
    )
    console.print(summary_table)

    if report.unlinked_items:
        console.print(f"\n⚠️  Unlinked / Queued Items ({len(report.unlinked_items)}):")
        for item in report.unlinked_items:
            console.print(
                f"   • {item.metadata.title} ({item.metadata.stremio_id}) — torrent_id={item.torrent_id}, "
                f"{len(item.episode_matches)} files linked (run `cb sync` once download completes)"
            )

    if report.missing_on_debrid:
        console.print(f"\n⚠️  Missing from TorBox Cloud Library/Queue ({len(report.missing_on_debrid)}):")
        for item in report.missing_on_debrid:
            console.print(
                f"   • {item.metadata.title} ({item.metadata.stremio_id}) — infoHash {item.info_hash}"
            )

    if report.orphaned_files:
        console.print(f"\n🧹 Orphaned Static Files ({len(report.orphaned_files)}):")
        for p in report.orphaned_files:
            console.print(f"   • {p}")

    if report.missing_files:
        console.print(f"\n🕳️  Missing Static Files ({len(report.missing_files)}):")
        for p in report.missing_files:
            console.print(f"   • {p}")

    if report.stale_files:
        console.print(f"\n📝 Out-of-Sync Static Files ({len(report.stale_files)}):")
        for p in report.stale_files:
            console.print(f"   • {p}")

    if fix and (report.has_fs_drift or not (addon_dir / "manifest.json").exists()):
        match commit_catalog_index(addon_dir, catalog_index, addon_config=addon_cfg, clean=True):
            case Err(err):
                err_console.print(f"❌ Failed to repair static files: {err}")
                raise typer.Exit(code=1)
            case Ok(written):
                console.print(
                    f"\n🔧 Repaired static addon tree ({written} file(s) written, {len(report.orphaned_files)} orphan(s) pruned)."
                )
                if publish:
                    _maybe_publish_addon(addon_dir, "chore(catalog): Repair and prune Contraband static addon tree")
                return

    if report.is_healthy:
        console.print("\n✅ All catalog items and static Stremio addon files are healthy and in sync.")
        return

    if report.has_fs_drift and not fix:
        console.print("\n💡 Tip: Run `cb doctor --fix` to recompile and prune out-of-sync static files.")
    raise typer.Exit(code=1)


@app.command("publish")
def cmd_publish(
    message: Annotated[
        str,
        typer.Option("--message", "-m", help="Commit message for Contraband addon repository"),
    ] = "feat(catalog): Sync Contraband metadata and stream links",
    workspace_dir: AddonDirOption = None,
) -> None:
    """Commit and push any pending Contraband static addon changes to GitHub."""
    addon_dir, _, _ = _resolve_context(workspace_dir)
    match publish_git_repo(addon_dir, commit_message=message):
        case Ok(True):
            console.print(f"🚀 [Contraband] Published updated addon files in {addon_dir.name} to GitHub!")
        case Ok(False):
            console.print(f"ℹ️  [Contraband] Working tree in {addon_dir.name} is already clean.")
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)


def main() -> None:
    """Main CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
    sys.exit(0)
