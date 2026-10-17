"""Pure bencode decoder, `.torrent` parser, and magnet URI extractor."""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from typing import Any

from contraband.core.errors import BencodeError, ParseError
from contraband.core.result import Err, Ok, Result
from contraband.core.types import MagnetTarget, ParsedTorrent, TorrentFileEntry

HEX_HASH_40_RE = re.compile(r"^[a-fA-F0-9]{40}$")
BTIH_URN_RE = re.compile(r"urn:btih:([a-zA-Z0-9]+)", re.IGNORECASE)
MAGNET_TEXT_RE = re.compile(r"magnet:\?xt=urn:btih:[^\s\)\"'>\]|]+", re.IGNORECASE)


def bdecode_with_spans(data: bytes, idx: int = 0) -> Result[tuple[Any, int], BencodeError]:
    """
    Pure recursive bencode decoder that retains `(decoded_value, raw_bytes_span)`
    for dictionary values so the exact `info` dictionary SHA-1 hash can be computed.
    """
    if idx >= len(data):
        return Err(BencodeError("Unexpected end of bencoded buffer."))

    ch = data[idx : idx + 1]
    if ch == b"i":
        end = data.find(b"e", idx + 1)
        if end == -1:
            return Err(BencodeError(f"Unterminated integer at offset {idx}."))
        try:
            return Ok((int(data[idx + 1 : end]), end + 1))
        except ValueError as exc:
            return Err(BencodeError(f"Invalid integer at offset {idx}: {exc}"))

    if ch == b"l":
        cur = idx + 1
        out_list: list[Any] = []
        while cur < len(data) and data[cur : cur + 1] != b"e":
            match bdecode_with_spans(data, cur):
                case Err() as err:
                    return err
                case Ok((val, next_idx)):
                    out_list.append(val)
                    cur = next_idx
        if cur >= len(data):
            return Err(BencodeError(f"Unterminated list starting at offset {idx}."))
        return Ok((out_list, cur + 1))

    if ch == b"d":
        cur = idx + 1
        out_dict: dict[bytes, tuple[Any, bytes]] = {}
        while cur < len(data) and data[cur : cur + 1] != b"e":
            match bdecode_with_spans(data, cur):
                case Err() as err:
                    return err
                case Ok((key, val_start)):
                    if not isinstance(key, bytes):
                        return Err(BencodeError(f"Dictionary key at offset {cur} is not bytes."))
                    match bdecode_with_spans(data, val_start):
                        case Err() as err:
                            return err
                        case Ok((val, next_idx)):
                            out_dict[key] = (val, data[val_start:next_idx])
                            cur = next_idx
        if cur >= len(data):
            return Err(BencodeError(f"Unterminated dictionary starting at offset {idx}."))
        return Ok((out_dict, cur + 1))

    colon = data.find(b":", idx)
    if colon == -1:
        return Err(BencodeError(f"Invalid byte string prefix at offset {idx}."))
    try:
        length = int(data[idx:colon])
    except ValueError as exc:
        return Err(BencodeError(f"Invalid byte string length at offset {idx}: {exc}"))
    if length < 0 or colon + 1 + length > len(data):
        return Err(BencodeError(f"Byte string length {length} exceeds buffer size."))
    start = colon + 1
    return Ok((data[start : start + length], start + length))


def parse_torrent_bytes(
    raw: bytes,
    fallback_name: str = "torrent",
) -> Result[ParsedTorrent, BencodeError]:
    """Purely parses `.torrent` file bytes into an immutable `ParsedTorrent`."""
    match bdecode_with_spans(raw, 0):
        case Err() as err:
            return err
        case Ok((top, _)):
            pass

    if not isinstance(top, dict):
        return Err(BencodeError("Top-level bencode structure is not a dictionary."))

    info_entry = top.get(b"info")
    if not isinstance(info_entry, tuple) or len(info_entry) != 2:
        return Err(BencodeError("Invalid .torrent payload: missing 'info' dictionary."))

    info_dict, info_raw_bytes = info_entry
    if not isinstance(info_dict, dict) or not isinstance(info_raw_bytes, bytes):
        return Err(BencodeError("Invalid .torrent 'info' section."))

    info_hash = hashlib.sha1(info_raw_bytes).hexdigest().lower()

    name_tuple = info_dict.get(b"name")
    if isinstance(name_tuple, tuple) and isinstance(name_tuple[0], bytes):
        name = name_tuple[0].decode("utf-8", errors="replace")
    else:
        name = fallback_name

    files_out: list[TorrentFileEntry] = []
    total_size = 0

    files_tuple = info_dict.get(b"files")
    if isinstance(files_tuple, tuple) and isinstance(files_tuple[0], list):
        for idx, f_entry in enumerate(files_tuple[0]):
            if not isinstance(f_entry, dict):
                continue
            len_tuple = f_entry.get(b"length", (0, b""))
            f_len = int(len_tuple[0]) if isinstance(len_tuple, tuple) else 0
            path_tuple = f_entry.get(b"path", ([], b""))
            raw_parts = path_tuple[0] if isinstance(path_tuple, tuple) else []
            path_parts = [
                p.decode("utf-8", errors="replace")
                for p in raw_parts
                if isinstance(p, bytes)
            ]
            f_path = "/".join(path_parts) or f"file_{idx}"
            files_out.append(TorrentFileEntry(index=idx, name=f_path, size_bytes=f_len))
            total_size += f_len
    else:
        len_tuple = info_dict.get(b"length", (0, b""))
        total_size = int(len_tuple[0]) if isinstance(len_tuple, tuple) else 0
        files_out.append(TorrentFileEntry(index=0, name=name, size_bytes=total_size))

    return Ok(
        ParsedTorrent(
            info_hash=info_hash,
            name=name,
            total_bytes=total_size,
            files=tuple(files_out),
        )
    )


def parse_magnet_uri(raw: str) -> Result[MagnetTarget, ParseError]:
    """
    Purely extracts `(info_hash, magnet_uri, display_name)` from a `magnet:?` link
    or a raw 40-char hex / 32-char base32 infoHash.
    """
    cleaned = raw.strip()
    if HEX_HASH_40_RE.fullmatch(cleaned):
        ih = cleaned.lower()
        return Ok(MagnetTarget(info_hash=ih, magnet_uri=f"magnet:?xt=urn:btih:{ih}", display_name=None))

    if not cleaned.lower().startswith("magnet:?"):
        return Err(ParseError(f"Not a valid magnet URI or 40-char infoHash: {cleaned}"))

    parsed = urllib.parse.urlparse(cleaned)
    qs = urllib.parse.parse_qs(parsed.query)
    dn_raw = qs.get("dn", [None])[0]
    dn = urllib.parse.unquote_plus(dn_raw) if dn_raw else None

    for xt in qs.get("xt", []):
        m = BTIH_URN_RE.search(xt)
        if m:
            hash_token = m.group(1)
            if len(hash_token) == 40 and HEX_HASH_40_RE.fullmatch(hash_token):
                return Ok(
                    MagnetTarget(
                        info_hash=hash_token.lower(),
                        magnet_uri=cleaned,
                        display_name=dn,
                    )
                )
            if len(hash_token) == 32:
                try:
                    decoded_hex = base64.b32decode(hash_token.upper()).hex().lower()
                    return Ok(
                        MagnetTarget(
                            info_hash=decoded_hex,
                            magnet_uri=cleaned,
                            display_name=dn,
                        )
                    )
                except Exception:
                    continue

    return Err(ParseError(f"Magnet URI is missing a valid urn:btih infoHash: {cleaned}"))


def extract_magnets_from_text(text: str) -> tuple[MagnetTarget, ...]:
    """Purely extracts and deduplicates all `magnet:?xt=urn:btih:...` links from Markdown/plain text."""
    raw_matches = MAGNET_TEXT_RE.findall(text)
    seen_hashes: set[str] = set()
    ordered: list[MagnetTarget] = []
    for raw_uri in raw_matches:
        match parse_magnet_uri(raw_uri):
            case Ok(target):
                if target.info_hash not in seen_hashes:
                    seen_hashes.add(target.info_hash)
                    ordered.append(target)
            case Err():
                continue
    return tuple(ordered)
