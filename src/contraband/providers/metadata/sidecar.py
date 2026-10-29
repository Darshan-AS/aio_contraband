"""Local `.toml` / `.json` sidecar metadata provider for custom offline courses, specials, and archives."""

import json
import re
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from contraband.core.matcher import build_typographic_artwork
from contraband.core.types import (
    EpisodeMetadata,
    MediaItemMetadata,
    MediaType,
    TorrentFileEntry,
)
from contraband.providers.protocols import HtmlFetcher

MAX_SIDECAR_BYTES = 2 * 1024 * 1024  # 2 MiB upper bound against oversized inputs


def _extract_sidecar_path(explicit_url: str | None) -> Path | None:
    if not explicit_url:
        return None
    cleaned = explicit_url.strip()
    if "\x00" in cleaned or cleaned.startswith(("http://", "https://")):
        return None
    if cleaned.startswith("file://"):
        parsed = urllib.parse.urlparse(cleaned)
        candidate = Path(urllib.parse.unquote(parsed.path)).expanduser()
    else:
        candidate = Path(cleaned).expanduser()
    if candidate.suffix.lower() not in (".toml", ".json"):
        return None
    return candidate


def parse_sidecar_metadata(
    content: str,
    *,
    fmt: Literal["toml", "json"],
    fallback_title: str = "",
    files: Sequence[TorrentFileEntry] = (),
) -> MediaItemMetadata | None:
    """Purely parses a `.toml` or `.json` sidecar string into `MediaItemMetadata`."""
    try:
        raw_doc: Any = tomllib.loads(content) if fmt == "toml" else json.loads(content)
    except (ValueError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(raw_doc, Mapping):
        return None

    meta_sec = raw_doc.get("metadata")
    sec: Mapping[str, Any] = meta_sec if isinstance(meta_sec, Mapping) else raw_doc

    title = str(sec.get("title") or fallback_title).strip()
    if not title:
        return None

    collection = str(sec.get("collection") or "Contraband").strip() or "Contraband"
    creator = str(sec.get("creator") or sec.get("instructor") or sec.get("director") or "").strip()
    code = str(sec.get("code") or "").strip()
    description = str(sec.get("description") or "").strip()

    raw_slug = str(sec.get("slug") or "").strip()
    slug = (
        re.sub(r"[^a-z0-9]+", "-", (raw_slug or title).lower()).strip("-")[:48]
        or "custom-item"
    )
    safe_id_suffix = slug.replace("-", "_")
    raw_stremio_id = str(sec.get("stremio_id") or sec.get("id") or "").strip()
    stremio_id = raw_stremio_id or f"sw_custom_{safe_id_suffix}"

    default_poster, default_backdrop, default_thumb = build_typographic_artwork(
        title, creator or collection
    )
    poster = str(sec.get("poster") or "").strip() or default_poster
    background = str(sec.get("background") or "").strip() or default_backdrop
    episode_thumbnail = str(sec.get("episode_thumbnail") or "").strip() or default_thumb

    raw_episodes = sec.get("episodes") or raw_doc.get("episodes")
    episodes: list[EpisodeMetadata] = []
    if isinstance(raw_episodes, list):
        for idx, ep in enumerate(raw_episodes, start=1):
            if not isinstance(ep, Mapping):
                continue
            ep_title = str(ep.get("title") or "").strip()
            if not ep_title:
                continue
            ep_num_raw = ep.get("number", idx)
            ep_num = int(ep_num_raw) if isinstance(ep_num_raw, int) else idx
            season_raw = ep.get("season", 1)
            season = int(season_raw) if isinstance(season_raw, int) and season_raw >= 1 else 1
            dur_raw = ep.get("duration_seconds", 0)
            dur = int(dur_raw) if isinstance(dur_raw, int) and dur_raw >= 0 else 0
            episodes.append(
                EpisodeMetadata(
                    number=ep_num,
                    title=ep_title,
                    description=str(ep.get("description") or "").strip(),
                    duration_seconds=dur,
                    thumbnail=str(ep.get("thumbnail") or "").strip() or episode_thumbnail,
                    season=season,
                )
            )

    raw_genres = sec.get("genres")
    genres: list[str] = [collection]
    if isinstance(raw_genres, list):
        for g in raw_genres:
            g_str = str(g).strip()
            if g_str and g_str not in genres:
                genres.append(g_str)

    raw_mtype = str(sec.get("media_type") or sec.get("type") or "").strip().lower()
    video_count = sum(1 for f in files if f.is_video)
    media_type: MediaType
    if raw_mtype in ("movie", "series"):
        media_type = "movie" if raw_mtype == "movie" else "series"
    elif episodes or video_count > 1:
        media_type = "series"
    elif video_count == 1:
        media_type = "movie"
    else:
        media_type = "series"

    return MediaItemMetadata(
        stremio_id=stremio_id,
        collection=collection,
        title=title,
        creator=creator,
        code=code,
        description=description,
        poster=poster,
        background=background,
        episode_thumbnail=episode_thumbnail,
        genres=tuple(genres),
        episodes=tuple(episodes),
        slug=slug,
        media_type=media_type,
    )


class SidecarMetadataProvider:
    """`MetadataProviderProtocol` for local `.toml` and `.json` metadata sidecars."""

    @property
    def name(self) -> str:
        return "Local Sidecar"

    @property
    def is_fallback(self) -> bool:
        return False

    def can_handle(
        self,
        display_title: str,
        explicit_url: str | None = None,
        explicit_slug: str | None = None,
    ) -> bool:
        _ = display_title, explicit_slug
        return _extract_sidecar_path(explicit_url) is not None

    def resolve_metadata(
        self,
        display_title: str,
        files: Sequence[TorrentFileEntry],
        fetch_html: HtmlFetcher,
        *,
        torrent_id: int = 0,
        explicit_url: str | None = None,
        explicit_slug: str | None = None,
    ) -> MediaItemMetadata | None:
        _ = fetch_html, torrent_id, explicit_slug
        sidecar_path = _extract_sidecar_path(explicit_url)
        if sidecar_path is None:
            return None
        try:
            resolved = sidecar_path.resolve()
            if not resolved.is_file():
                return None
            if resolved.stat().st_size > MAX_SIDECAR_BYTES:
                return None
            content = resolved.read_text(encoding="utf-8")
        except OSError:
            return None
        fmt: Literal["toml", "json"] = (
            "toml" if resolved.suffix.lower() == ".toml" else "json"
        )
        return parse_sidecar_metadata(
            content,
            fmt=fmt,
            fallback_title=display_title,
            files=files,
        )
