"""Contraband metadata provider pipeline and default provider chain."""

from __future__ import annotations

from collections.abc import Sequence

from contraband.core.types import MediaItemMetadata, TorrentFileEntry
from contraband.providers.metadata.fallback import (
    FilenameFallbackProvider,
    has_known_collection_keyword,
)
from contraband.providers.metadata.peterson import PetersonAcademyParser
from contraband.providers.metadata.schema_org import SchemaOrgMetadataProvider
from contraband.providers.protocols import HtmlFetcher, MetadataProviderProtocol

DEFAULT_METADATA_PROVIDERS: tuple[MetadataProviderProtocol, ...] = (
    PetersonAcademyParser(),
    SchemaOrgMetadataProvider(),
    FilenameFallbackProvider(),
)


def resolve_media_metadata(
    display_title: str,
    files: Sequence[TorrentFileEntry],
    fetch_html: HtmlFetcher,
    *,
    parsers: Sequence[MetadataProviderProtocol] = DEFAULT_METADATA_PROVIDERS,
    torrent_id: int = 0,
    explicit_url: str | None = None,
    explicit_slug: str | None = None,
    allow_fallback: bool = False,
) -> MediaItemMetadata | None:
    """Runs the registered `MetadataProviderProtocol` chain and returns normalized `MediaItemMetadata`."""
    use_fallback = bool(allow_fallback or has_known_collection_keyword(display_title))
    active_parsers = (
        parsers if use_fallback else tuple(p for p in parsers if not p.is_fallback)
    )
    for parser in active_parsers:
        if parser.can_handle(
            display_title,
            explicit_url=explicit_url,
            explicit_slug=explicit_slug,
        ):
            meta = parser.resolve_metadata(
                display_title,
                files,
                fetch_html,
                torrent_id=torrent_id,
                explicit_url=explicit_url,
                explicit_slug=explicit_slug,
            )
            if meta is not None:
                return meta
    return None
