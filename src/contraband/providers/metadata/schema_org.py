"""Pure Schema.org JSON-LD & OpenGraph metadata parser using stdlib `html.parser`."""

import hashlib
import json
import urllib.parse
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser

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


class _HeadAndJsonLdParser(HTMLParser):
    """Stdlib DOM-tokenizing extractor for `<meta>`, `<title>`, and `<script type="application/ld+json">`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_tags: dict[str, str] = {}
        self.json_ld_blocks: list[str] = []
        self.page_title: str = ""
        self._in_json_ld = False
        self._in_title = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower_tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if lower_tag == "meta":
            content = attr_map.get("content", "").strip()
            if content:
                for key_attr in ("property", "name", "itemprop"):
                    key_val = attr_map.get(key_attr, "").strip().lower()
                    if key_val and key_val not in self.meta_tags:
                        self.meta_tags[key_val] = content
        elif lower_tag == "script":
            script_type = attr_map.get("type", "").split(";", 1)[0].strip().lower()
            if script_type == "application/ld+json":
                self._in_json_ld = True
                self._buf = []
        elif lower_tag == "title" and not self.page_title:
            self._in_title = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            block = "".join(self._buf).strip()
            if block:
                self.json_ld_blocks.append(block)
            self._buf = []
        elif lower_tag == "title" and self._in_title:
            self._in_title = False
            self.page_title = " ".join("".join(self._buf).split()).strip()
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld or self._in_title:
            self._buf.append(data)


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

    parser = _HeadAndJsonLdParser()
    parser.feed(raw_html)
    parser.close()

    meta_map = parser.meta_tags
    title = (
        meta_map.get("og:title")
        or meta_map.get("twitter:title")
        or parser.page_title
        or fallback_title
        or url
    )
    description = (
        meta_map.get("og:description")
        or meta_map.get("twitter:description")
        or meta_map.get("description")
        or ""
    )
    image = meta_map.get("og:image") or meta_map.get("twitter:image") or ""
    site_name = meta_map.get("og:site_name") or ""
    if collection_name == "Contraband" and site_name:
        collection_name = site_name

    creator = ""
    episodes: list[EpisodeMetadata] = []
    detected_schema_type = ""
    extra_genres: list[str] = []

    for raw_block in parser.json_ld_blocks:
        try:
            data = json.loads(raw_block)
        except ValueError:
            continue
        items = (
            data
            if isinstance(data, list)
            else (data.get("@graph", [data]) if isinstance(data, dict) else [])
        )
        for obj in items:
            if not isinstance(obj, dict):
                continue
            raw_type = obj.get("@type", "")
            type_candidates = (
                [str(t) for t in raw_type] if isinstance(raw_type, list) else [str(raw_type)]
            )
            matched_type = next(
                (t for t in type_candidates if t in SERIES_SCHEMA_TYPES or t in MOVIE_SCHEMA_TYPES),
                "",
            )
            if matched_type:
                detected_schema_type = matched_type
                title = str(obj.get("name") or title)
                description = str(obj.get("description") or description)
                raw_creator = (
                    obj.get("instructor")
                    or obj.get("director")
                    or obj.get("performer")
                    or obj.get("author")
                    or obj.get("creator")
                )
                if isinstance(raw_creator, str) and raw_creator.strip():
                    creator = raw_creator.strip()
                elif isinstance(raw_creator, dict):
                    creator = str(raw_creator.get("name", ""))
                elif isinstance(raw_creator, list) and raw_creator:
                    first_creator = raw_creator[0]
                    if isinstance(first_creator, dict):
                        creator = str(first_creator.get("name", ""))
                    elif isinstance(first_creator, str):
                        creator = first_creator.strip()

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
                        if not isinstance(part, dict):
                            continue
                        part_item = (
                            part.get("item")
                            if isinstance(part.get("item"), dict) and not part.get("name")
                            else part
                        )
                        if isinstance(part_item, dict) and part_item.get("name"):
                            episodes.append(
                                EpisodeMetadata(
                                    number=idx,
                                    title=str(part_item["name"]),
                                    description=str(part_item.get("description", "")),
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
