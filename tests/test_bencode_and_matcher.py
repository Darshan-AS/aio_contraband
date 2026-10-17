"""Unit tests for bencode decoding, magnet parsing, and smart episode matching."""

from __future__ import annotations

import hashlib

from contraband.core.bencode import (
    extract_magnets_from_text,
    parse_magnet_uri,
    parse_torrent_bytes,
)
from contraband.core.matcher import (
    clean_video_stem,
    extract_episode_number_from_filename,
    format_bytes,
    match_files_to_episodes,
)
from contraband.core.result import Ok
from contraband.core.types import EpisodeMetadata, TorrentFileEntry


def test_parse_single_file_torrent_bytes() -> None:
    info_bytes = b"d6:lengthi1048576e4:name12:lecture1.mp412:piece lengthi16384e6:pieces20:01234567890123456789e"
    torrent_bytes = b"d8:announce18:http://tracker/ann4:info" + info_bytes + b"e"
    expected_hash = hashlib.sha1(info_bytes).hexdigest().lower()

    res = parse_torrent_bytes(torrent_bytes)
    assert isinstance(res, Ok)
    parsed = res.value
    assert parsed.info_hash == expected_hash
    assert parsed.name == "lecture1.mp4"
    assert parsed.total_bytes == 1_048_576
    assert len(parsed.video_files) == 1


def test_parse_magnet_and_extract_from_markdown() -> None:
    ih = "a" * 40
    md = f"""
    # Courses
    * [Course 1](magnet:?xt=urn:btih:{ih}&dn=Peterson+Academy+-+Plato)
    * Duplicate: magnet:?xt=urn:btih:{ih.upper()}
    """
    extracted = extract_magnets_from_text(md)
    assert len(extracted) == 1
    assert extracted[0].info_hash == ih
    assert extracted[0].display_name == "Peterson Academy - Plato"

    direct = parse_magnet_uri(ih)
    assert isinstance(direct, Ok)
    assert direct.value.info_hash == ih


def test_episode_matching_numbered_and_fuzzy() -> None:
    assert extract_episode_number_from_filename("03. The Republic.mp4") == 3
    assert extract_episode_number_from_filename("Lecture 07 - Aristotle.mkv") == 7
    assert clean_video_stem("03. The_Republic.mp4") == "The Republic"
    assert format_bytes(2 * (1024**3)) == "2.00 GB"

    files = (
        TorrentFileEntry(index=10, name="01. Intro.mp4", size_bytes=100),
        TorrentFileEntry(index=11, name="Bonus_Aristotle_Ethics.mp4", size_bytes=100),
    )
    episodes = (
        EpisodeMetadata(number=1, title="Introduction"),
        EpisodeMetadata(number=2, title="Aristotle Ethics"),
    )
    matches = match_files_to_episodes(files, episodes)
    assert len(matches) == 2
    assert matches[0].episode_number == 1
    assert matches[0].file_index == 10
    assert matches[1].episode_number == 2
    assert matches[1].file_index == 11
