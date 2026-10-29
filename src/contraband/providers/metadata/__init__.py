"""Contraband metadata provider pipeline, sidecar/Archive.org support, and entry-point plugin discovery."""

import importlib.metadata
from collections.abc import Sequence

from contraband.core.types import MediaItemMetadata, TorrentFileEntry
from contraband.providers.metadata.archive_org import InternetArchiveMetadataProvider
from contraband.providers.metadata.fallback import (
    FilenameFallbackProvider,
    has_known_collection_keyword,
)
from contraband.providers.metadata.peterson import PetersonAcademyParser
from contraband.providers.metadata.schema_org import SchemaOrgMetadataProvider
from contraband.providers.metadata.sidecar import SidecarMetadataProvider
from contraband.providers.protocols import HtmlFetcher, MetadataProviderProtocol

PLUGIN_ENTRY_POINT_GROUP = "contraband.metadata_providers"

BUILTIN_PRIMARY_PROVIDERS: tuple[MetadataProviderProtocol, ...] = (
    SidecarMetadataProvider(),
    PetersonAcademyParser(),
    InternetArchiveMetadataProvider(),
    SchemaOrgMetadataProvider(),
)


def discover_plugin_metadata_providers() -> tuple[MetadataProviderProtocol, ...]:
    """
    Discovers locally installed third-party `MetadataProviderProtocol` plugins
    registered under the `contraband.metadata_providers` Python entry-point group.
    """
    discovered: list[MetadataProviderProtocol] = []
    for ep in importlib.metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP):
        try:
            loaded = ep.load()
            instance = loaded() if isinstance(loaded, type) else loaded
            if callable(getattr(instance, "can_handle", None)) and callable(
                getattr(instance, "resolve_metadata", None)
            ):
                discovered.append(instance)
        except Exception:
            continue
    return tuple(discovered)


def load_metadata_providers(*, include_plugins: bool = True) -> tuple[MetadataProviderProtocol, ...]:
    """Returns the ordered metadata provider chain with `FilenameFallbackProvider` last."""
    plugins = discover_plugin_metadata_providers() if include_plugins else ()
    return (
        *BUILTIN_PRIMARY_PROVIDERS,
        *plugins,
        FilenameFallbackProvider(),
    )


DEFAULT_METADATA_PROVIDERS: tuple[MetadataProviderProtocol, ...] = load_metadata_providers(
    include_plugins=False
)


def resolve_media_metadata(
    display_title: str,
    files: Sequence[TorrentFileEntry],
    fetch_html: HtmlFetcher,
    *,
    parsers: Sequence[MetadataProviderProtocol] | None = None,
    torrent_id: int = 0,
    explicit_url: str | None = None,
    explicit_slug: str | None = None,
    allow_fallback: bool = False,
) -> MediaItemMetadata | None:
    """Runs the registered `MetadataProviderProtocol` chain and returns normalized `MediaItemMetadata`."""
    resolved_parsers = parsers if parsers is not None else load_metadata_providers()
    use_fallback = bool(allow_fallback or has_known_collection_keyword(display_title))
    active_parsers = (
        resolved_parsers
        if use_fallback
        else tuple(p for p in resolved_parsers if not p.is_fallback)
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
