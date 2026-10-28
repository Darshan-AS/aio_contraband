"""Zero-network fallback provider that builds structured metadata from any torrent's filenames."""

import hashlib
import re
from collections.abc import Mapping, Sequence

from contraband.core.matcher import (
    build_typographic_artwork,
    clean_video_stem,
    extract_episode_number_from_filename,
    match_files_to_episodes,
)
from contraband.core.types import (
    EpisodeMetadata,
    MediaItemMetadata,
    MediaType,
    TorrentFileEntry,
)
from contraband.providers.protocols import HtmlFetcher

# keyword -> (collection_name, id_prefix, default_genre_or_none, is_educational)
COLLECTION_KEYWORDS: Mapping[str, tuple[str, str, str | None, bool]] = {
    "masterclass": ("MasterClass", "mc", "Education", True),
    "the great courses": ("The Great Courses", "tgc", "Education", True),
    "wondrium": ("Wondrium", "won", "Education", True),
    "ttc": ("The Great Courses", "tgc", "Education", True),
    "peterson academy": ("Peterson Academy", "pa", "Education", True),
    "coursera": ("Coursera", "crs", "Education", True),
    "udemy": ("Udemy", "udm", "Education", True),
    "mit ocw": ("MIT OpenCourseWare", "mit", "Education", True),
    "bbc": ("BBC Documentaries", "bbc", "Documentary", False),
    "pbs": ("PBS Documentaries", "pbs", "Documentary", False),
    "curiositystream": ("Curiosity Stream", "cts", "Documentary", False),
    "stand-up": ("Stand-Up Specials", "su", "Comedy", False),
    "standup": ("Stand-Up Specials", "su", "Comedy", False),
}

RELEASE_NOISE_RE = re.compile(
    r"\b(EDU4U|1080p|720p|2160p|4K|WEB-DL|WEBRip|BluRay|x264|x265|HEVC|AAC|MP4|MKV)\b.*",
    re.IGNORECASE,
)


def has_known_collection_keyword(display_title: str) -> bool:
    """Returns True if `display_title` contains any recognized Contraband collection keyword."""
    lowered = display_title.lower()
    return any(kw in lowered for kw in COLLECTION_KEYWORDS)


def build_fallback_media_metadata(
    display_title: str,
    files: Sequence[TorrentFileEntry],
    torrent_id: int = 0,
) -> MediaItemMetadata:
    """
    Pure function: constructs `MediaItemMetadata` from a torrent title and its video file entries.
    Uses educational phrasing only when matched against an educational collection; uses neutral
    Contraband phrasing and automatic `movie` vs `series` detection otherwise.
    """
    cleaned = RELEASE_NOISE_RE.sub("", display_title).strip(" -_.")

    collection_name = "Contraband"
    prefix = "tb"
    default_genre: str | None = None
    is_edu = False

    lower_title = cleaned.lower()
    for kw, (cname, pfx, genre, edu_flag) in COLLECTION_KEYWORDS.items():
        if kw in lower_title:
            collection_name, prefix, default_genre, is_edu = cname, pfx, genre, edu_flag
            break

    parts = [p.strip() for p in re.split(r"\s+-\s+|\s+–\s+", cleaned) if p.strip()]
    creator = ""
    if len(parts) >= 3:
        item_title = parts[1]
        creator = parts[2]
    elif len(parts) == 2:
        if parts[0].lower() in COLLECTION_KEYWORDS:
            item_title = parts[1]
        else:
            item_title, creator = parts[0], parts[1]
    else:
        item_title = cleaned or display_title

    poster, backdrop, thumb = build_typographic_artwork(item_title, creator or collection_name)
    ep_matches = match_files_to_episodes(files, ())

    ep_label = "Lecture" if is_edu else "Part"
    episodes = tuple(
        EpisodeMetadata(
            number=m.episode_number,
            title=clean_video_stem(m.filename),
            description=f"{ep_label} {m.episode_number} — {item_title}"
            + (f" ({creator})" if creator else ""),
            thumbnail=thumb,
            season=m.season,
        )
        for m in ep_matches
    )

    video_files = [f for f in files if f.is_video]
    has_explicit_ep_num = (
        len(video_files) == 1
        and extract_episode_number_from_filename(video_files[0].short_name) is not None
    )
    media_type: MediaType = (
        "movie"
        if (not is_edu and len(video_files) == 1 and not has_explicit_ep_num)
        else "series"
    )

    if is_edu:
        description = f"{item_title}" + (f" taught by {creator}." if creator else ".")
    else:
        description = f"{item_title}" + (f" — {creator}." if creator else ".")

    genres: tuple[str, ...] = (
        (collection_name, default_genre)
        if default_genre and default_genre != collection_name
        else (collection_name,)
    )

    id_suffix = (
        str(torrent_id)
        if torrent_id
        else hashlib.sha1(display_title.encode("utf-8")).hexdigest()[:8]
    )
    slug = re.sub(r"[^a-z0-9]+", "-", item_title.lower()).strip("-")

    return MediaItemMetadata(
        stremio_id=f"sw_{prefix}_{id_suffix}",
        collection=collection_name,
        title=item_title,
        creator=creator,
        description=description,
        poster=poster,
        background=backdrop,
        episode_thumbnail=thumb,
        genres=genres,
        episodes=episodes,
        slug=slug,
        media_type=media_type,
    )


class FilenameFallbackProvider:
    """Zero-config fallback `MetadataProviderProtocol` implementation."""

    @property
    def name(self) -> str:
        return "Filename Fallback"

    @property
    def is_fallback(self) -> bool:
        return True

    def can_handle(
        self,
        display_title: str,
        explicit_url: str | None = None,
        explicit_slug: str | None = None,
    ) -> bool:
        _ = display_title, explicit_url, explicit_slug
        return True

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
        _ = fetch_html, explicit_url, explicit_slug
        return build_fallback_media_metadata(display_title, files, torrent_id=torrent_id)
