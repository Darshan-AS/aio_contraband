"""Unit tests for pure metadata providers, static addon compiler (series & movie), and path security."""

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from contraband.cli import (
    _discover_synced_torrent_ids,
    _refresh_aiostreams_cache,
    commit_catalog_index,
    load_catalog_index,
    resolve_bound_media_item,
)
from contraband.core.compiler import (
    audit_contraband_addon,
    compile_contraband_addon,
    remove_media_items,
    seed_index_from_existing_files,
    upsert_media_item,
)
from contraband.core.errors import NetworkError
from contraband.core.result import Err, Ok, Result
from contraband.core.types import (
    AddonConfig,
    BoundMediaItem,
    CatalogIndex,
    EpisodeFileMatch,
    TorrentFileEntry,
    VirtualFS,
)
from contraband.effects.git import publish_git_repo
from contraband.effects.http import HttpResponse
from contraband.effects.storage import resolve_within_directory
from contraband.providers.metadata import (
    discover_plugin_metadata_providers,
    resolve_media_metadata,
)
from contraband.providers.metadata.archive_org import parse_archive_org_metadata_json
from contraband.providers.metadata.fallback import build_fallback_media_metadata
from contraband.providers.metadata.peterson import (
    infer_peterson_slug_and_subject,
    parse_peterson_catalog_html,
    parse_peterson_course_html,
)
from contraband.providers.metadata.schema_org import parse_web_media_html
from contraband.providers.metadata.sidecar import parse_sidecar_metadata
from contraband.providers.torbox import TorBoxProvider


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
    assert PurePosixPath("meta/library/aiostreams::library.torbox.torrent.555.json") not in vfs.files

    manifest = json.loads(vfs.files[PurePosixPath("manifest.json")])
    assert "movie" in manifest["types"]
    assert "library" not in manifest["types"]
    assert "aiostreams::library." not in manifest["idPrefixes"]
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
    assert PurePosixPath("meta/library/aiostreams::library.torbox.torrent.999.json") not in vfs.files
    assert PurePosixPath("stream/series/sw_mc_999_s1e1.json") in vfs.files

    series_meta = json.loads(vfs.files[PurePosixPath("meta/series/sw_mc_999.json")])
    assert series_meta["meta"]["videos"][0]["id"] == "sw_mc_999_s1e1"
    assert series_meta["meta"]["videos"][1]["id"] == "sw_mc_999_s1e2"

    manifest = json.loads(vfs.files[PurePosixPath("manifest.json")])
    assert manifest["id"] == "dev.darshanas.contraband"
    assert manifest["catalogs"][0]["id"] == "masterclass"

    # Opt-in emit_library_meta=True still emits legacy library metadata when explicitly requested
    lib_vfs = compile_contraband_addon(idx, addon_config=AddonConfig(emit_library_meta=True))
    assert PurePosixPath("meta/library/aiostreams::library.torbox.torrent.999.json") in lib_vfs.files
    lib_manifest = json.loads(lib_vfs.files[PurePosixPath("manifest.json")])
    assert "library" in lib_manifest["types"]

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


def test_multi_season_series_compilation_and_roundtrip(tmp_path: Path) -> None:
    files = (
        TorrentFileEntry(index=4, name="Archive.S01E01.Origins.mkv", size_bytes=1000),
        TorrentFileEntry(index=9, name="Archive.S02E01.Expansion.mkv", size_bytes=2000),
    )
    bound = resolve_bound_media_item(
        display_title="BBC - Deep History Archive",
        info_hash="d" * 40,
        torrent_id=777,
        files=files,
        fetch_html=lambda _url: None,
        allow_fallback=True,
    )
    assert bound is not None
    assert len(bound.episode_matches) == 2

    idx = upsert_media_item(CatalogIndex(), bound)
    commit_res = commit_catalog_index(tmp_path, idx)
    assert isinstance(commit_res, Ok)

    s1_stream = json.loads((tmp_path / "stream" / "series" / "sw_bbc_777_s1e1.json").read_text())
    s2_stream = json.loads((tmp_path / "stream" / "series" / "sw_bbc_777_s2e1.json").read_text())
    assert s1_stream["streams"][0]["fileIdx"] == 4
    assert s2_stream["streams"][0]["fileIdx"] == 9

    assert _discover_synced_torrent_ids(tmp_path) == {777}
    reloaded = load_catalog_index(tmp_path)
    assert [(m.season, m.episode_number, m.file_index) for m in reloaded.items[0].episode_matches] == [
        (1, 1, 4),
        (2, 1, 9),
    ]


class _FakeHttpClient:
    def __init__(self, responses: Mapping[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        raw_body: bytes | None = None,
        timeout: float = 30.0,
    ) -> Result[HttpResponse, NetworkError]:
        _ = headers, json_body, raw_body, timeout
        self.calls.append((method, url))
        payload = self._responses.get(url, {"success": True, "data": {}})
        return Ok(HttpResponse(status_code=200, data=payload, text=""))

    def fetch_html(self, url: str, *, timeout: float = 20.0) -> str | None:
        _ = url, timeout
        return None


def test_torbox_check_cached_bulk_batches_single_request() -> None:
    h1 = "a" * 40
    h2 = "b" * 40
    expected_url = (
        f"https://api.torbox.app/v1/api/torrents/checkcached?hash={h1},{h2}&format=object&list_files=true"
    )
    fake_http = _FakeHttpClient(
        {
            expected_url: {
                "success": True,
                "data": {
                    h1: {
                        "size": 4096,
                        "files": [{"id": 0, "short_name": "01.mp4", "size": 4096}],
                    }
                },
            }
        }
    )
    provider = TorBoxProvider(api_key="tb_test_key", http_client=fake_http)
    res = provider.check_cached_bulk([h1, h2, h1.upper()])
    assert isinstance(res, Ok)
    assert len(fake_http.calls) == 1
    assert res.value[h1].is_cached is True
    assert res.value[h1].size_bytes == 4096
    assert res.value[h2].is_cached is False


def test_refresh_aiostreams_cache_injects_secret_placeholder(tmp_path: Path) -> None:
    addon_dir = tmp_path / "aio_contraband"
    addon_dir.mkdir()
    mgr_dir = tmp_path / "stream-weaver" / "configs"
    mgr_dir.mkdir(parents=True)
    (mgr_dir / "aiomanager.json").write_text(
        json.dumps(
            {
                "addons": {
                    "savedAddons": [
                        {
                            "name": "AIOStreams",
                            "installUrl": "https://streams.example.com/stremio/u1/{{SECRET_AIOSTREAMS_ENCRYPTED_PASSWORD}}/manifest.json",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    fake_http = _FakeHttpClient({})
    assert _refresh_aiostreams_cache(addon_dir, fake_http, env={}) is False
    assert fake_http.calls == []

    assert (
        _refresh_aiostreams_cache(
            addon_dir,
            fake_http,
            env={"SECRET_AIOSTREAMS_ENCRYPTED_PASSWORD": "enc_secret_value"},
        )
        is True
    )
    assert fake_http.calls == [
        (
            "GET",
            "https://streams.example.com/stremio/u1/enc_secret_value/stream/movie/aiostreams::library.torbox.action.refresh.json",
        )
    ]


def test_publish_git_repo_stages_only_managed_paths(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Contraband Test"],
        check=True,
        capture_output=True,
    )

    # Create both managed addon output and unmanaged source/scratch files
    (tmp_path / "manifest.json").write_text('{"id":"dev.darshanas.contraband"}\n', encoding="utf-8")
    (tmp_path / "scratch_notes.txt").write_text("do not commit me\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "wip.py").write_text("x = 1\n", encoding="utf-8")

    res = publish_git_repo(tmp_path, commit_message="test: managed only", push=False)
    assert res == Ok(True)

    committed = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-tree", "-r", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert committed == ["manifest.json"]

    # Second run with only unmanaged dirty files should be a no-op
    res_noop = publish_git_repo(tmp_path, commit_message="test: noop", push=False)
    assert res_noop == Ok(False)


def test_schema_org_html_parser_handles_arbitrary_attribute_order_and_multiline_tags() -> None:
    html_doc = """
    <!doctype html>
    <html>
      <head>
        <title>Fallback HTML Title</title>
        <meta
          data-rh="true"
          content="Linear Algebra &amp; Differential Equations"
          property="og:title"
        >
        <meta content='https://cdn.example.org/mit.png' id="og-img" property='og:image'>
        <script nonce="abc123" type="application/ld+json; charset=utf-8">
        {
          "@graph": [
            {
              "@type": ["Course", "LearningResource"],
              "name": "18.06 Linear Algebra",
              "description": "Gilbert Strang's classic MIT lecture series.",
              "instructor": ["Prof. Gilbert Strang"],
              "itemListElement": [
                {"@type": "ListItem", "item": {"name": "The Geometry of Linear Equations"}},
                {"@type": "ListItem", "item": {"name": "Elimination with Matrices"}}
              ]
            }
          ]
        }
        </script>
      </head>
    </html>
    """
    meta = parse_web_media_html(html_doc, "https://ocw.mit.edu/courses/18-06-linear-algebra/")
    assert meta.collection == "MIT OpenCourseWare"
    assert meta.title == "18.06 Linear Algebra"
    assert meta.creator == "Prof. Gilbert Strang"
    assert meta.poster == "https://cdn.example.org/mit.png"
    assert len(meta.episodes) == 2
    assert meta.episodes[0].title == "The Geometry of Linear Equations"
    assert meta.episodes[1].title == "Elimination with Matrices"


def test_archive_org_provider_and_routing() -> None:
    ia_json = json.dumps(
        {
            "metadata": {
                "title": "Night of the Living Dead (1968)",
                "creator": "George A. Romero",
                "description": "<p>Classic public-domain <b>horror</b> feature film.</p>",
                "subject": "horror; public domain; zombies",
            },
            "files": [
                {
                    "name": "Night_of_the_Living_Dead_1080p.mp4",
                    "source": "original",
                    "length": "5760",
                },
                {
                    "name": "Night_of_the_Living_Dead.ogv",
                    "source": "derivative",
                    "length": "5760",
                },
            ],
        }
    )
    meta = parse_archive_org_metadata_json(ia_json, "night_of_the_living_dead")
    assert meta is not None
    assert meta.stremio_id == "sw_ia_night_of_the_living_dead"
    assert meta.collection == "Internet Archive"
    assert meta.media_type == "movie"
    assert meta.creator == "George A. Romero"
    assert meta.description == "Classic public-domain horror feature film."
    assert "Horror" in meta.genres

    # Verify resolve_media_metadata routes archive.org/details/<id> to InternetArchiveMetadataProvider
    requested_urls: list[str] = []

    def fake_fetch(u: str) -> str | None:
        requested_urls.append(u)
        return ia_json

    routed = resolve_media_metadata(
        "Night of the Living Dead",
        (TorrentFileEntry(index=0, name="Night_of_the_Living_Dead_1080p.mp4", size_bytes=100),),
        fake_fetch,
        explicit_url="https://archive.org/details/night_of_the_living_dead",
    )
    assert routed is not None
    assert routed.stremio_id == "sw_ia_night_of_the_living_dead"
    assert requested_urls == ["https://archive.org/metadata/night_of_the_living_dead"]


def test_sidecar_metadata_provider_toml_and_json(tmp_path: Path) -> None:
    toml_sidecar = tmp_path / "course.toml"
    toml_sidecar.write_text(
        '[metadata]\n'
        'title = "Distributed Systems Seminar"\n'
        'collection = "Computer Science"\n'
        'creator = "Dr. Leslie Lamport"\n'
        'description = " clocks, state machines, and consensus."\n'
        'genres = ["Systems", "Theory"]\n'
        "\n"
        "[[episodes]]\n"
        "number = 1\n"
        'title = "Time, Clocks, and the Ordering of Events"\n'
        "duration_seconds = 3200\n"
        "\n"
        "[[episodes]]\n"
        "number = 2\n"
        'title = "The Part-Time Parliament (Paxos)"\n'
        "duration_seconds = 3500\n",
        encoding="utf-8",
    )
    files = (
        TorrentFileEntry(index=0, name="01 - Time Clocks.mp4", size_bytes=1000),
        TorrentFileEntry(index=1, name="02 - Paxos.mp4", size_bytes=1000),
    )
    resolved = resolve_media_metadata(
        "Distributed Systems",
        files,
        lambda _: None,
        explicit_url=str(toml_sidecar),
    )
    assert resolved is not None
    assert resolved.title == "Distributed Systems Seminar"
    assert resolved.collection == "Computer Science"
    assert resolved.creator == "Dr. Leslie Lamport"
    assert resolved.media_type == "series"
    assert len(resolved.episodes) == 2
    assert resolved.episodes[1].title == "The Part-Time Parliament (Paxos)"

    json_meta = parse_sidecar_metadata(
        json.dumps(
            {
                "title": "Indie Short Film",
                "collection": "Festival Shorts",
                "media_type": "movie",
            }
        ),
        fmt="json",
    )
    assert json_meta is not None
    assert json_meta.media_type == "movie"
    assert json_meta.collection == "Festival Shorts"


def test_discover_plugin_metadata_providers_handles_custom_entry_points(monkeypatch: Any) -> None:
    class _DummyPluginProvider:
        @property
        def name(self) -> str:
            return "Dummy Plugin"

        @property
        def is_fallback(self) -> bool:
            return False

        def can_handle(
            self,
            display_title: str,
            explicit_url: str | None = None,
            explicit_slug: str | None = None,
        ) -> bool:
            _ = display_title, explicit_url
            return explicit_slug == "plugin:demo"

        def resolve_metadata(
            self,
            display_title: str,
            files: Any,
            fetch_html: Any,
            *,
            torrent_id: int = 0,
            explicit_url: str | None = None,
            explicit_slug: str | None = None,
        ) -> Any:
            _ = display_title, files, fetch_html, torrent_id, explicit_url, explicit_slug
            return None

    class _FakeEntryPoint:
        def load(self) -> Any:
            return _DummyPluginProvider

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_FakeEntryPoint()] if group == "contraband.metadata_providers" else [],
    )
    plugins = discover_plugin_metadata_providers()
    assert len(plugins) == 1
    assert plugins[0].name == "Dummy Plugin"

