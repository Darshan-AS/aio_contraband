"""Contraband CLI (`cb`) — Static Stremio Addon Compiler & TorBox Cloud Ingestor."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from contraband.core.bencode import (
    extract_magnets_from_text,
    parse_magnet_uri,
    parse_torrent_bytes,
)
from contraband.core.compiler import (
    DEFAULT_ADDON_CONFIG,
    compile_contraband_addon,
    seed_index_from_existing_files,
    upsert_media_item,
)
from contraband.core.errors import StreamWeaverError
from contraband.core.matcher import format_bytes, match_files_to_episodes
from contraband.core.result import Err, Ok, Result
from contraband.core.types import (
    DEFAULT_CONTRABAND_LOGO,
    AddonConfig,
    BoundMediaItem,
    DebridQueuedItem,
    DebridTorrentItem,
    EpisodeFileMatch,
    MediaItemMetadata,
    MediaType,
    TorrentFileEntry,
    VirtualFS,
)
from contraband.effects.git import publish_git_repo
from contraband.effects.http import HttpClientProtocol, HttpxClient
from contraband.effects.storage import (
    load_env_with_fallback,
    materialize_virtual_fs,
    read_addon_virtual_fs,
    read_json_file,
)
from contraband.providers.metadata import (
    DEFAULT_METADATA_PROVIDERS,
    resolve_media_metadata,
)
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


def _resolve_context() -> tuple[Path, dict[str, str], AddonConfig]:
    addon_dir = Path(__file__).resolve().parents[2]
    env = load_env_with_fallback(addon_dir)
    if override_dir := env.get("ADDON_REPO_DIR"):
        addon_dir = Path(override_dir).expanduser()
    addon_cfg = AddonConfig(logo=env.get("ADDON_LOGO_URL") or DEFAULT_CONTRABAND_LOGO)
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


def weave_media_into_addon(
    *,
    display_title: str,
    info_hash: str,
    torrent_id: int,
    files: Sequence[TorrentFileEntry],
    addon_dir: Path,
    fetch_html: HtmlFetcher,
    parsers: Sequence[MetadataProviderProtocol] = DEFAULT_METADATA_PROVIDERS,
    explicit_url: str | None = None,
    explicit_slug: str | None = None,
    collection_override: str | None = None,
    media_type_override: MediaType | None = None,
    allow_fallback: bool = False,
    clean_before_build: bool = False,
    addon_config: AddonConfig = DEFAULT_ADDON_CONFIG,
) -> Result[WeaveMediaOutcome | None, StreamWeaverError]:
    """Resolves metadata, matches episodes/files, updates `CatalogIndex`, and materializes `VirtualFS`."""
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
        return Ok(None)

    if collection_override:
        meta = meta.with_collection(collection_override)
    if media_type_override:
        meta = meta.with_media_type(media_type_override)

    ep_matches = match_files_to_episodes(files, meta.episodes)
    bound = BoundMediaItem(
        metadata=meta,
        info_hash=info_hash.lower(),
        torrent_id=torrent_id,
        episode_matches=ep_matches,
    )

    existing_vfs = read_addon_virtual_fs(addon_dir) if not clean_before_build else VirtualFS()
    current_index = seed_index_from_existing_files(existing_vfs.files)
    next_index = upsert_media_item(current_index, bound)
    compiled_vfs = compile_contraband_addon(next_index, addon_config=addon_config)

    clean_subdirs = ("catalog", "meta", "stream") if clean_before_build else ()
    match materialize_virtual_fs(addon_dir, compiled_vfs, clean_subdirs=clean_subdirs):
        case Err() as err:
            return err
        case Ok(written_count):
            return Ok(
                WeaveMediaOutcome(
                    metadata=meta,
                    episode_matches=ep_matches,
                    files_written=written_count,
                )
            )


def _print_weave_outcome(outcome: WeaveMediaOutcome, addon_dir: Path) -> None:
    meta = outcome.metadata
    status_note = (
        f"{len(outcome.episode_matches)}/{len(meta.episodes) or len(outcome.episode_matches)} files linked"
        if outcome.episode_matches
        else "queued (run `cb sync` once download finishes)"
    )
    console.print(
        f"   🎨 [Metadata] Matched collection '{meta.collection}' ({meta.media_type}) for '{meta.title}' — weaving artwork & synopses..."
    )
    console.print(
        f"   ✨ [Metadata] Built '{meta.title}' in {addon_dir.name}/ ({meta.stremio_id}, {status_note})"
    )


def _weave_and_log(
    *,
    display_title: str,
    info_hash: str,
    torrent_id: int,
    files: Sequence[TorrentFileEntry],
    addon_dir: Path,
    http_client: HttpxClient,
    addon_config: AddonConfig,
    url: str | None = None,
    slug: str | None = None,
    collection: str | None = None,
    media_type: MediaType | None = None,
    allow_fallback: bool = False,
) -> bool:
    match weave_media_into_addon(
        display_title=display_title,
        info_hash=info_hash,
        torrent_id=torrent_id,
        files=files,
        addon_dir=addon_dir,
        fetch_html=http_client.fetch_html,
        explicit_url=url,
        explicit_slug=slug,
        collection_override=collection,
        media_type_override=media_type,
        allow_fallback=allow_fallback,
        addon_config=addon_config,
    ):
        case Ok(outcome) if outcome is not None:
            _print_weave_outcome(outcome, addon_dir)
            return True
        case _:
            return False


def _maybe_publish_addon(addon_dir: Path, message: str) -> None:
    match publish_git_repo(addon_dir, commit_message=message):
        case Ok(True):
            console.print(f"🚀 [Contraband] Published updated addon files in {addon_dir.name} to GitHub!")
        case Ok(False):
            pass
        case Err(err):
            err_console.print(f"⚠️  [Contraband] Auto-publish warning: {err}")


def _refresh_aiostreams_cache(addon_dir: Path, http_client: HttpClientProtocol) -> bool:
    aiomanager_path = addon_dir.parent / "stream-weaver" / "configs" / "aiomanager.json"
    if not aiomanager_path.exists():
        return False
    match read_json_file(aiomanager_path):
        case Err():
            return False
        case Ok(mgr):
            pass

    install_url = ""
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
    if install_url.endswith("/manifest.json"):
        install_url = install_url[: -len("/manifest.json")]

    refresh_url = f"{install_url}/stream/movie/aiostreams::library.torbox.action.refresh.json"
    return isinstance(http_client.request_json("GET", refresh_url, timeout=15.0), Ok)


def _expand_cli_targets(
    raw_targets: Sequence[str],
    from_file: Path | None = None,
) -> Result[tuple[tuple[str, ...], list[tuple[str, int]]], StreamWeaverError]:
    expanded: list[str] = []
    file_extractions: list[tuple[str, int]] = []

    if from_file is not None:
        fpath = from_file.expanduser()
        if not fpath.exists():
            return Err(StreamWeaverError(f"File not found: {fpath}"))
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
    synced_ids: set[int] = set()
    lib_meta_dir = addon_dir / "meta" / "library"
    if lib_meta_dir.exists():
        for p in lib_meta_dir.rglob("aiostreams::library.torbox.torrent.*.json"):
            m = re.search(r"\.(\d+)\.json$", p.name)
            if m:
                synced_ids.add(int(m.group(1)))
    return synced_ids


# ============================================================================
# CLI Commands (`cb list`, `cb sync`, `cb watch`, `cb add`, `cb build`, `cb publish`)
# ============================================================================


@app.command("list")
def cmd_list() -> None:
    """List all active, completed, and queued torrents in your TorBox Cloud Library."""
    _, env, _ = _resolve_context()
    debrid = _require_torbox_provider(env, HttpxClient())

    match debrid.list_queued():
        case Ok(q_items) if q_items:
            console.print(
                f"⏳ TorBox Cloud Queue ({len(q_items)} items — Web UI only displays 12 at a time):\n"
            )
            for idx, qitem in enumerate(q_items, start=1):
                console.print(f"  Q{idx:02d}. 🕒 Queued (ID: {qitem.id}) | {qitem.name}")
            console.print()
        case _:
            q_items = ()

    match debrid.list_library():
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)
        case Ok(items):
            if not items and not q_items:
                console.print("📭 Your TorBox Cloud Library is currently empty.")
                return
            console.print(f"📚 TorBox Cloud Library ({len(items)} items — all searchable in Nuvio):\n")
            for idx, item in enumerate(items, start=1):
                size_str = format_bytes(item.size_bytes)
                badge = (
                    "✅ Ready"
                    if item.download_finished
                    else f"⏳ {item.download_state} ({item.progress * 100.0:.1f}%)"
                )
                console.print(
                    f"  {idx:2d}. {badge} | {size_str:>10} | {item.video_count:2d} videos | {item.name}"
                )


@app.command("sync")
def cmd_sync(
    weave_all: Annotated[
        bool,
        typer.Option("--weave", help="Force weaving structured Contraband metadata for all finished downloads"),
    ] = False,
) -> None:
    """Scan TorBox Cloud Library, weave Contraband metadata for finished downloads, and promote queued slots."""
    addon_dir, env, addon_cfg = _resolve_context()
    http_client = HttpxClient()
    debrid = _require_torbox_provider(env, http_client)

    match debrid.list_library():
        case Err(err):
            err_console.print(f"❌ {err}")
            raise typer.Exit(code=1)
        case Ok(library_items):
            pass

    q_items: tuple[DebridQueuedItem, ...] = (
        q_res.value if isinstance(q_res := debrid.list_queued(), Ok) else ()
    )

    built_count = 0
    active_count = 0

    for item in library_items:
        if not item.id or not item.name or not item.info_hash:
            continue
        if not item.download_finished:
            active_count += 1
            continue
        if _weave_and_log(
            display_title=item.name,
            info_hash=item.info_hash,
            torrent_id=item.id,
            files=item.files,
            addon_dir=addon_dir,
            http_client=http_client,
            addon_config=addon_cfg,
            allow_fallback=weave_all,
        ):
            built_count += 1

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
) -> None:
    """Continuously drain the TorBox queue, keeping parallel slots active and publishing finished items."""
    addon_dir, env, addon_cfg = _resolve_context()
    http_client = HttpxClient()
    debrid = _require_torbox_provider(env, http_client, max_active=max_active)

    synced_ids = _discover_synced_torrent_ids(addon_dir)
    console.print(
        f"👀 Watching TorBox Queue — keeping {max_active} parallel slots active & auto-syncing finished downloads..."
    )

    while True:
        lib_items: tuple[DebridTorrentItem, ...] = (
            lib_res.value if isinstance(lib_res := debrid.list_library(), Ok) else ()
        )
        q_items: tuple[DebridQueuedItem, ...] = (
            q_res.value if isinstance(q_res := debrid.list_queued(), Ok) else ()
        )

        active_items: list[DebridTorrentItem] = []
        newly_synced = 0

        for item in lib_items:
            if not item.id or not item.name or not item.info_hash:
                continue
            if not item.download_finished or not item.files:
                active_items.append(item)
            elif item.id not in synced_ids:
                if _weave_and_log(
                    display_title=item.name,
                    info_hash=item.info_hash,
                    torrent_id=item.id,
                    files=item.files,
                    addon_dir=addon_dir,
                    http_client=http_client,
                    addon_config=addon_cfg,
                    allow_fallback=weave_all,
                ):
                    newly_synced += 1
                synced_ids.add(item.id)

        if newly_synced > 0:
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
            if _refresh_aiostreams_cache(addon_dir, http_client):
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
        typer.Option("--url", help="Webpage URL to scrape metadata from (courses, specials, IMDb/TMDB, YouTube)"),
    ] = None,
    slug: Annotated[
        str | None,
        typer.Option("--slug", help="Item slug override (e.g. 'napoleon')"),
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
) -> None:
    """Add magnet links, .md lists, or .torrent files to TorBox and weave Contraband metadata."""
    addon_dir, env, addon_cfg = _resolve_context()
    resolved_mtype = _parse_media_type_option(media_type)
    http_client = HttpxClient()
    debrid = _require_torbox_provider(env, http_client)

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

    library_by_hash: dict[str, DebridTorrentItem] = {}
    queued_by_hash: dict[str, DebridQueuedItem] = {}
    active_downloads = 0

    if isinstance(lib_res := debrid.list_library(), Ok):
        for item in lib_res.value:
            if item.info_hash:
                library_by_hash[item.info_hash] = item
            if not item.download_finished:
                active_downloads += 1

    if isinstance(q_res := debrid.list_queued(), Ok):
        for qitem in q_res.value:
            if qitem.info_hash:
                queued_by_hash[qitem.info_hash] = qitem

    added_any = False
    cached_count = 0
    queued_count = 0
    failed_count = 0

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

        dn: str | None = None
        if t_path is not None:
            console.print(f"\n📦 {prefix_label}Inspecting .torrent file: {t_path.name}")
            match parse_torrent_bytes(t_path.read_bytes(), fallback_name=t_path.stem):
                case Err(err):
                    err_console.print(f"❌ {err}")
                    failed_count += 1
                    continue
                case Ok(parsed_t):
                    info_hash = parsed_t.info_hash
                    display_title = name or parsed_t.name
                    magnet_uri = parsed_t.magnet_uri
                    dn = parsed_t.name
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
            match parse_magnet_uri(raw_target):
                case Err(err):
                    err_console.print(f"❌ {err}")
                    failed_count += 1
                    continue
                case Ok(mag):
                    info_hash = mag.info_hash
                    magnet_uri = mag.magnet_uri
                    dn = mag.display_name
                    display_title = name or dn or info_hash
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
            _weave_and_log(
                display_title=display_title,
                info_hash=info_hash,
                torrent_id=lib_item.id if lib_item.download_finished else 0,
                files=lib_item.files if lib_item.download_finished else (),
                addon_dir=addon_dir,
                http_client=http_client,
                addon_config=addon_cfg,
                url=url,
                slug=slug,
                collection=collection,
                media_type=resolved_mtype,
                allow_fallback=weave_all,
            )
            added_any = True
            continue

        if ih_lower in queued_by_hash:
            q_item = queued_by_hash[ih_lower]
            queued_count += 1
            console.print(f"   • Status:     ⏳ Already Queued on TorBox (Queue ID: {q_item.id})")
            _weave_and_log(
                display_title=display_title,
                info_hash=info_hash,
                torrent_id=0,
                files=(),
                addon_dir=addon_dir,
                http_client=http_client,
                addon_config=addon_cfg,
                url=url,
                slug=slug,
                collection=collection,
                media_type=resolved_mtype,
                allow_fallback=weave_all,
            )
            added_any = True
            continue

        is_cached = False
        if isinstance(cache_res := debrid.check_cached(info_hash), Ok) and cache_res.value.is_cached:
            is_cached = True
            c_info = cache_res.value
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
            debrid.add_torrent_file(t_path, t_path.read_bytes(), as_queued=should_queue)
            if t_path is not None
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

                _weave_and_log(
                    display_title=display_title,
                    info_hash=info_hash,
                    torrent_id=real_tid,
                    files=fetched_files,
                    addon_dir=addon_dir,
                    http_client=http_client,
                    addon_config=addon_cfg,
                    url=url,
                    slug=slug,
                    collection=collection,
                    media_type=resolved_mtype,
                    allow_fallback=weave_all,
                )
                added_any = True

    if len(expanded_targets) > 1:
        console.print(
            f"\n📊 Batch Summary: {cached_count} instantly cached, {queued_count} queued on TorBox, {failed_count} failed."
        )

    if added_any:
        _maybe_publish_addon(addon_dir, "feat(catalog): Add new Contraband metadata and stream links")
        if _refresh_aiostreams_cache(addon_dir, http_client):
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
        typer.Option("--slug", help="Optional item slug (e.g. 'napoleon')"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Optional webpage URL (MasterClass, Wondrium, IMDb, YouTube, etc.)"),
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
) -> None:
    """Build static Stremio metadata for Contraband (courses, specials, documentaries, series, or movies)."""
    addon_dir, env, addon_cfg = _resolve_context()
    resolved_mtype = _parse_media_type_option(media_type)
    http_client = HttpxClient()

    api_key = env.get("SECRET_TORBOX_API_KEY", "")
    tb_files: tuple[TorrentFileEntry, ...] = ()
    if torrent_id and api_key:
        debrid = TorBoxProvider(api_key=api_key, http_client=http_client)
        if isinstance(f_res := debrid.fetch_torrent_files(torrent_id), Ok):
            tb_files = f_res.value

    display_title = title or slug or url or info_hash
    match weave_media_into_addon(
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
    ):
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


@app.command("publish")
def cmd_publish(
    message: Annotated[
        str,
        typer.Option("--message", "-m", help="Commit message for Contraband addon repository"),
    ] = "feat(catalog): Sync Contraband metadata and stream links",
) -> None:
    """Commit and push any pending Contraband static addon changes to GitHub."""
    addon_dir, _, _ = _resolve_context()
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
