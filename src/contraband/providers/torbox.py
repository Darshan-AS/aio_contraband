"""TorBox cloud debrid provider implementing `DebridProviderProtocol`."""

import uuid
from collections.abc import Mapping
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

TORBOX_API_BASE = "https://api.torbox.app/v1/api"
MAX_ESSENTIAL_BYTES = 200 * (1024**3)  # 200 GiB warning threshold


def build_multipart_form_body(
    fields: Mapping[str, str],
    *,
    file_field: tuple[str, str, str, bytes] | None = None,
    boundary: str | None = None,
) -> tuple[str, bytes]:
    """
    Pure function: encodes `fields` and an optional `(field_name, filename, content_type, data)`
    into a `multipart/form-data` payload, returning `(content_type_header, body_bytes)`.
    """
    sep = boundary or f"----StreamWeaverBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    if file_field is not None:
        f_name, filename, c_type, file_bytes = file_field
        safe_filename = filename.replace('"', "").replace("\r", "").replace("\n", "")
        chunks.extend(
            (
                f"--{sep}\r\n".encode(),
                f'Content-Disposition: form-data; name="{f_name}"; filename="{safe_filename}"\r\n'.encode(),
                f"Content-Type: {c_type}\r\n\r\n".encode(),
                file_bytes,
                b"\r\n",
            )
        )

    for k, v in fields.items():
        chunks.extend(
            (
                f"--{sep}\r\n".encode(),
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode(),
                f"{v}\r\n".encode(),
            )
        )

    chunks.append(f"--{sep}--\r\n".encode())
    return f"multipart/form-data; boundary={sep}", b"".join(chunks)


def parse_torbox_files(raw_files: Any) -> tuple[TorrentFileEntry, ...]:
    """Pure function: normalizes TorBox file objects into `TorrentFileEntry` tuples."""
    if not isinstance(raw_files, list):
        return ()
    entries: list[TorrentFileEntry] = []
    for idx, f in enumerate(raw_files):
        if not isinstance(f, dict):
            continue
        raw_id = f.get("id")
        file_idx = int(raw_id) if raw_id is not None and int(raw_id) < 500 else idx
        raw_name = str(f.get("short_name") or f.get("name") or f"file_{idx}")
        size = int(f.get("size") or 0)
        entries.append(TorrentFileEntry(index=file_idx, name=raw_name, size_bytes=size))
    return tuple(entries)


def parse_torbox_torrent_item(item: Mapping[str, Any]) -> DebridTorrentItem | None:
    """Pure function: converts a TorBox `/torrents/mylist` dictionary into `DebridTorrentItem`."""
    t_id = int(item.get("id") or 0)
    if t_id <= 0:
        return None
    return DebridTorrentItem(
        id=t_id,
        info_hash=str(item.get("hash") or "").lower(),
        name=str(item.get("name") or "Untitled"),
        size_bytes=int(item.get("size") or 0),
        download_state=str(item.get("download_state") or "unknown"),
        progress=float(item.get("progress") or 0.0),
        download_finished=bool(item.get("download_finished")),
        files=parse_torbox_files(item.get("files")),
    )


class TorBoxProvider:
    """Extensible `DebridProviderProtocol` implementation for TorBox."""

    def __init__(
        self,
        api_key: str,
        http_client: HttpClientProtocol,
        *,
        base_url: str = TORBOX_API_BASE,
        max_active: int = 3,
    ) -> None:
        self._api_key = api_key
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._max_active = max_active

    @property
    def name(self) -> str:
        return "TorBox"

    @property
    def max_active_downloads(self) -> int:
        return self._max_active

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        raw_body: bytes | None = None,
    ) -> Result[tuple[int, Any], NetworkError]:
        url = f"{self._base_url}{endpoint}"
        req_headers = {"Authorization": f"Bearer {self._api_key}"}
        if headers:
            req_headers.update(headers)
        match self._http.request_json(
            method,
            url,
            headers=req_headers,
            json_body=json_body,
            raw_body=raw_body,
        ):
            case Err() as err:
                return err
            case Ok(resp):
                return Ok((resp.status_code, resp.data))

    def check_cached(self, info_hash: str) -> Result[DebridCacheStatus, NetworkError]:
        ih = info_hash.lower()
        match self._request(
            "GET",
            f"/torrents/checkcached?hash={ih}&format=object&list_files=true",
        ):
            case Err() as err:
                return err
            case Ok((status, data)):
                if status == 200 and isinstance(data, dict):
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        hit = inner.get(ih) or inner.get(ih.upper())
                        if isinstance(hit, dict):
                            return Ok(
                                DebridCacheStatus(
                                    info_hash=ih,
                                    is_cached=True,
                                    size_bytes=int(hit.get("size") or 0),
                                    files=parse_torbox_files(hit.get("files")),
                                )
                            )
                return Ok(DebridCacheStatus(info_hash=ih, is_cached=False))

    def list_library(self) -> Result[tuple[DebridTorrentItem, ...], NetworkError]:
        match self._request("GET", "/torrents/mylist?bypass_cache=true&limit=1000"):
            case Err() as err:
                return err
            case Ok((status, data)):
                if status != 200 or not isinstance(data, dict):
                    return Err(
                        NetworkError(
                            message=f"TorBox library fetch failed (HTTP {status}): {data}",
                            status_code=status,
                        )
                    )
                raw_items = data.get("data") or []
                if not isinstance(raw_items, list):
                    return Ok(())
                parsed = [
                    item
                    for raw in raw_items
                    if isinstance(raw, dict)
                    and (item := parse_torbox_torrent_item(raw)) is not None
                ]
                return Ok(tuple(parsed))

    def list_queued(self) -> Result[tuple[DebridQueuedItem, ...], NetworkError]:
        match self._request("GET", "/queued/getqueued?bypass_cache=true&limit=1000"):
            case Err() as err:
                return err
            case Ok((status, data)):
                if status != 200 or not isinstance(data, dict):
                    return Ok(())
                raw_items = data.get("data") or []
                if not isinstance(raw_items, list):
                    return Ok(())
                out: list[DebridQueuedItem] = []
                for q in raw_items:
                    if isinstance(q, dict) and q.get("id"):
                        out.append(
                            DebridQueuedItem(
                                id=int(q["id"]),
                                info_hash=str(q.get("hash") or "").lower(),
                                name=str(q.get("name") or q.get("hash") or "Untitled"),
                            )
                        )
                return Ok(tuple(out))

    def fetch_torrent_files(
        self, torrent_id: int
    ) -> Result[tuple[TorrentFileEntry, ...], NetworkError]:
        if torrent_id <= 0:
            return Ok(())
        match self._request("GET", f"/torrents/mylist?id={torrent_id}&bypass_cache=true"):
            case Err() as err:
                return err
            case Ok((status, resp)):
                if status == 200 and isinstance(resp, dict):
                    data = resp.get("data")
                    if isinstance(data, dict) and int(data.get("id") or 0) == torrent_id:
                        return Ok(parse_torbox_files(data.get("files")))
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and int(item.get("id") or 0) == torrent_id:
                                return Ok(parse_torbox_files(item.get("files")))
                return Ok(())

    def add_magnet(
        self,
        magnet_uri: str,
        *,
        name: str | None = None,
        as_queued: bool = False,
    ) -> Result[DebridAddOutcome, NetworkError]:
        fields: dict[str, str] = {"magnet": magnet_uri, "allow_zip": "true"}
        if as_queued:
            fields["as_queued"] = "true"
        if name:
            fields["name"] = name

        content_type, body = build_multipart_form_body(fields)
        match self._request(
            "POST",
            "/torrents/createtorrent",
            headers={"Content-Type": content_type},
            raw_body=body,
        ):
            case Err() as err:
                return err
            case Ok((status, resp)):
                is_ok = status in (200, 201) and isinstance(resp, dict) and bool(resp.get("success"))
                if not is_ok and not as_queued:
                    return self.add_magnet(magnet_uri, name=name, as_queued=True)
                if not is_ok:
                    detail = resp.get("detail") if isinstance(resp, dict) else str(resp)
                    return Err(
                        NetworkError(
                            message=f"TorBox add_magnet failed (HTTP {status}): {detail}",
                            status_code=status,
                        )
                    )
                return Ok(_parse_add_outcome(resp))

    def add_torrent_file(
        self,
        torrent_path: Path,
        file_bytes: bytes,
        *,
        as_queued: bool = False,
    ) -> Result[DebridAddOutcome, NetworkError]:
        fields: dict[str, str] = {"as_queued": "true"} if as_queued else {}
        content_type, body = build_multipart_form_body(
            fields,
            file_field=("file", torrent_path.name, "application/x-bittorrent", file_bytes),
        )
        match self._request(
            "POST",
            "/torrents/createtorrent",
            headers={"Content-Type": content_type},
            raw_body=body,
        ):
            case Err() as err:
                return err
            case Ok((status, resp)):
                is_ok = status in (200, 201) and isinstance(resp, dict) and bool(resp.get("success"))
                if not is_ok and not as_queued:
                    return self.add_torrent_file(torrent_path, file_bytes, as_queued=True)
                if not is_ok:
                    detail = resp.get("detail") if isinstance(resp, dict) else str(resp)
                    return Err(
                        NetworkError(
                            message=f"TorBox add_torrent_file failed (HTTP {status}): {detail}",
                            status_code=status,
                        )
                    )
                return Ok(_parse_add_outcome(resp))

    def start_queued_item(self, queued_id: int) -> Result[bool, NetworkError]:
        match self._request(
            "POST",
            "/queued/controlqueued",
            json_body={"queued_id": queued_id, "operation": "start"},
        ):
            case Err() as err:
                return err
            case Ok((status, resp)):
                return Ok(
                    status == 200 and isinstance(resp, dict) and bool(resp.get("success"))
                )


def _parse_add_outcome(resp: Mapping[str, Any]) -> DebridAddOutcome:
    detail = str(resp.get("detail") or "Added to TorBox Cloud Library")
    data_obj = resp.get("data")
    t_id: int | None = None
    q_id: int | None = None
    if isinstance(data_obj, dict):
        if data_obj.get("torrent_id") is not None:
            t_id = int(data_obj["torrent_id"])
        if data_obj.get("queued_id") is not None:
            q_id = int(data_obj["queued_id"])
    return DebridAddOutcome(
        detail=detail,
        torrent_id=t_id,
        queued_id=q_id,
        was_cached="Cached" in detail,
    )
