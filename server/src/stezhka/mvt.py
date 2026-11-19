"""Minimal Mapbox Vector Tile (MVT 2.1) decoder: layers, features, tags, line/point geometry.

Only what roads.py needs, so no protobuf dependency. Spec:
https://github.com/mapbox/vector-tile-spec/tree/master/2.1
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf: bytes):
    """Yield (field number, wire type, value) of one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 2:
            ln, i = _varint(buf, i)
            val = buf[i:i + ln]
            i += ln
        elif wire == 5:
            val = buf[i:i + 4]
            i += 4
        elif wire == 1:
            val = buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield field, wire, val


def _packed(buf: bytes) -> list[int]:
    out, i = [], 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _zigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _value(buf: bytes):
    for field, _wire, val in _fields(buf):
        if field == 1:
            return val.decode("utf-8")
        if field == 2:
            return struct.unpack("<f", val)[0]
        if field == 3:
            return struct.unpack("<d", val)[0]
        if field in (4, 5):
            return val
        if field == 6:
            return _zigzag(val)
        if field == 7:
            return bool(val)
    return None


POINT, LINESTRING, POLYGON = 1, 2, 3


@dataclass(slots=True)
class Feature:
    type: int
    props: dict
    parts: list[list[tuple[int, int]]]  # tile coordinates, 0..extent


@dataclass(slots=True)
class Layer:
    name: str
    extent: int
    features: list[Feature]


def _geometry(cmds: list[int]) -> list[list[tuple[int, int]]]:
    parts: list[list[tuple[int, int]]] = []
    x = y = 0
    i = 0
    while i < len(cmds):
        cmd, count = cmds[i] & 7, cmds[i] >> 3
        i += 1
        if cmd == 7:  # ClosePath
            if parts and parts[-1]:
                parts[-1].append(parts[-1][0])
            continue
        for _ in range(count):
            x += _zigzag(cmds[i])
            y += _zigzag(cmds[i + 1])
            i += 2
            if cmd == 1:  # MoveTo starts a part
                parts.append([(x, y)])
            else:  # LineTo
                parts[-1].append((x, y))
    return parts


def decode(data: bytes, layers: set[str] | None = None) -> dict[str, Layer]:
    """Decode an (uncompressed) tile. `layers`: only these (saves time on big tiles)."""
    out: dict[str, Layer] = {}
    for field, _w, raw in _fields(data):
        if field != 3:
            continue
        name, extent, keys, values, feats = "", 4096, [], [], []
        for f, _w2, v in _fields(raw):
            if f == 1:
                name = v.decode("utf-8")
            elif f == 5:
                extent = v
            elif f == 3:
                keys.append(v.decode("utf-8"))
            elif f == 4:
                values.append(_value(v))
            elif f == 2:
                feats.append(v)
        if layers is not None and name not in layers:
            continue
        features = []
        for fb in feats:
            ftype, tags, geom = 0, [], []
            for f, _w3, v in _fields(fb):
                if f == 3:
                    ftype = v
                elif f == 2:
                    tags = _packed(v)
                elif f == 4:
                    geom = _packed(v)
            props = {keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)}
            features.append(Feature(ftype, props, _geometry(geom)))
        out[name] = Layer(name, extent, features)
    return out
