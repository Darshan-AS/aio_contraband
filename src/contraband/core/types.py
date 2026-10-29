"""Immutable domain data structures and PEP 742 TypeIs narrowing guards for Contraband."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, TypeIs

type JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
type JsonDict = dict[str, Any]
type EnvMap = Mapping[str, str]
type MediaType = Literal["series", "movie"]

VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v")
DEFAULT_CONTRABAND_LOGO: str = "https://placehold.co/256x256/111827/f9fafb/png?text=CB"


def is_json_mapping(val: Any) -> TypeIs[Mapping[str, Any]]:
    """PEP 742 bidirectional type guard narrowing arbitrary values to `Mapping[str, Any]`."""
    return isinstance(val, Mapping)


def is_json_dict(val: Any) -> TypeIs[JsonDict]:
    """PEP 742 bidirectional type guard narrowing arbitrary values to mutable `JsonDict`."""
    return isinstance(val, dict)


def is_json_list(val: Any) -> TypeIs[list[Any]]:
    """PEP 742 bidirectional type guard narrowing arbitrary values to `list[Any]`."""
    return isinstance(val, list)


@dataclass(frozen=True, slots=True)
class EpisodeMetadata:
    """Immutable metadata for a single episode, lecture, or part."""

    number: int
    title: str
    description: str = ""
    duration_seconds: int = 0
    thumbnail: str = ""
    season: int = 1


@dataclass(frozen=True, slots=True)
class MediaItemMetadata:
    """Immutable normalized metadata for any Contraband media item (course, series, special, documentary, movie)."""

    stremio_id: str
    collection: str
    title: str
    creator: str = ""
    code: str = ""
    description: str = ""
    poster: str = ""
    background: str = ""
    episode_thumbnail: str = ""
    genres: tuple[str, ...] = ()
    episodes: tuple[EpisodeMetadata, ...] = ()
    slug: str = ""
    media_type: MediaType = "series"

    def with_collection(self, new_collection: str) -> MediaItemMetadata:
        """Returns a new `MediaItemMetadata` assigned to `new_collection`."""
        preserved_genres = tuple(
            g
            for g in self.genres
            if g not in (self.collection, new_collection, "Education")
        )
        updated_genres = (new_collection, *preserved_genres)
        return MediaItemMetadata(
            stremio_id=self.stremio_id,
            collection=new_collection,
            title=self.title,
            creator=self.creator,
            code=self.code,
            description=self.description,
            poster=self.poster,
            background=self.background,
            episode_thumbnail=self.episode_thumbnail,
            genres=updated_genres,
            episodes=self.episodes,
            slug=self.slug,
            media_type=self.media_type,
        )

    def with_media_type(self, new_type: MediaType) -> MediaItemMetadata:
        """Returns a new `MediaItemMetadata` with an explicit `media_type` (`'series'` or `'movie'`)."""
        return MediaItemMetadata(
            stremio_id=self.stremio_id,
            collection=self.collection,
            title=self.title,
            creator=self.creator,
            code=self.code,
            description=self.description,
            poster=self.poster,
            background=self.background,
            episode_thumbnail=self.episode_thumbnail,
            genres=self.genres,
            episodes=self.episodes,
            slug=self.slug,
            media_type=new_type,
        )


@dataclass(frozen=True, slots=True)
class EpisodeFileMatch:
    """Maps a 1-based episode number (and season) to a Debrid file index and short filename."""

    episode_number: int
    file_index: int
    filename: str
    season: int = 1


@dataclass(frozen=True, slots=True)
class BoundMediaItem:
    """A media item bound to a specific BitTorrent infoHash, Debrid torrent ID, and file map."""

    metadata: MediaItemMetadata
    info_hash: str
    torrent_id: int = 0
    episode_matches: tuple[EpisodeFileMatch, ...] = ()


@dataclass(frozen=True, slots=True)
class AddonConfig:
    """Static Stremio addon manifest metadata."""

    addon_id: str = "dev.darshanas.contraband"
    name: str = "Contraband"
    version: str = "1.5.0"
    description: str = "Curated niche courses, rare lectures, and unlisted specials"
    logo: str = DEFAULT_CONTRABAND_LOGO
    id_prefixes: tuple[str, ...] = ("sw_", "pa_")
    emit_library_meta: bool = False


@dataclass(frozen=True, slots=True)
class CatalogIndex:
    """Canonical immutable state of all media items published in the static addon."""

    items: tuple[BoundMediaItem, ...] = ()


@dataclass(frozen=True, slots=True)
class VirtualFS:
    """Immutable in-memory representation of a generated directory tree."""

    files: Mapping[PurePosixPath, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CatalogAuditReport:
    """Immutable health and consistency audit report for a Contraband static addon."""

    total_items: int
    series_count: int
    movie_count: int
    unlinked_items: tuple[BoundMediaItem, ...] = ()
    missing_on_debrid: tuple[BoundMediaItem, ...] = ()
    orphaned_files: tuple[PurePosixPath, ...] = ()
    missing_files: tuple[PurePosixPath, ...] = ()
    stale_files: tuple[PurePosixPath, ...] = ()

    @property
    def has_fs_drift(self) -> bool:
        """True if any static files on disk are orphaned, missing, or out-of-sync with `CatalogIndex`."""
        return bool(self.orphaned_files or self.missing_files or self.stale_files)

    @property
    def is_healthy(self) -> bool:
        """True if all items are linked, present on Debrid (if checked), and static files match `CatalogIndex`."""
        return not (self.unlinked_items or self.missing_on_debrid or self.has_fs_drift)


@dataclass(frozen=True, slots=True)
class TorrentFileEntry:
    """A single file entry inside a torrent or Debrid cloud item."""

    index: int
    name: str
    size_bytes: int = 0

    @property
    def short_name(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    @property
    def is_video(self) -> bool:
        return self.short_name.lower().endswith(VIDEO_EXTENSIONS)


@dataclass(frozen=True, slots=True)
class ParsedTorrent:
    """Metadata extracted purely from a `.torrent` file's bencoded bytes."""

    info_hash: str
    name: str
    total_bytes: int
    files: tuple[TorrentFileEntry, ...]

    @property
    def magnet_uri(self) -> str:
        return f"magnet:?xt=urn:btih:{self.info_hash}"

    @property
    def video_files(self) -> tuple[TorrentFileEntry, ...]:
        return tuple(f for f in self.files if f.is_video)


@dataclass(frozen=True, slots=True)
class MagnetTarget:
    """Normalized magnet link or infoHash target."""

    info_hash: str
    magnet_uri: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class DebridTorrentItem:
    """Normalized representation of a torrent in a Debrid cloud library."""

    id: int
    info_hash: str
    name: str
    size_bytes: int
    download_state: str
    progress: float
    download_finished: bool
    files: tuple[TorrentFileEntry, ...] = ()

    @property
    def video_count(self) -> int:
        return sum(1 for f in self.files if f.is_video)


@dataclass(frozen=True, slots=True)
class DebridQueuedItem:
    """Normalized representation of a queued torrent in a Debrid cloud queue."""

    id: int
    info_hash: str
    name: str


@dataclass(frozen=True, slots=True)
class DebridCacheStatus:
    """Cache status of an infoHash on a Debrid service."""

    info_hash: str
    is_cached: bool
    size_bytes: int = 0
    files: tuple[TorrentFileEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class DebridAddOutcome:
    """Result of adding a magnet or torrent file to a Debrid service."""

    detail: str
    torrent_id: int | None = None
    queued_id: int | None = None
    was_cached: bool = False
