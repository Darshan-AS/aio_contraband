"""Pure Schema.org JSON-LD & OpenGraph metadata parser for arbitrary web URLs (courses, specials, movies, series)."""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence

from contraband.core.matcher import build_typographic_artwork
from contraband.core.types import (
    EpisodeMetadata,
    MediaItemMetadata,
    MediaType,
    TorrentFileEntry,
)
from contraband.providers.protocols import HtmlFetcher

# (collection_name, id_prefix, default_genre_or_none)
KNOWN_DOMAINS: Mapping[str, tuple[str, str, str | None]] = {
    "masterclass.com": ("MasterClass", "mc", "Education"),
    "wondrium.com": ("Wondrium", "won", "Education"),
    "thegreatcourses.com": ("The Great Courses", "tgc", "Education"),
    "coursera.org": ("Coursera", "crs", "Education"),
    "udemy.com": ("Udemy", "udm", "Education"),
    "edx.org": ("edX", "edx", "Education"),
    "ocw.mit.edu": ("MIT OpenCourseWare", "mit", "Education"),
    "youtube.com": ("YouTube", "yt", None),
    "vimeo.com": ("Vimeo", "vim", None),
    "imdb.com": ("Contraband", "imdb", None),
    "themoviedb.org": ("Contraband", "tmdb", None),
}

SERIES_SCHEMA_TYPES: frozenset[str] = frozenset(
    {"Course", "CreativeWorkSeries", "TVSeries", "VideoGallery", "ItemList"}
)
MOVIE_SCHEMA_TYPES: frozenset[str] = frozenset(
    {"Movie", "VideoObject", "ComedyEvent", "MusicEvent", "TVSpecial", "CreativeWork"}
)

JSON_LD_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _extract_meta_tag(raw_html: str, prop: str) -> str:
    escaped = re.escape(prop)
    m = re.search(
        rf'<meta\s+(?:property|name)=["\']{escaped}["\']\s+content=["\']([^"\']+)["\']',
        raw_html,
        re.IGNORECASE,
    ) or re.search(
        rf'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']{escaped}["\']',
        raw_html,
        re.IGNORECASE,
    )
    return html.unescape(m.group(1).strip()) if m else ""


def parse_web_media_html(
    raw_html: str,
    url: str,
    fallback_title: str = "",
    *,
    video_file_count: int = 0,
) -> MediaItemMetadata:
    """
    Pure function: extracts `MediaItemMetadata` from any webpage's Schema.org JSON-LD
    (`Course`, `TVSeries`, `Movie`, `VideoObject`, `ComedyEvent`, etc.) and OpenGraph meta tags.
    """
    parsed_url = urllib.parse.urlparse(url)
    host = parsed_url.netloc.lower().removeprefix("www.")
    collection_name, prefix, domain_genre = KNOWN_DOMAINS.get(
        host, ("Contraband", "web", None)
    )

    title = _extract_meta_tag(raw_html, "og:title") or fallback_title or url
    description = _extract_meta_tag(raw_html, "og:description") or _extract_meta_tag(
        raw_html, "description"
    )
    image = _extract_meta_tag(raw_html, "og:image") or _extract_meta_tag(
        raw_html, "twitter:image"
    )
    site_name = _extract_meta_tag(raw_html, "og:site_name")
    if collection_name == "Contraband" and site_name:
        collection_name = site_name

    creator = ""
    episodes: list[EpisodeMetadata] = []
    detected_schema_type = ""
    extra_genres: list[str] = []

    for ld_match in JSON_LD_SCRIPT_RE.finditer(raw_html):
        try:
            data = json.loads(ld_match.group(1).strip())
        except Exception:
            continue
        items = (
            data
            if isinstance(data, list)
            else (data.get("@graph", [data]) if isinstance(data, dict) else [])
        )
        for obj in items:
            if not isinstance(obj, dict):
                continue
            otype = str(obj.get("@type", ""))
            if otype in SERIES_SCHEMA_TYPES or otype in MOVIE_SCHEMA_TYPES:
                detected_schema_type = otype
                title = str(obj.get("name") or title)
                description = str(obj.get("description") or description)
                raw_creator = (
                    obj.get("instructor")
                    or obj.get("director")
                    or obj.get("performer")
                    or obj.get("author")
                    or obj.get("creator")
                )
                if isinstance(raw_creator, dict):
                    creator = str(raw_creator.get("name", ""))
                elif isinstance(raw_creator, list) and raw_creator and isinstance(raw_creator[0], dict):
                    creator = str(raw_creator[0].get("name", ""))

                raw_genre = obj.get("genre")
                if isinstance(raw_genre, str) and raw_genre:
                    extra_genres.append(raw_genre)
                elif isinstance(raw_genre, list):
                    extra_genres.extend(str(g) for g in raw_genre if isinstance(g, str))

                parts = (
                    obj.get("hasPart")
                    or obj.get("syllabusSections")
                    or obj.get("episodes")
                    or obj.get("itemListElement")
                    or []
                )
                if isinstance(parts, list):
                    for idx, part in enumerate(parts, start=1):
                        if isinstance(part, dict) and part.get("name"):
                            episodes.append(
                                EpisodeMetadata(
                                    number=idx,
                                    title=str(part["name"]),
                                    description=str(part.get("description", "")),
                                    thumbnail=image,
                                )
                            )

    if detected_schema_type == "Course" and "Education" not in extra_genres:
        extra_genres.append("Education")
    elif domain_genre and domain_genre not in extra_genres:
        extra_genres.append(domain_genre)

    genres: list[str] = [collection_name]
    for g in extra_genres:
        if g not in genres:
            genres.append(g)

    media_type: MediaType
    if episodes or video_file_count > 1 or detected_schema_type in SERIES_SCHEMA_TYPES:
        media_type = "series"
    elif detected_schema_type in MOVIE_SCHEMA_TYPES and video_file_count == 1:
        media_type = "movie"
    else:
        media_type = "series"

    short_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    poster, backdrop, thumb = build_typographic_artwork(title, creator or collection_name)

    return MediaItemMetadata(
        stremio_id=f"sw_{prefix}_{short_hash}",
        collection=collection_name,
        title=title,
        creator=creator,
        description=description,
        poster=image or poster,
        background=image or backdrop,
        episode_thumbnail=image or thumb,
        genres=tuple(genres),
        episodes=tuple(episodes),
        slug=short_hash,
        media_type=media_type,
    )


# Backward-compatible alias
parse_web_course_html = parse_web_media_html


class SchemaOrgMetadataProvider:
    """`MetadataProviderProtocol` implementation for arbitrary web URLs (courses, specials, movies, series)."""

    @property
    def name(self) -> str:
        return "Web Metadata"

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
        return bool(explicit_url and explicit_url.startswith(("http://", "https://")))

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
        _ = torrent_id, explicit_slug
        if not explicit_url:
            return None
        raw_html = fetch_html(explicit_url)
        if not raw_html:
            return None
        video_count = sum(1 for f in files if f.is_video)
        return parse_web_media_html(
            raw_html,
            explicit_url,
            fallback_title=display_title,
            video_file_count=video_count,
        )
