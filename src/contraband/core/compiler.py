"""Pure Static Stremio Addon Compiler (`CatalogIndex` -> `VirtualFS`) supporting both series and movie items."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from contraband.core.errors import ParseError
from contraband.core.matcher import clean_video_stem
from contraband.core.result import Err, Ok, Result
from contraband.core.types import (
    AddonConfig,
    BoundMediaItem,
    CatalogIndex,
    EpisodeFileMatch,
    EpisodeMetadata,
    JsonDict,
    MediaItemMetadata,
    MediaType,
    VirtualFS,
)

CATALOG_INDEX_PATH = PurePosixPath("catalog_index.json")


def _collection_catalog_id(collection_name: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "_", (collection_name or "contraband").lower()).strip("_")
        or "contraband"
    )


def _legacy_stremio_id(stremio_id: str) -> str:
    return stremio_id.replace("sw_pa_", "pa_")


def serialize_catalog_index(index: CatalogIndex) -> str:
    """Purely serializes a `CatalogIndex` into canonical formatted JSON."""
    payload = {
        "version": 2,
        "items": [
            {
                "info_hash": item.info_hash.lower(),
                "torrent_id": item.torrent_id,
                "metadata": {
                    "stremio_id": item.metadata.stremio_id,
                    "media_type": item.metadata.media_type,
                    "collection": item.metadata.collection,
                    "title": item.metadata.title,
                    "creator": item.metadata.creator,
                    "code": item.metadata.code,
                    "description": item.metadata.description,
                    "poster": item.metadata.poster,
                    "background": item.metadata.background,
                    "episode_thumbnail": item.metadata.episode_thumbnail,
                    "genres": list(item.metadata.genres),
                    "slug": item.metadata.slug,
                    "episodes": [
                        {
                            "number": ep.number,
                            "title": ep.title,
                            "description": ep.description,
                            "duration_seconds": ep.duration_seconds,
                            "thumbnail": ep.thumbnail,
                            "season": ep.season,
                        }
                        for ep in item.metadata.episodes
                    ],
                },
                "episode_matches": [
                    {
                        "episode_number": em.episode_number,
                        "file_index": em.file_index,
                        "filename": em.filename,
                    }
                    for em in item.episode_matches
                ],
            }
            for item in index.items
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def parse_catalog_index(raw_json: str) -> Result[CatalogIndex, ParseError]:
    """Purely deserializes a `CatalogIndex` from JSON string."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return Err(ParseError(f"Invalid JSON in catalog_index.json: {exc}"))

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return Err(ParseError("catalog_index.json must contain an 'items' list."))

    items: list[BoundMediaItem] = []
    for entry in data["items"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
            continue
        m = entry["metadata"]
        if not m.get("stremio_id"):
            continue
        raw_eps = m.get("episodes")
        raw_episodes: list[object] = raw_eps if isinstance(raw_eps, list) else []
        episodes = tuple(
            EpisodeMetadata(
                number=int(ep.get("number", idx + 1)),
                title=str(ep.get("title", "")),
                description=str(ep.get("description", "")),
                duration_seconds=int(ep.get("duration_seconds", 0)),
                thumbnail=str(ep.get("thumbnail", "")),
                season=int(ep.get("season", 1)),
            )
            for idx, ep in enumerate(raw_episodes)
            if isinstance(ep, dict)
        )
        raw_type = str(m.get("media_type", "series")).lower()
        media_type: MediaType = "movie" if raw_type == "movie" else "series"

        meta = MediaItemMetadata(
            stremio_id=str(m.get("stremio_id", "")),
            collection=str(m.get("collection") or "Contraband"),
            title=str(m.get("title", "")),
            creator=str(m.get("creator", "")),
            code=str(m.get("code", "")),
            description=str(m.get("description", "")),
            poster=str(m.get("poster", "")),
            background=str(m.get("background", "")),
            episode_thumbnail=str(m.get("episode_thumbnail", "")),
            genres=tuple(str(g) for g in m.get("genres", []) if isinstance(g, str)),
            episodes=episodes,
            slug=str(m.get("slug", "")),
            media_type=media_type,
        )
        matches = tuple(
            EpisodeFileMatch(
                episode_number=int(em.get("episode_number", 1)),
                file_index=int(em.get("file_index", 0)),
                filename=str(em.get("filename", "")),
            )
            for em in entry.get("episode_matches", [])
            if isinstance(em, dict)
        )
        items.append(
            BoundMediaItem(
                metadata=meta,
                info_hash=str(entry.get("info_hash", "")).lower(),
                torrent_id=int(entry.get("torrent_id", 0)),
                episode_matches=matches,
            )
        )

    return Ok(CatalogIndex(items=tuple(items)))


def seed_index_from_existing_files(
    existing_files: Mapping[PurePosixPath, str],
) -> CatalogIndex:
    """
    Purely reconstructs a `CatalogIndex` from an existing `aio_contraband` directory tree
    when `catalog_index.json` does not yet exist.
    """
    if CATALOG_INDEX_PATH in existing_files:
        match parse_catalog_index(existing_files[CATALOG_INDEX_PATH]):
            case Ok(idx):
                return idx
            case Err():
                pass

    items: list[BoundMediaItem] = []
    meta_paths = sorted(
        p
        for p in existing_files
        if len(p.parts) == 3
        and p.parts[0] == "meta"
        and p.parts[1] in ("series", "movie")
        and p.suffix == ".json"
    )

    for mp in meta_paths:
        item_type: MediaType = "movie" if mp.parts[1] == "movie" else "series"
        try:
            raw_meta = json.loads(existing_files[mp]).get("meta", {})
        except Exception:
            continue
        if not isinstance(raw_meta, dict) or not raw_meta.get("id"):
            continue

        stremio_id = str(raw_meta["id"])
        raw_name = str(raw_meta.get("name", ""))
        cast = raw_meta.get("cast") or []
        creator = str(cast[0]) if isinstance(cast, list) and cast else ""
        if creator and raw_name.endswith(f" — {creator}"):
            title = raw_name[: -len(f" — {creator}")]
        else:
            title = raw_name

        raw_desc = str(raw_meta.get("description", ""))
        code = ""
        desc = raw_desc
        code_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", raw_desc, re.DOTALL)
        if code_match:
            code, desc = code_match.group(1), code_match.group(2)

        genres = tuple(str(g) for g in raw_meta.get("genres", []) if isinstance(g, str))
        collection = genres[0] if genres else str(raw_meta.get("releaseInfo", "Contraband"))

        episodes: list[EpisodeMetadata] = []
        ep_matches: list[EpisodeFileMatch] = []
        info_hash = ""
        torrent_id = 0
        ep_thumb_default = ""

        if item_type == "movie":
            stream_rel = PurePosixPath("stream") / "movie" / f"{stremio_id}.json"
            if stream_rel in existing_files:
                try:
                    s_data = json.loads(existing_files[stream_rel])
                    streams = s_data.get("streams", [])
                    if isinstance(streams, list) and streams and isinstance(streams[0], dict):
                        s0 = streams[0]
                        info_hash = str(s0.get("infoHash", "")).lower()
                        f_idx = int(s0.get("fileIdx", 0))
                        f_name = str(
                            s0.get("behaviorHints", {}).get("filename")
                            or s0.get("title")
                            or f"{title}.mp4"
                        )
                        ep_matches.append(
                            EpisodeFileMatch(episode_number=1, file_index=f_idx, filename=f_name)
                        )
                except Exception:
                    pass
        else:
            for vid in raw_meta.get("videos", []):
                if not isinstance(vid, dict):
                    continue
                ep_num = int(vid.get("episode", 1))
                season = int(vid.get("season", 1))
                raw_vid_title = str(vid.get("title", ""))
                ep_title = re.sub(rf"^{ep_num}\.\s*", "", raw_vid_title)
                overview = str(vid.get("overview") or vid.get("description") or "")
                dur_secs = 0
                dur_m = re.search(r"⏱️\s*(\d+)\s*min", overview)
                if dur_m:
                    dur_secs = int(dur_m.group(1)) * 60
                ep_desc = overview.split("\n\n", 1)[1] if "\n\n" in overview else overview
                v_thumb = str(vid.get("thumbnail", ""))
                if v_thumb and not ep_thumb_default:
                    ep_thumb_default = v_thumb

                episodes.append(
                    EpisodeMetadata(
                        number=ep_num,
                        title=ep_title,
                        description=ep_desc,
                        duration_seconds=dur_secs,
                        thumbnail=v_thumb,
                        season=season,
                    )
                )

                vid_id = str(vid.get("id", ""))
                tb_match = re.match(r"^aiostreams::library\.torbox\.torrent\.(\d+):(\d+)$", vid_id)
                if tb_match:
                    torrent_id = int(tb_match.group(1))

                stream_rel = PurePosixPath("stream") / "series" / f"{stremio_id}_s{season}e{ep_num}.json"
                if stream_rel in existing_files:
                    try:
                        s_data = json.loads(existing_files[stream_rel])
                        streams = s_data.get("streams", [])
                        if isinstance(streams, list) and streams and isinstance(streams[0], dict):
                            s0 = streams[0]
                            info_hash = str(s0.get("infoHash", info_hash)).lower()
                            f_idx = int(s0.get("fileIdx", ep_num - 1))
                            f_name = str(
                                s0.get("behaviorHints", {}).get("filename")
                                or s0.get("title")
                                or f"{ep_num}. {ep_title}.mp4"
                            )
                            ep_matches.append(
                                EpisodeFileMatch(
                                    episode_number=ep_num,
                                    file_index=f_idx,
                                    filename=f_name,
                                )
                            )
                    except Exception:
                        pass

        if info_hash:
            items.append(
                BoundMediaItem(
                    metadata=MediaItemMetadata(
                        stremio_id=stremio_id,
                        collection=collection,
                        title=title,
                        creator=creator,
                        code=code,
                        description=desc,
                        poster=str(raw_meta.get("poster", "")),
                        background=str(raw_meta.get("background", "")),
                        episode_thumbnail=ep_thumb_default,
                        genres=genres,
                        episodes=tuple(episodes),
                        slug=stremio_id,
                        media_type=item_type,
                    ),
                    info_hash=info_hash,
                    torrent_id=torrent_id,
                    episode_matches=tuple(ep_matches),
                )
            )

    return CatalogIndex(items=tuple(items))


def upsert_media_item(index: CatalogIndex, new_item: BoundMediaItem) -> CatalogIndex:
    """
    Pure state transition: inserts `new_item` at the front of `index`, replacing any
    prior entry with the same `stremio_id` or legacy `pa_` ID.
    """
    target_id = new_item.metadata.stremio_id
    legacy_id = _legacy_stremio_id(target_id)
    remaining = tuple(
        item
        for item in index.items
        if item.metadata.stremio_id not in (target_id, legacy_id)
    )
    return CatalogIndex(items=(new_item, *remaining))


def _compile_single_item(
    bound: BoundMediaItem,
) -> tuple[JsonDict, PurePosixPath, JsonDict, JsonDict | None, dict[PurePosixPath, JsonDict]]:
    """
    Purely compiles a single `BoundMediaItem` into:
      `(catalog_card, meta_rel_path, meta_payload, library_meta_payload_or_none, stream_files_map)`
    """
    item_meta = bound.metadata
    stremio_id = item_meta.stremio_id
    media_type = item_meta.media_type
    torbox_lib_id = f"aiostreams::library.torbox.torrent.{bound.torrent_id}"
    match_by_ep = {m.episode_number: (m.file_index, m.filename) for m in bound.episode_matches}

    display_name = (
        f"{item_meta.title} — {item_meta.creator}"
        if item_meta.creator
        else item_meta.title
    )
    full_desc = (
        f"[{item_meta.code}] {item_meta.description}"
        if item_meta.code
        else item_meta.description
    )

    catalog_card: JsonDict = {
        "id": stremio_id,
        "type": media_type,
        "name": display_name,
        "poster": item_meta.poster,
        "background": item_meta.background,
        "description": full_desc,
        "releaseInfo": item_meta.code or item_meta.collection,
        "genres": list(item_meta.genres),
        "cast": [item_meta.creator] if item_meta.creator else [],
    }

    stream_files: dict[PurePosixPath, JsonDict] = {}

    if media_type == "movie":
        file_idx, filename = match_by_ep.get(1, (0, f"{item_meta.title}.mp4"))
        stream_rel_path = PurePosixPath("stream") / "movie" / f"{stremio_id}.json"
        stream_files[stream_rel_path] = {
            "streams": [
                {
                    "name": item_meta.collection,
                    "title": filename,
                    "infoHash": bound.info_hash.lower(),
                    "fileIdx": file_idx,
                    "behaviorHints": {
                        "filename": filename,
                        "bingeGroup": f"sw-{item_meta.slug or stremio_id}",
                    },
                }
            ]
        }
        meta_rel_path = PurePosixPath("meta") / "movie" / f"{stremio_id}.json"
        meta_payload: JsonDict = {"meta": dict(catalog_card)}
        library_meta: JsonDict | None = None
        if bound.torrent_id:
            library_meta = {
                "meta": {
                    **catalog_card,
                    "id": torbox_lib_id,
                    "type": "library",
                    "videos": [
                        {
                            "id": f"{torbox_lib_id}:{file_idx}",
                            "title": item_meta.title,
                            "season": 1,
                            "episode": 1,
                            "thumbnail": item_meta.episode_thumbnail or item_meta.background,
                            "overview": full_desc,
                            "description": full_desc,
                        }
                    ],
                }
            }
        return catalog_card, meta_rel_path, meta_payload, library_meta, stream_files

    episodes = item_meta.episodes
    if not episodes and match_by_ep:
        episodes = tuple(
            EpisodeMetadata(
                number=ep_num,
                title=clean_video_stem(fname),
                thumbnail=item_meta.episode_thumbnail,
            )
            for ep_num, (_, fname) in sorted(match_by_ep.items())
        )

    videos_series: list[JsonDict] = []
    videos_library: list[JsonDict] = []

    for ep in episodes:
        ep_num = ep.number
        file_idx, filename = match_by_ep.get(
            ep_num, (ep_num - 1, f"{ep_num}. {ep.title}.mp4")
        )
        dur_mins = round(ep.duration_seconds / 60) if ep.duration_seconds else 0
        ep_thumb = ep.thumbnail or item_meta.episode_thumbnail

        header_bits: list[str] = []
        if dur_mins > 0:
            header_bits.append(f"⏱️ {dur_mins} min")
        if item_meta.creator:
            header_bits.append(item_meta.creator)
        elif item_meta.collection:
            header_bits.append(item_meta.collection)

        header = " · ".join(header_bits)
        ep_overview = f"{header}\n\n{ep.description}".strip() if header else ep.description

        vid_series_id = f"{stremio_id}_s{ep.season}e{ep_num}"
        vid_playback_id = (
            f"{torbox_lib_id}:{file_idx}"
            if (bound.torrent_id and ep_num in match_by_ep)
            else vid_series_id
        )

        videos_series.append(
            {
                "id": vid_playback_id,
                "title": f"{ep_num}. {ep.title}",
                "season": ep.season,
                "episode": ep_num,
                "thumbnail": ep_thumb,
                "overview": ep_overview,
                "description": ep_overview,
            }
        )

        videos_library.append(
            {
                "id": f"{torbox_lib_id}:{file_idx}",
                "title": f"{ep_num}. {ep.title}",
                "season": ep.season,
                "episode": ep_num,
                "thumbnail": ep_thumb,
                "overview": ep_overview,
                "description": ep_overview,
            }
        )

        stream_rel_path = PurePosixPath("stream") / "series" / f"{vid_series_id}.json"
        stream_files[stream_rel_path] = {
            "streams": [
                {
                    "name": item_meta.collection,
                    "title": filename,
                    "infoHash": bound.info_hash.lower(),
                    "fileIdx": file_idx,
                    "behaviorHints": {
                        "filename": filename,
                        "bingeGroup": f"sw-{item_meta.slug or stremio_id}",
                    },
                }
            ]
        }

    meta_rel_path = PurePosixPath("meta") / "series" / f"{stremio_id}.json"
    series_meta: JsonDict = {"meta": {**catalog_card, "videos": videos_series}}
    library_meta = None
    if bound.torrent_id:
        library_meta = {
            "meta": {
                **catalog_card,
                "id": torbox_lib_id,
                "type": "library",
                "videos": videos_library,
            }
        }

    return catalog_card, meta_rel_path, series_meta, library_meta, stream_files


DEFAULT_ADDON_CONFIG = AddonConfig()


def compile_contraband_addon(
    index: CatalogIndex,
    addon_config: AddonConfig = DEFAULT_ADDON_CONFIG,
) -> VirtualFS:
    """
    Pure compiler: deterministically projects `CatalogIndex` into the full static Stremio
    addon file tree (`catalog_index.json`, `manifest.json`, `catalog/`, `meta/`, `stream/`),
    supporting both `"series"` and `"movie"` resources.
    """
    files: dict[PurePosixPath, str] = {}
    files[CATALOG_INDEX_PATH] = serialize_catalog_index(index)

    # Group cards by `(media_type, collection_id)`
    cards_by_bucket: dict[tuple[MediaType, str], list[JsonDict]] = {}
    display_by_bucket: dict[tuple[MediaType, str], str] = {}
    active_types: set[str] = {"series", "library"}

    for bound in index.items:
        card, meta_path, meta_payload, library_meta, stream_map = _compile_single_item(bound)
        m_type = bound.metadata.media_type
        active_types.add(m_type)
        col_id = _collection_catalog_id(bound.metadata.collection)
        bucket = (m_type, col_id)
        display_by_bucket.setdefault(
            bucket, bound.metadata.collection or col_id.replace("_", " ").title()
        )
        cards_by_bucket.setdefault(bucket, []).append(card)

        files[meta_path] = json.dumps(meta_payload, indent=2, ensure_ascii=False) + "\n"

        if library_meta is not None and bound.torrent_id:
            torbox_lib_id = f"aiostreams::library.torbox.torrent.{bound.torrent_id}"
            meta_lib_path = PurePosixPath("meta") / "library" / f"{torbox_lib_id}.json"
            files[meta_lib_path] = json.dumps(library_meta, indent=2, ensure_ascii=False) + "\n"

        for s_path, s_payload in stream_map.items():
            files[s_path] = json.dumps(s_payload, indent=2, ensure_ascii=False) + "\n"

    catalogs_manifest: list[JsonDict] = []
    for m_type, col_id in sorted(cards_by_bucket):
        metas = cards_by_bucket[(m_type, col_id)]
        cat_display = display_by_bucket[(m_type, col_id)]
        cat_path = PurePosixPath("catalog") / m_type / f"{col_id}.json"
        files[cat_path] = json.dumps({"metas": metas}, indent=2, ensure_ascii=False) + "\n"

        genres: list[str] = []
        for m in metas:
            for g in m.get("genres", []):
                if (
                    isinstance(g, str)
                    and g.lower() != cat_display.lower()
                    and g != "Education"
                    and g not in genres
                ):
                    genres.append(g)

        for g in genres:
            filtered = [m for m in metas if g in m.get("genres", [])]
            genre_path = PurePosixPath("catalog") / m_type / col_id / f"genre={g}.json"
            files[genre_path] = json.dumps({"metas": filtered}, indent=2, ensure_ascii=False) + "\n"

        cat_entry: JsonDict = {
            "type": m_type,
            "id": col_id,
            "name": cat_display,
        }
        if genres:
            cat_entry["extra"] = [
                {
                    "name": "genre",
                    "isRequired": False,
                    "options": genres,
                }
            ]
        catalogs_manifest.append(cat_entry)

    ordered_types = [t for t in ("series", "movie", "library") if t in active_types]
    manifest_payload: JsonDict = {
        "id": addon_config.addon_id,
        "version": addon_config.version,
        "name": addon_config.name,
        "description": addon_config.description,
        "logo": addon_config.logo,
        "resources": ["catalog", "meta", "stream"],
        "types": ordered_types,
        "idPrefixes": list(addon_config.id_prefixes),
        "catalogs": catalogs_manifest,
    }
    files[PurePosixPath("manifest.json")] = (
        json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n"
    )
    return VirtualFS(files=files)
