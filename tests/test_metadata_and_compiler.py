"""Unit tests for pure metadata providers, static addon compiler (series & movie), and path security."""

import json
from pathlib import Path, PurePosixPath

from contraband.cli import commit_catalog_index, load_catalog_index, resolve_bound_media_item
from contraband.core.compiler import (
    audit_contraband_addon,
    compile_contraband_addon,
    remove_media_items,
    seed_index_from_existing_files,
    upsert_media_item,
)
from contraband.core.result import Err, Ok
from contraband.core.types import (
    BoundMediaItem,
    CatalogIndex,
    EpisodeFileMatch,
    TorrentFileEntry,
    VirtualFS,
)
from contraband.effects.storage import resolve_within_directory
from contraband.providers.metadata.fallback import build_fallback_media_metadata
from contraband.providers.metadata.peterson import (
    infer_peterson_slug_and_subject,
    parse_peterson_catalog_html,
    parse_peterson_course_html,
)
from contraband.providers.metadata.schema_org import parse_web_media_html


def test_peterson_parser_pure_html_extraction() -> None:
    catalog_html = (
        '{label:"Philosophy",icon:"book",courses:['
        '{id:3215,slug:"plato-s-republic",title:"Plato\'s Republic",courseCode:"PHL-101",instructor:"Dr. James Orr"}'
        "]}"
    )
    entries = parse_peterson_catalog_html(catalog_html)
    assert len(entries) == 1
    assert entries[0].slug == "plato-s-republic"
    assert entries[0].subject == "Philosophy"

    slug, subj = infer_peterson_slug_and_subject(
        "Peterson Academy - Plato's Republic - Dr. James Orr 1080p",
        entries,
    )
    assert slug == "plato-s-republic"
    assert subj == "Philosophy"

    course_html = (
        'previewCourse:{id:3215,title:"Plato\'s Republic",description:"A deep dive into justice.",'
        'slug:"plato-s-republic",label:"Course",courseCode:"PHL-101",'
        'instructors:[{id:1,fullName:"Dr. James Orr",slug:"james-orr",title:"Professor"}],'
        'trailerPlaybackId:"mux123"},'
        'lectures:[{id:"l1",lectureId:10,lectureNumber:1,path:"/1",title:"The Cave",description:"Shadows.",durationSeconds:3600,muxAssetId:"m1"}]'
    )
    meta = parse_peterson_course_html(course_html, subject=subj)
    assert meta is not None
    assert meta.stremio_id == "sw_pa_3215"
    assert meta.creator == "Dr. James Orr"
    assert meta.genres == ("Peterson Academy", "Philosophy", "Education")
    assert len(meta.episodes) == 1
    assert meta.episodes[0].title == "The Cave"


def test_schema_org_parser_pure_html_extraction() -> None:
    html_doc = """
    <html>
      <head>
        <meta property="og:title" content="Chris Voss Teaches the Art of Negotiation" />
        <meta property="og:image" content="https://example.com/voss.jpg" />
        <script type="application/ld+json">
        {
          "@type": "Course",
          "name": "The Art of Negotiation",
          "description": "Learn tactical empathy.",
          "instructor": {"name": "Chris Voss"},
          "hasPart": [
            {"name": "Introduction", "description": "Mirroring and labeling."}
          ]
        }
        </script>
      </head>
    </html>
    """
    meta = parse_web_media_html(html_doc, "https://www.masterclass.com/classes/chris-voss")
    assert meta.collection == "MasterClass"
    assert meta.stremio_id.startswith("sw_mc_")
    assert meta.title == "The Art of Negotiation"
    assert meta.creator == "Chris Voss"
    assert "Education" in meta.genres
    assert len(meta.episodes) == 1
    assert meta.episodes[0].title == "Introduction"


def test_non_course_standup_special_movie_compilation() -> None:
    single_file = (
        TorrentFileEntry(index=0, name="Louis.CK.Sorry.2021.1080p.WEB-DL.mp4", size_bytes=2_000_000),
    )
    meta = build_fallback_media_metadata(
        "Louis C.K. - Sorry (2021) 1080p",
        single_file,
        torrent_id=555,
    ).with_collection("Stand-Up Specials")

    # Single-file non-educational item automatically defaults to `movie` without "Education" genre
    assert meta.media_type == "movie"
    assert meta.collection == "Stand-Up Specials"
    assert "Education" not in meta.genres
    assert "taught by" not in meta.description

    bound = BoundMediaItem(
        metadata=meta,
        info_hash="c" * 40,
        torrent_id=555,
        episode_matches=(
            EpisodeFileMatch(
                episode_number=1,
                file_index=0,
                filename="Louis.CK.Sorry.2021.1080p.WEB-DL.mp4",
            ),
        ),
    )

    idx = upsert_media_item(CatalogIndex(), bound)
    vfs = compile_contraband_addon(idx)

    assert PurePosixPath("catalog/movie/stand_up_specials.json") in vfs.files
    assert PurePosixPath("meta/movie/sw_tb_555.json") in vfs.files
    assert PurePosixPath("stream/movie/sw_tb_555.json") in vfs.files
    assert PurePosixPath("meta/library/aiostreams::library.torbox.torrent.555.json") in vfs.files

    manifest = json.loads(vfs.files[PurePosixPath("manifest.json")])
    assert "movie" in manifest["types"]
    assert manifest["catalogs"][0] == {
        "type": "movie",
        "id": "stand_up_specials",
        "name": "Stand-Up Specials",
    }


def test_addon_compiler_and_seed_roundtrip() -> None:
    files = (
        TorrentFileEntry(index=0, name="01. First Lecture.mp4", size_bytes=1000),
        TorrentFileEntry(index=1, name="02. Second Lecture.mp4", size_bytes=2000),
    )
    meta = build_fallback_media_metadata(
        "MasterClass - Negotiation - Chris Voss",
        files,
        torrent_id=999,
    )
    bound = BoundMediaItem(
        metadata=meta,
        info_hash="b" * 40,
        torrent_id=999,
        episode_matches=(
            EpisodeFileMatch(episode_number=1, file_index=0, filename="01. First Lecture.mp4"),
            EpisodeFileMatch(episode_number=2, file_index=1, filename="02. Second Lecture.mp4"),
        ),
    )

    idx = upsert_media_item(CatalogIndex(), bound)
    vfs = compile_contraband_addon(idx)

    assert PurePosixPath("manifest.json") in vfs.files
    assert PurePosixPath("catalog_index.json") in vfs.files
    assert PurePosixPath("catalog/series/masterclass.json") in vfs.files
    assert PurePosixPath("meta/series/sw_mc_999.json") in vfs.files
    assert PurePosixPath("meta/library/aiostreams::library.torbox.torrent.999.json") in vfs.files
    assert PurePosixPath("stream/series/sw_mc_999_s1e1.json") in vfs.files

    manifest = json.loads(vfs.files[PurePosixPath("manifest.json")])
    assert manifest["id"] == "dev.darshanas.contraband"
    assert manifest["catalogs"][0]["id"] == "masterclass"

    # Verify seeding from legacy files without catalog_index.json
    legacy_files = {k: v for k, v in vfs.files.items() if k != PurePosixPath("catalog_index.json")}
    seeded = seed_index_from_existing_files(legacy_files)
    assert len(seeded.items) == 1
    assert seeded.items[0].metadata.stremio_id == "sw_mc_999"
    assert seeded.items[0].info_hash == "b" * 40


def test_resolve_within_directory_blocks_traversal(tmp_path: Path) -> None:
    ok_res = resolve_within_directory(tmp_path, "catalog/series/courses.json")
    assert isinstance(ok_res, Ok)

    bad_res = resolve_within_directory(tmp_path, "../../etc/passwd")
    assert isinstance(bad_res, Err)


def test_single_pass_batch_index_weaving_and_commit(tmp_path: Path) -> None:
    idx = load_catalog_index(tmp_path)
    assert idx.items == ()

    bound1 = resolve_bound_media_item(
        display_title="MasterClass - Chris Voss - Art of Negotiation",
        info_hash="1" * 40,
        torrent_id=101,
        files=(TorrentFileEntry(index=0, name="01. Intro.mp4", size_bytes=100),),
        fetch_html=lambda _url: None,
        allow_fallback=True,
    )
    bound2 = resolve_bound_media_item(
        display_title="Standup Special 2026",
        info_hash="2" * 40,
        torrent_id=102,
        files=(TorrentFileEntry(index=0, name="Special.2026.1080p.mp4", size_bytes=200),),
        fetch_html=lambda _url: None,
        collection_override="Stand-Up Specials",
        media_type_override="movie",
    )
    assert bound1 is not None
    assert bound2 is not None

    idx = upsert_media_item(idx, bound1)
    idx = upsert_media_item(idx, bound2)
    commit_res = commit_catalog_index(tmp_path, idx)
    assert isinstance(commit_res, Ok)
    assert commit_res.value > 0

    reloaded = load_catalog_index(tmp_path)
    assert len(reloaded.items) == 2


def test_remove_media_items_and_audit_contraband_addon() -> None:
    series_files = (TorrentFileEntry(index=0, name="01. Intro.mp4", size_bytes=100),)
    movie_files = (TorrentFileEntry(index=0, name="Louis.CK.Sorry.2021.mp4", size_bytes=100),)
    meta1 = build_fallback_media_metadata(
        "MasterClass - Chris Voss - Negotiation",
        series_files,
        torrent_id=101,
    )
    meta2 = build_fallback_media_metadata(
        "Louis C.K. - Sorry (2021)",
        movie_files,
        torrent_id=202,
    ).with_collection("Stand-Up Specials")

    bound1 = BoundMediaItem(
        metadata=meta1,
        info_hash="1" * 40,
        torrent_id=101,
        episode_matches=(EpisodeFileMatch(episode_number=1, file_index=0, filename="01. Intro.mp4"),),
    )
    bound2 = BoundMediaItem(
        metadata=meta2,
        info_hash="2" * 40,
        torrent_id=0,
        episode_matches=(),
    )

    idx = upsert_media_item(upsert_media_item(CatalogIndex(), bound1), bound2)
    compiled_vfs = compile_contraband_addon(idx)

    expected_stream_path = PurePosixPath(f"stream/movie/{meta2.stremio_id}.json")
    # Inject an orphan file and remove an expected file to verify audit_contraband_addon
    drifted_files = dict(compiled_vfs.files)
    drifted_files[PurePosixPath("meta/series/orphan.json")] = "{}\n"
    del drifted_files[expected_stream_path]

    report = audit_contraband_addon(
        idx,
        VirtualFS(files=drifted_files),
        active_hashes={"1" * 40},
    )
    assert report.total_items == 2
    assert report.series_count == 1
    assert report.movie_count == 1
    assert len(report.unlinked_items) == 1
    assert report.unlinked_items[0].metadata.stremio_id == bound2.metadata.stremio_id
    assert len(report.missing_on_debrid) == 1
    assert report.missing_on_debrid[0].info_hash == "2" * 40
    assert PurePosixPath("meta/series/orphan.json") in report.orphaned_files
    assert expected_stream_path in report.missing_files
    assert report.has_fs_drift is True
    assert report.is_healthy is False

    # Remove the unlinked item by title substring and verify resulting index is healthy
    pruned_idx, removed = remove_media_items(idx, "Louis C.K.")
    assert len(removed) == 1
    assert removed[0].metadata.stremio_id == bound2.metadata.stremio_id
    assert len(pruned_idx.items) == 1

    clean_vfs = compile_contraband_addon(pruned_idx)
    healthy_report = audit_contraband_addon(
        pruned_idx,
        clean_vfs,
        active_hashes={"1" * 40},
    )
    assert healthy_report.is_healthy is True
