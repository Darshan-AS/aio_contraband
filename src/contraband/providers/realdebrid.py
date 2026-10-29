"""Real-Debrid cloud provider implementing `DebridProviderProtocol`."""

import concurrent.futures
import urllib.parse
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from contraband.core.errors import NetworkError
from contraband.core.result import Err, Ok, Result
from contraband.core.types import (
    DebridAddOutcome,
    DebridCacheStatus,
    DebridQueuedItem,
    DebridTorrentItem,
    TorrentFileEntry,
)
from contraband.effects.http import HttpClientProtocol

REALDEBRID_API_BASE = "https://api.real-debrid.com/rest/1.0"


def rd_id_to_int(rd_id: str) -> int:
    """Deterministically maps a Real-Debrid string torrent ID to a positive 31-bit integer."""
    hashed = zlib.crc32(rd_id.strip().encode("utf-8")) & 0x7FFFFFFF
    return hashed if hashed > 0 else 1


def parse_realdebrid_files(raw_files: Any) -> tuple[TorrentFileEntry, ...]:
    """
    Pure function: normalizes Real-Debrid `/torrents/info/{id}` file objects into `TorrentFileEntry`.
    Real-Debrid file `id` fields are 1-based indices into the bencoded torrent file list, so we
    subtract 1 to produce the 0-based `fileIdx` expected by Stremio addons.
    """
    if not isinstance(raw_files, list):
        return ()
    entries: list[TorrentFileEntry] = []
    for idx, f in enumerate(raw_files):
        if not isinstance(f, dict):
            continue
        selected = f.get("selected", 1)
        if isinstance(selected, int) and selected == 0 and len(raw_files) > 1:
            # Keep unselected files out only when Real-Debrid explicitly excluded them,
            # while preserving the original 0-based BitTorrent fileIdx!
            continue
        raw_id = f.get("id")
        file_idx = max(0, int(raw_id) - 1) if isinstance(raw_id, int) and raw_id >= 1 else idx
        raw_path = str(f.get("path") or f.get("name") or f"file_{file_idx}").lstrip("/")
        size = int(f.get("bytes") or f.get("size") or 0)
        entries.append(TorrentFileEntry(index=file_idx, name=raw_path, size_bytes=size))
    return tuple(entries)


def parse_realdebrid_torrent_item(
    item: Mapping[str, Any],
    *,
    files: tuple[TorrentFileEntry, ...] = (),
) -> tuple[str, DebridTorrentItem] | None:
    """Pure function: converts a Real-Debrid `/torrents` object into `(rd_id, DebridTorrentItem)`."""
    rd_id = str(item.get("id") or "").strip()
    if not rd_id:
        return None
    status = str(item.get("status") or "unknown")
    raw_progress = float(item.get("progress") or 0.0)
    normalized_progress = raw_progress / 100.0 if raw_progress > 1.0 else raw_progress
    normalized_progress = max(0.0, min(1.0, normalized_progress))
    finished = status == "downloaded"
    parsed_files = files or parse_realdebrid_files(item.get("files"))
    return (
        rd_id,
        DebridTorrentItem(
            id=rd_id_to_int(rd_id),
            info_hash=str(item.get("hash") or "").lower(),
            name=str(item.get("filename") or item.get("original_filename") or "Untitled"),
            size_bytes=int(item.get("bytes") or item.get("original_bytes") or 0),
            download_state=status,
            progress=1.0 if finished else normalized_progress,
            download_finished=finished,
            files=parsed_files,
        ),
    )


class RealDebridProvider:
    """`DebridProviderProtocol` implementation for Real-Debrid REST API v1.0."""

    def __init__(
        self,
        api_key: str,
        http_client: HttpClientProtocol,
        *,
        base_url: str = REALDEBRID_API_BASE,
        max_active: int = 5,
    ) -> None:
        self._api_key = api_key
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._max_active = max_active
        self._id_map: dict[int, str] = {}

    @property
    def name(self) -> str:
        return "Real-Debrid"

    @property
    def max_active_downloads(self) -> int:
        return self._max_active

    def _remember_id(self, rd_id: str) -> int:
        num_id = rd_id_to_int(rd_id)
        self._id_map[num_id] = rd_id
        return num_id

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        headers: Mapping[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> Result[tuple[int, Any], NetworkError]:
        url = f"{self._base_url}{endpoint}"
        req_headers: dict[str, str] = {"Authorization": f"Bearer {self._api_key}"}
        if headers:
            req_headers.update(headers)
        match self._http.request_json(
            method,
            url,
            headers=req_headers,
            raw_body=raw_body,
        ):
            case Err() as err:
                return err
            case Ok(resp):
                return Ok((resp.status_code, resp.data))

    def _post_form(
        self,
        endpoint: str,
        fields: Mapping[str, str],
    ) -> Result[tuple[int, Any], NetworkError]:
        encoded = urllib.parse.urlencode(fields).encode("utf-8")
        return self._request(
            "POST",
            endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            raw_body=encoded,
        )

    def check_cached_bulk(
        self, info_hashes: Sequence[str]
    ) -> Result[Mapping[str, DebridCacheStatus], NetworkError]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in info_hashes:
            ih = raw.strip().lower()
            if ih and ih not in seen:
                seen.add(ih)
                normalized.append(ih)

        if not normalized:
            return Ok({})

        results: dict[str, DebridCacheStatus] = {
            ih: DebridCacheStatus(info_hash=ih, is_cached=False) for ih in normalized
        }

        chunk_size = 40
        for start in range(0, len(normalized), chunk_size):
            chunk = normalized[start : start + chunk_size]
            joined = "/".join(chunk)
            match self._request("GET", f"/torrents/instantAvailability/{joined}"):
                case Err():
                    # Real-Debrid disables instantAvailability for many third-party tokens;
                    # degrade gracefully to uncached rather than aborting `cb add`.
                    return Ok(results)
                case Ok((status, data)):
                    if status != 200 or not isinstance(data, dict):
                        return Ok(results)
                    for ih in chunk:
                        entry = data.get(ih) or data.get(ih.upper())
                        if not isinstance(entry, dict):
                            continue
                        rd_variants = entry.get("rd")
                        if isinstance(rd_variants, list) and rd_variants:
                            first_var = rd_variants[0]
                            files: list[TorrentFileEntry] = []
                            total_size = 0
                            if isinstance(first_var, dict):
                                for f_key, f_meta in first_var.items():
                                    if not isinstance(f_meta, dict):
                                        continue
                                    f_idx = (
                                        max(0, int(f_key) - 1)
                                        if str(f_key).isdigit()
                                        else len(files)
                                    )
                                    f_size = int(f_meta.get("filesize") or 0)
                                    total_size += f_size
                                    files.append(
                                        TorrentFileEntry(
                                            index=f_idx,
                                            name=str(f_meta.get("filename") or f"file_{f_idx}"),
                                            size_bytes=f_size,
                                        )
                                    )
                            results[ih] = DebridCacheStatus(
                                info_hash=ih,
                                is_cached=True,
                                size_bytes=total_size,
                                files=tuple(files),
                            )

        return Ok(results)

    def check_cached(self, info_hash: str) -> Result[DebridCacheStatus, NetworkError]:
        ih = info_hash.strip().lower()
        match self.check_cached_bulk((ih,)):
            case Err() as err:
                return err
            case Ok(mapping):
                return Ok(
                    mapping.get(ih) or DebridCacheStatus(info_hash=ih, is_cached=False)
                )

    def _fetch_info_by_rd_id(self, rd_id: str) -> Result[Mapping[str, Any], NetworkError]:
        match self._request("GET", f"/torrents/info/{urllib.parse.quote(rd_id, safe='')}"):
            case Err() as err:
                return err
            case Ok((status, data)) if status == 200 and isinstance(data, dict):
                return Ok(data)
            case Ok((status, data)):
                return Err(
                    NetworkError(
                        message=f"Real-Debrid torrent info failed (HTTP {status}): {data}",
                        status_code=status,
                    )
                )

    def list_library(self) -> Result[tuple[DebridTorrentItem, ...], NetworkError]:
        match self._request("GET", "/torrents?limit=250"):
            case Err() as err:
                return err
            case Ok((status, data)):
                if status != 200 or not isinstance(data, list):
                    return Err(
                        NetworkError(
                            message=f"Real-Debrid library fetch failed (HTTP {status}): {data}",
                            status_code=status,
                        )
                    )

                raw_dicts = [d for d in data if isinstance(d, dict) and d.get("id")]
                downloaded_ids = [
                    str(d["id"])
                    for d in raw_dicts
                    if str(d.get("status") or "") == "downloaded" and not isinstance(d.get("files"), list)
                ]

                files_by_rd_id: dict[str, tuple[TorrentFileEntry, ...]] = {}
                if downloaded_ids:
                    def _load_files(rid: str) -> tuple[str, tuple[TorrentFileEntry, ...]]:
                        match self._fetch_info_by_rd_id(rid):
                            case Ok(info_obj):
                                return rid, parse_realdebrid_files(info_obj.get("files"))
                            case Err():
                                return rid, ()

                    workers = min(4, len(downloaded_ids))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                        for rid, f_tuple in pool.map(_load_files, downloaded_ids):
                            files_by_rd_id[rid] = f_tuple

                items: list[DebridTorrentItem] = []
                for raw in raw_dicts:
                    rid = str(raw["id"])
                    parsed = parse_realdebrid_torrent_item(
                        raw, files=files_by_rd_id.get(rid, ())
                    )
                    if parsed is not None:
                        real_id, t_item = parsed
                        self._remember_id(real_id)
                        items.append(t_item)
                return Ok(tuple(items))

    def list_queued(self) -> Result[tuple[DebridQueuedItem, ...], NetworkError]:
        # Real-Debrid surfaces queued/converting items directly in `/torrents`.
        return Ok(())

    def fetch_torrent_files(
        self, torrent_id: int
    ) -> Result[tuple[TorrentFileEntry, ...], NetworkError]:
        if torrent_id <= 0:
            return Ok(())
        rd_id = self._id_map.get(torrent_id)
        if rd_id is None:
            if isinstance(lib_res := self.list_library(), Err):
                return lib_res
            rd_id = self._id_map.get(torrent_id)
        if rd_id is None:
            return Ok(())
        match self._fetch_info_by_rd_id(rd_id):
            case Err() as err:
                return err
            case Ok(info_obj):
                return Ok(parse_realdebrid_files(info_obj.get("files")))

    def _finalize_added_torrent(
        self, rd_id: str, *, as_queued: bool
    ) -> Result[DebridAddOutcome, NetworkError]:
        num_id = self._remember_id(rd_id)
        if not as_queued:
            # Select all files so Real-Debrid begins caching/unrestricting immediately.
            _ = self._post_form(
                f"/torrents/selectFiles/{urllib.parse.quote(rd_id, safe='')}",
                {"files": "all"},
            )
        was_cached = False
        if isinstance(info_res := self._fetch_info_by_rd_id(rd_id), Ok):
            was_cached = str(info_res.value.get("status") or "") == "downloaded"
        detail = (
            "Instantly Cached on Real-Debrid"
            if was_cached
            else "Added to Real-Debrid Torrents"
        )
        return Ok(
            DebridAddOutcome(
                detail=detail,
                torrent_id=num_id,
                was_cached=was_cached,
            )
        )

    def add_magnet(
        self,
        magnet_uri: str,
        *,
        name: str | None = None,
        as_queued: bool = False,
    ) -> Result[DebridAddOutcome, NetworkError]:
        _ = name
        match self._post_form("/torrents/addMagnet", {"magnet": magnet_uri}):
            case Err() as err:
                return err
            case Ok((status, resp)):
                if status not in (200, 201) or not isinstance(resp, dict) or not resp.get("id"):
                    return Err(
                        NetworkError(
                            message=f"Real-Debrid addMagnet failed (HTTP {status}): {resp}",
                            status_code=status,
                        )
                    )
                return self._finalize_added_torrent(str(resp["id"]), as_queued=as_queued)

    def add_torrent_file(
        self,
        torrent_path: Path,
        file_bytes: bytes,
        *,
        as_queued: bool = False,
    ) -> Result[DebridAddOutcome, NetworkError]:
        _ = torrent_path
        match self._request(
            "PUT",
            "/torrents/addTorrent",
            headers={"Content-Type": "application/x-bittorrent"},
            raw_body=file_bytes,
        ):
            case Err() as err:
                return err
            case Ok((status, resp)):
                if status not in (200, 201) or not isinstance(resp, dict) or not resp.get("id"):
                    return Err(
                        NetworkError(
                            message=f"Real-Debrid addTorrent failed (HTTP {status}): {resp}",
                            status_code=status,
                        )
                    )
                return self._finalize_added_torrent(str(resp["id"]), as_queued=as_queued)

    def start_queued_item(self, queued_id: int) -> Result[bool, NetworkError]:
        rd_id = self._id_map.get(queued_id)
        if not rd_id:
            return Ok(False)
        match self._post_form(
            f"/torrents/selectFiles/{urllib.parse.quote(rd_id, safe='')}",
            {"files": "all"},
        ):
            case Err() as err:
                return err
            case Ok((status, _)):
                return Ok(status in (200, 202, 204))
