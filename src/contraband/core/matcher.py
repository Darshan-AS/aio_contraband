"""Pure functions for episode-to-file matching, filename cleaning, and artwork URL generation."""

import re
import urllib.parse
from collections.abc import Sequence

from contraband.core.types import EpisodeFileMatch, EpisodeMetadata, TorrentFileEntry

EPISODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:s\d+e|ep?\s*|lecture\s*|lesson\s*|part\s*|chapter\s*|module\s*\d+\s*[-_.]\s*)0*(\d+)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*0*(\d+)[\.\s_:\-]"),
)

STEM_EXTENSION_RE = re.compile(r"\.[a-zA-Z0-9]{2,4}$")
STEM_LEADING_NUM_RE = re.compile(
    r"^(?:s\d+e\d+|ep?\s*\d+|lecture\s*\d+|lesson\s*\d+|part\s*\d+|\d+)[\.\s_:\-]+",
    re.IGNORECASE,
)
WORD_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def format_bytes(size: int) -> str:
    """Formats a byte count into a human-readable string (`B`, `KB`, `MB`, `GB`, `TB`)."""
    if size <= 0:
        return "Unknown size"
    val = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.2f} {unit}" if unit in ("GB", "TB") else f"{val:.0f} {unit}"
        val /= 1024.0
    return f"{val:.2f} TB"


def build_typographic_artwork(title: str, subtitle: str = "") -> tuple[str, str, str]:
    """Generates clean dark-mode typographic poster, backdrop, and thumbnail URLs."""
    label = f"{title}\n{subtitle}".strip() if subtitle else title
    encoded = urllib.parse.quote(label)
    poster = f"https://placehold.co/600x900/111827/f9fafb/png?text={encoded}"
    backdrop = f"https://placehold.co/1920x1080/111827/f9fafb/png?text={encoded}"
    thumb = f"https://placehold.co/640x360/1f2937/f9fafb/png?text={encoded}"
    return poster, backdrop, thumb


def clean_video_stem(filename: str) -> str:
    """Strips file extension, directory prefix, and leading episode numbering from a video filename."""
    base = filename.rsplit("/", 1)[-1]
    stem = STEM_EXTENSION_RE.sub("", base)
    stem = STEM_LEADING_NUM_RE.sub("", stem)
    cleaned = stem.replace("_", " ").strip()
    return cleaned or base


def extract_episode_number_from_filename(short_name: str) -> int | None:
    """Extracts a 1-based episode number from common series/course naming patterns."""
    for pat in EPISODE_PATTERNS:
        m = pat.search(short_name)
        if m:
            num = int(m.group(1))
            if num > 0:
                return num
    return None


def match_files_to_episodes(
    files: Sequence[TorrentFileEntry],
    episodes: Sequence[EpisodeMetadata],
) -> tuple[EpisodeFileMatch, ...]:
    """
    Purely matches video files to 1-based episode numbers using:
      1. Explicit episode/part/lecture numbering in filenames (`01.`, `S01E01`, `Part 1`, `Lecture 1`)
      2. Fuzzy token similarity against known `EpisodeMetadata` titles
      3. Lexicographical fallback when files are unnumbered
    """
    by_episode: dict[int, EpisodeFileMatch] = {}
    video_files = [f for f in files if f.is_video]

    for vf in video_files:
        ep_num = extract_episode_number_from_filename(vf.short_name)
        if ep_num is not None and ep_num not in by_episode:
            by_episode[ep_num] = EpisodeFileMatch(
                episode_number=ep_num,
                file_index=vf.index,
                filename=vf.short_name,
            )

    # Fuzzy title fallback for unnumbered files when episode titles are known
    if len(by_episode) < len(episodes) and video_files:
        used_indices = {m.file_index for m in by_episode.values()}
        for ep in episodes:
            if ep.number in by_episode:
                continue
            ep_tokens = set(WORD_TOKEN_RE.findall(ep.title.lower()))
            if not ep_tokens:
                continue
            best_vf: TorrentFileEntry | None = None
            best_score = 0
            for vf in video_files:
                if vf.index in used_indices:
                    continue
                file_tokens = set(WORD_TOKEN_RE.findall(vf.short_name.lower()))
                score = len(ep_tokens & file_tokens)
                if score > best_score:
                    best_score = score
                    best_vf = vf
            if best_vf is not None and best_score > 0:
                by_episode[ep.number] = EpisodeFileMatch(
                    episode_number=ep.number,
                    file_index=best_vf.index,
                    filename=best_vf.short_name,
                )
                used_indices.add(best_vf.index)

    if not by_episode and video_files:
        sorted_videos = sorted(video_files, key=lambda item: item.short_name)
        for ep_num, vf in enumerate(sorted_videos, start=1):
            by_episode[ep_num] = EpisodeFileMatch(
                episode_number=ep_num,
                file_index=vf.index,
                filename=vf.short_name,
            )

    return tuple(by_episode[k] for k in sorted(by_episode))
