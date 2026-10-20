"""Structural `typing.Protocol` interfaces for Contraband metadata and Debrid providers."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from contraband.core.errors import NetworkError
from contraband.core.result import Result
from contraband.core.types import (
    DebridAddOutcome,
    DebridCacheStatus,
    DebridQueuedItem,
    DebridTorrentItem,
    MediaItemMetadata,
    TorrentFileEntry,
)

type HtmlFetcher = Callable[[str], str | None]


class MetadataProviderProtocol(Protocol):
    """
    Pure/extensible media metadata provider protocol for Contraband items
    (courses, lectures, stand-up specials, documentaries, concerts, unlisted series).
    """

    @property
    def name(self) -> str: ...

    @property
    def is_fallback(self) -> bool: ...

    def can_handle(
        self,
        display_title: str,
        explicit_url: str | None = None,
        explicit_slug: str | None = None,
    ) -> bool: ...

    def resolve_metadata(
        self,
        display_title: str,
        files: Sequence[TorrentFileEntry],
        fetch_html: HtmlFetcher,
        *,
        torrent_id: int = 0,
        explicit_url: str | None = None,
        explicit_slug: str | None = None,
    ) -> MediaItemMetadata | None: ...


class DebridProviderProtocol(Protocol):
    """Protocol for cloud Debrid services (TorBox, Real-Debrid, AllDebrid, etc.)."""

    @property
    def name(self) -> str: ...

    @property
    def max_active_downloads(self) -> int: ...

    def check_cached(self, info_hash: str) -> Result[DebridCacheStatus, NetworkError]: ...

    def list_library(self) -> Result[tuple[DebridTorrentItem, ...], NetworkError]: ...

    def list_queued(self) -> Result[tuple[DebridQueuedItem, ...], NetworkError]: ...

    def fetch_torrent_files(
        self, torrent_id: int
    ) -> Result[tuple[TorrentFileEntry, ...], NetworkError]: ...

    def add_magnet(
        self,
        magnet_uri: str,
        *,
        name: str | None = None,
        as_queued: bool = False,
    ) -> Result[DebridAddOutcome, NetworkError]: ...

    def add_torrent_file(
        self,
        torrent_path: Path,
        file_bytes: bytes,
        *,
        as_queued: bool = False,
    ) -> Result[DebridAddOutcome, NetworkError]: ...

    def start_queued_item(self, queued_id: int) -> Result[bool, NetworkError]: ...
