"""Pure Peterson Academy course metadata parser and slug/subject matcher."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass

from contraband.core.types import (
    EpisodeMetadata,
    MediaItemMetadata,
    TorrentFileEntry,
)
from contraband.providers.protocols import HtmlFetcher

SUBJECT_BLOCK_RE = re.compile(r'\{label:"([^"]+)",icon:"[^"]+",courses:\[(.*?)\]\}')
CATALOG_ENTRY_RE = re.compile(
    r'\{id:(\d+),slug:"([^"]+)",title:"((?:\\.|[^"])*)",courseCode:"([^"]+)",instructor:"((?:\\.|[^"])*)"'
)
PREVIEW_COURSE_RE = re.compile(
    r'previewCourse:\{id:(\d+),title:"([^"]+)",description:"((?:\\.|[^"])*)",slug:"([^"]+)",label:[^,]+,courseCode:"([^"]+)"'
)
INSTRUCTOR_RE = re.compile(
    r'instructors:\[\{id:\d+,fullName:"([^"]+)",slug:"([^"]+)",title:"([^"]+)"'
)
TRAILER_MUX_RE = re.compile(r'trailerPlaybackId:"([^"]+)"')
LECTURE_RE = re.compile(
    r'\{id:"[^"]+",lectureId:(\d+),lectureNumber:(\d+),path:"([^"]+)",title:"((?:\\.|[^"])*)",description:"((?:\\.|[^"])*)",durationSeconds:(\d+),muxAssetId:"([^"]+)"'
)
RELEASE_TAGS_RE = re.compile(
    r"\b(EDU4U|1080p|720p|2160p|4K|WEB-DL|WEBRip|x264|x265|HEVC|AAC|MP4|MKV)\b.*",
    re.IGNORECASE,
)
STOP_WORDS: frozenset[str] = frozenset({"the", "of", "in", "to", "and", "dr", "lord", "part"})

SLUG_ALIASES: dict[str, str] = {
    "the-secrets-of-big-pharma": "the-pharmaceutical-industry-practices-incentives-and-patient-awareness",
    "secrets-of-big-pharma": "the-pharmaceutical-industry-practices-incentives-and-patient-awareness",
}


@dataclass(frozen=True, slots=True)
class PetersonCatalogEntry:
    """Immutable entry from Peterson Academy's `/courses` SSR catalog."""

    course_id: int
    slug: str
    title: str
    course_code: str
    instructor: str
    subject: str


def _unescape_js_string(val: str) -> str:
    try:
        return html.unescape(str(json.loads(f'"{val}"')))
    except Exception:
        return html.unescape(
            val.replace('\\"', '"')
            .replace("\\'", "'")
            .replace("\\n", "\n")
            .replace("\\\\", "\\")
        )


def parse_peterson_catalog_html(raw_html: str) -> tuple[PetersonCatalogEntry, ...]:
    """Pure function: parses `/courses` SSR hydration HTML into a tuple of catalog entries."""
    by_slug: dict[str, PetersonCatalogEntry] = {}

    for sb in SUBJECT_BLOCK_RE.finditer(raw_html):
        subject = sb.group(1)
        for em in CATALOG_ENTRY_RE.finditer(sb.group(2)):
            cid, slug, title, code, inst = em.groups()
            by_slug[slug] = PetersonCatalogEntry(
                course_id=int(cid),
                slug=slug,
                title=html.unescape(title),
                course_code=code,
                instructor=html.unescape(inst),
                subject=subject,
            )

    for em in CATALOG_ENTRY_RE.finditer(raw_html):
        cid, slug, title, code, inst = em.groups()
        if slug not in by_slug:
            by_slug[slug] = PetersonCatalogEntry(
                course_id=int(cid),
                slug=slug,
                title=html.unescape(title),
                course_code=code,
                instructor=html.unescape(inst),
                subject="Humanities",
            )

    return tuple(by_slug.values())


def _norm_tokens(text: str) -> set[str]:
    normalized = text.lower().replace("&", "and").replace("intro ", "introduction ")
    return {
        w
        for w in re.findall(r"[a-z0-9]{2,}", normalized)
        if w not in STOP_WORDS
    }


def infer_peterson_slug_and_subject(
    display_title: str,
    catalog: Sequence[PetersonCatalogEntry] = (),
    *,
    explicit_url: str | None = None,
    explicit_slug: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Pure function: resolves `(slug, subject)` from a display title, explicit URL, or explicit slug
    against a parsed catalog sequence.
    """
    subj_by_slug = {c.slug: c.subject for c in catalog}

    if explicit_url and "petersonacademy.com/courses/" in explicit_url.lower():
        path = urllib.parse.urlparse(explicit_url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1].lower()
        return slug, subj_by_slug.get(slug)

    if explicit_slug:
        slug = explicit_slug.strip().strip("/").lower()
        return slug, subj_by_slug.get(slug)

    cleaned = RELEASE_TAGS_RE.sub("", display_title).strip(" -_.")
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", cleaned) if p.strip()]
    if len(parts) >= 2 and "peterson" in parts[0].lower():
        course_name = parts[1]
    elif parts:
        course_name = re.sub(
            r"^peterson\s+academy\s*[:\-]?\s*",
            "",
            parts[0],
            flags=re.IGNORECASE,
        )
    else:
        return None, None

    raw_slug = re.sub(
        r"[^a-z0-9]+", "-", course_name.lower().replace("&", "and")
    ).strip("-")

    if raw_slug in SLUG_ALIASES:
        mapped = SLUG_ALIASES[raw_slug]
        return mapped, subj_by_slug.get(mapped)

    if not catalog:
        return (raw_slug or None), None

    for candidate in (
        raw_slug,
        f"the-{raw_slug}",
        raw_slug.replace("intro-to-", "introduction-to-"),
        raw_slug.replace("introduction-to-", "intro-to-"),
    ):
        if candidate in subj_by_slug:
            return candidate, subj_by_slug[candidate]

    query_title_tokens = _norm_tokens(" ".join(parts[1:-1]) if len(parts) > 3 else course_name)
    query_all_tokens = _norm_tokens(" ".join(parts[1:]))
    best_entry: PetersonCatalogEntry | None = None
    best_score = 0.0

    for entry in catalog:
        entry_title_tokens = _norm_tokens(entry.title)
        entry_inst_tokens = _norm_tokens(entry.instructor)
        title_overlap = len(query_title_tokens & entry_title_tokens)
        inst_overlap = sum(
            1
            for qt in query_all_tokens
            if any(et == qt or (len(qt) >= 3 and et.startswith(qt)) for et in entry_inst_tokens)
        )

        for part_tag in ("part-1", "part-2"):
            if part_tag in raw_slug and part_tag not in entry.slug:
                title_overlap -= 3

        score = title_overlap * 3.0 + inst_overlap * 2.0
        if entry_title_tokens and title_overlap == len(entry_title_tokens):
            score += 4.0
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_entry is not None and best_score >= 3.0:
        return best_entry.slug, best_entry.subject

    return (raw_slug or None), None


def parse_peterson_course_html(
    raw_html: str,
    *,
    fallback_slug: str = "",
    subject: str | None = None,
) -> MediaItemMetadata | None:
    """Pure function: parses a Peterson Academy course page SSR HTML into `MediaItemMetadata`."""
    m = PREVIEW_COURSE_RE.search(raw_html)
    if not m:
        return None

    course_id, raw_title, raw_desc, parsed_slug, course_code = m.groups()
    title = _unescape_js_string(raw_title)
    description = _unescape_js_string(raw_desc)

    window_4k = raw_html[m.start() : m.start() + 4000]
    inst_match = INSTRUCTOR_RE.search(window_4k)
    instructor = _unescape_js_string(inst_match.group(1)) if inst_match else ""

    window_2k = raw_html[m.start() : m.start() + 2000]
    trailer_match = TRAILER_MUX_RE.search(window_2k)
    mux_id = (
        trailer_match.group(1).strip()
        if trailer_match and trailer_match.group(1).strip()
        else None
    )

    poster_url = f"https://ik.imagekit.io/0qkyxdfkk/prod/courses%2F{course_id}%2Fthumb?tr=h-1200&alt=media"
    hero_url = f"https://ik.imagekit.io/0qkyxdfkk/prod/courses%2F{course_id}%2Fhero?tr=w-1920&alt=media"
    thumb_landscape = (
        f"https://image.mux.com/{mux_id}/thumbnail.jpg?width=640"
        if mux_id
        else f"https://ik.imagekit.io/0qkyxdfkk/prod/courses%2F{course_id}%2Fhero?tr=w-640&alt=media"
    )

    lectures_by_num: dict[int, EpisodeMetadata] = {}
    for lm in LECTURE_RE.finditer(raw_html):
        _, lec_num_str, _, lec_title, lec_desc, dur_str, _ = lm.groups()
        lec_num = int(lec_num_str)
        if lec_num not in lectures_by_num:
            lectures_by_num[lec_num] = EpisodeMetadata(
                number=lec_num,
                title=_unescape_js_string(lec_title),
                description=_unescape_js_string(lec_desc),
                duration_seconds=int(dur_str),
                thumbnail=thumb_landscape,
            )

    genres_list: list[str] = ["Peterson Academy"]
    if subject and subject not in genres_list:
        genres_list.append(subject)
    genres_list.append("Education")

    return MediaItemMetadata(
        stremio_id=f"sw_pa_{course_id}",
        collection="Peterson Academy",
        title=title,
        creator=instructor,
        code=course_code,
        description=description,
        poster=poster_url,
        background=hero_url,
        episode_thumbnail=thumb_landscape,
        genres=tuple(genres_list),
        episodes=tuple(lectures_by_num[k] for k in sorted(lectures_by_num)),
        slug=parsed_slug or fallback_slug,
        media_type="series",
    )


class PetersonAcademyParser:
    """Extensible `MetadataProviderProtocol` implementation for Peterson Academy courses."""

    BASE_URL: str = "https://petersonacademy.com"

    def __init__(self) -> None:
        self._cached_catalog: tuple[PetersonCatalogEntry, ...] | None = None

    @property
    def name(self) -> str:
        return "Peterson Academy"

    @property
    def is_fallback(self) -> bool:
        return False

    def can_handle(
        self,
        display_title: str,
        explicit_url: str | None = None,
        explicit_slug: str | None = None,
    ) -> bool:
        if explicit_url and "petersonacademy.com" in explicit_url.lower():
            return True
        if explicit_slug:
            return True
        return "peterson academy" in display_title.lower()

    def _get_catalog(self, fetch_html: HtmlFetcher) -> tuple[PetersonCatalogEntry, ...]:
        if self._cached_catalog is not None:
            return self._cached_catalog
        raw_html = fetch_html(f"{self.BASE_URL}/courses")
        if not raw_html:
            self._cached_catalog = ()
            return ()
        self._cached_catalog = parse_peterson_catalog_html(raw_html)
        return self._cached_catalog

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
        _ = files, torrent_id
        catalog = self._get_catalog(fetch_html)
        slug, subject = infer_peterson_slug_and_subject(
            display_title,
            catalog,
            explicit_url=explicit_url,
            explicit_slug=explicit_slug,
        )
        if not slug:
            return None

        course_html = fetch_html(f"{self.BASE_URL}/courses/{slug}")
        if not course_html:
            return None

        return parse_peterson_course_html(
            course_html,
            fallback_slug=slug,
            subject=subject,
        )
