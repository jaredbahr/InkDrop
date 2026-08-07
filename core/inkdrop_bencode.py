#!/usr/bin/env python3
"""Minimal bencode/torrent-file decoding, shared by inkdrop_missing_acquire
and inkdrop_source_providers -- previously two near-verbatim copies."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


def bdecode_value(data):
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("bencode payload must be bytes")
    data = bytes(data)

    def parse(index):
        if index >= len(data):
            raise ValueError("unexpected end of bencode payload")
        token = data[index:index + 1]
        if token == b"i":
            end = data.index(b"e", index)
            return int(data[index + 1:end]), end + 1
        if token == b"l":
            values = []
            index += 1
            while index < len(data) and data[index:index + 1] != b"e":
                value, index = parse(index)
                values.append(value)
            if index >= len(data):
                raise ValueError("unterminated bencode list")
            return values, index + 1
        if token == b"d":
            values = {}
            index += 1
            while index < len(data) and data[index:index + 1] != b"e":
                key, index = parse(index)
                value, index = parse(index)
                values[key] = value
            if index >= len(data):
                raise ValueError("unterminated bencode dict")
            return values, index + 1
        if token.isdigit():
            colon = data.index(b":", index)
            length = int(data[index:colon])
            start = colon + 1
            end = start + length
            if end > len(data):
                raise ValueError("bencode string length exceeds payload")
            return data[start:end], end
        raise ValueError(f"unexpected bencode token: {token!r}")

    value, offset = parse(0)
    if offset > len(data):
        raise ValueError("bencode parse exceeded payload")
    return value


def torrent_dict_get(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        for key in (name, str(name).encode("utf-8")):
            if key in mapping:
                return mapping.get(key)
    return None


def torrent_text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1", errors="replace")
    if value is None:
        return ""
    return str(value)


def torrent_path_text(value):
    if isinstance(value, list):
        parts = [torrent_text(item).strip(" /\\") for item in value]
        return "/".join(part for part in parts if part)
    return torrent_text(value).strip(" /\\")
