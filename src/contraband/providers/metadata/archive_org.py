"""Pure Internet Archive (`archive.org`) metadata provider for public-domain films, lectures, and collections."""

import json
import re
import urllib.parse
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Any

from contraband.core.matcher import build_typographic_artwork, clean_video_stem
from contraband.core.types import (
     VIDEO_EXTENSIONS,
    EpisodeMetadata,
    MediaItemMetadata,
    MediaType,
    TorrentFileEntry,
)
from contraband.providers.protocols import HtmlFetcher


class _PlainTextExtractor(HTMLParser):
    """Strips HTML tags from Internet Archive description fields."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join("".join(self._parts).split()).strip()


def _strip_html(raw: str) -> str:
    if "<" not in raw:
        return " ".join(raw.split()).strip()
    extractor = _PlainTextExtractor()
    extractor.feed(raw)
    extractor.close()
    return extractor.get_text()


def extract_archive_org_identifier(
    explicit_url: str | None = None,
    explicit_slug: str | None = None,
) -> str | None:
    """Extracts an Internet Archive item identifier from `https://archive.org/details/<id>` or `ia:<id>`."""
    if explicit_slug and explicit_slug.lower().startswith("ia:"):
        ident = explicit_slug[3:].strip().strip("/")
        if ident:
            return ident

    if explicit_url and explicit_url.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(explicit_url)
        host = parsed.netloc.lower().removeprefix("www.")
        if host == "archive.org":
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("details", "metadata", "download"):
                return urllib.parse.unquote(parts[1])
    return None


def _coerce_first_str(val: Any) -> str:
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _parse_duration_seconds(raw: Any) -> int:
    if isinstance(raw, (int, float)):
        return max(0, int(raw))
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return 0
        try:
            return max(0, int(float(cleaned)))
        except ValueError:
            parts = cleaned.split(":")
            if all(p.isdigit() for p in parts) and 2 <= len(parts) <= 3:
                acc = 0
                for p in parts:
                    acc = acc * 60 + int(p)
                return acc
    return 0


def parse_archive_org_metadata_json(
    raw_json: str,
    identifier: str,
    *,
    fallback_title: str = "",
    files: Sequence[TorrentFileEntry] = (),
) -> MediaItemMetadata | None:
    """Purely parses `https://archive.org/metadata/<identifier>` JSON into `MediaItemMetadata`."""
    try:
        payload = json.loads(raw_json)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    meta_obj = payload.get("metadata")
    if not isinstance(meta_obj, dict) or not meta_obj:
        return None

    title = _coerce_first_str(meta_obj.get("title")) or fallback_title or identifier
    creator = _coerce_first_str(meta_obj.get("creator")) or _coerce_first_str(
        meta_obj.get("director")
    )
    raw_desc = _coerce_first_str(meta_obj.get("description"))
    description = _strip_html(raw_desc) if raw_desc else ""

    subjects: list[str] = []
    raw_subject = meta_obj.get("subject")
    if isinstance(raw_subject, str):
        subjects.extend(s.strip() for s in re.split(r"[;,]", raw_subject) if s.strip())
    elif isinstance(raw_subject, list):
        for s in raw_subject:
            if isinstance(s, str):
                subjects.extend(part.strip() for part in re.split(r"[;,]", s) if part.strip())

    collection_name = "Internet Archive"
    genres: list[str] = [collection_name]
    for subj in subjects[:4]:
        title_cased = subj if any(c.isupper() for c in subj) else subj.title()
        if title_cased not in genres:
            genres.append(title_cased)

    quoted_id = urllib.parse.quote(identifier, safe="")
    ia_thumb = f"https://archive.org/services/img/{quoted_id}"
    _, backdrop, ep_thumb = build_typographic_artwork(title, creator or collection_name)

    episodes: list[EpisodeMetadata] = []
    ia_files = payload.get("files")
    if isinstance(ia_files, list):
        original_videos = [
            f
            for f in ia_files
            if isinstance(f, dict)
            and str(f.get("source", "original")).lower() == "original"
            and str(f.get("name", "")).lower().endswith(VIDEO_EXTENSIONS)
        ]
        candidate_videos = original_videos or [
            f
            for f in ia_files
            if isinstance(f, dict)
            and str(f.get("name", "")).lower().endswith(VIDEO_EXTENSIONS)
        ]
        if len(candidate_videos) > 1:
            sorted_ia_videos = sorted(candidate_videos, key=lambda x: str(x.get("name", "")))
            for idx, vf in enumerate(sorted_ia_videos, start=1):
                raw_vtitle = str(vf.get("title") or "").strip()
                vname = str(vf.get("name") or f"Part {idx}")
                ep_title = raw_vtitle or clean_video_stem(vname.rsplit("/", 1)[-1])
                episodes.append(
                    EpisodeMetadata(
                        number=idx,
                        title=ep_title,
                        duration_seconds=_parse_duration_seconds(vf.get("length")),
                        thumbnail=ia_thumb,
                    )
                )

    local_videos = [f for f in files if f.is_video]
    if not episodes and len(local_videos) > 1:
        for idx, lf in enumerate(sorted(local_videos, key=lambda x: x.name), start=1):
            episodes.append(
                EpisodeMetadata(
                    number=idx,
                    title=clean_video_stem(lf.short_name),
                    thumbnail=ia_thumb,
                )
            )

    video_count = max(
        len(episodes),
        len(local_videos),
        1
        if isinstance(ia_files, list)
        and any(
            isinstance(f, dict) and str(f.get("name", "")).lower().endswith(VIDEO_EXTENSIONS)
            for f in ia_files
        )
        else 0,
    )
    media_type: MediaType = "movie" if (not episodes and video_count == 1) else "series"
    safe_slug = re.sub(r"[^a-z0-9]+", "_", identifier.lower()).strip("_")[:48] or "item"

    return MediaItemMetadata(
        stremio_id=f"sw_ia_{safe_slug}",
        collection=collection_name,
        title=title,
        creator=creator,
        description=description,
        poster=ia_thumb,
        background=backdrop,
        episode_thumbnail=ia_thumb or ep_thumb,
        genres=tuple(genres),
        episodes=tuple(episodes),
        slug=identifier,
        media_type=media_type,
    )


class InternetArchiveMetadataProvider:
    """`MetadataProviderProtocol` for `https://archive.org/details/<id>` URLs and `ia:<id>` slugs."""

    @property
    def name(self) -> str:
        return "Internet Archive"

    @property
    def is_fallback(self) -> bool:
        return False

    def can_handle(
        self,
        display_title: str,
        explicit_url: str | None = None,
        explicit_slug: str | None = None,
    ) -> bool:
        _ = display_title
        return extract_archive_org_identifier(explicit_url, explicit_slug) is not None

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
        _ = torrent_id
        identifier = extract_archive_org_identifier(explicit_url, explicit_slug)
        if not identifier:
            return None
        api_url = f"https://archive.org/metadata/{urllib.parse.quote(identifier, safe='')}"
        raw_json = fetch_html(api_url)
        if not raw_json:
            return None
        return parse_archive_org_metadata_json(
            raw_json,
            identifier,
            fallback_title=display_title,
            files=files,
        )
