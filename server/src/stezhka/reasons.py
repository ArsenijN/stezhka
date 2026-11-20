"""Why a ride stopped or slowed down sharply: the nearest thing on the map that explains it.

Sources: OSM nodes (osm.py: traffic signals, crossings, stop / give-way signs, level crossings),
the basemap's `poi` layer (barriers such as gates and bollards, bus stops) and the road network
itself (junctions: three or more ways meet). The first reason in REASONS order within its radius
wins; nothing in reach leaves the reason "unknown" - never a guess. Breaks (long rests) and pauses
are not explained: they are the rider's choice.
"""

from __future__ import annotations

import gzip
import logging
import math
import sqlite3

from . import mvt
from .db import tx
from .roads import EXTENT, ZOOM, Graph, RoadSource, lonlat

log = logging.getLogger(__name__)

REASON_VERSION = 1
# (reason, radius in metres): a stop line is often 10-20 m before a signal node at the junction
REASONS = (
    ("traffic_signals", 30.0),
    ("level_crossing", 25.0),
    ("crossing", 15.0),
    ("stop_sign", 20.0),
    ("barrier", 12.0),
    ("junction", 12.0),
    ("bus_stop", 20.0),
)
EXPLAINED_KINDS = ("stop", "slowdown")
BARRIERS = {"gate", "lift_gate", "bollard", "barrier", "cycle_barrier", "kissing_gate", "stile", "turnstile", "entrance"}
M_PER_DEG = 111_320.0


def _dist_m(lat: float, lon: float, lat2: float, lon2: float) -> float:
    return math.hypot((lon2 - lon) * M_PER_DEG * math.cos(math.radians(lat)), (lat2 - lat) * M_PER_DEG)


class _Pois:
    """Barriers and bus stops from the basemap's poi layer, per z14 tile."""

    def __init__(self, source: RoadSource):
        self._reader = source._reader  # noqa: SLF001 (same file, separate layer)
        self._cache: dict[tuple[int, int], list[tuple[str, float, float]]] = {}

    def _tile(self, tx: int, ty: int) -> list[tuple[str, float, float]]:
        if (tx, ty) not in self._cache:
            out = []
            data = self._reader.get(ZOOM, tx, ty)
            if data:
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                layer = mvt.decode(data, {"poi"}).get("poi")
                if layer:
                    scale = EXTENT // layer.extent if layer.extent <= EXTENT else 1
                    for f in layer.features:
                        cls, sub = f.props.get("class"), f.props.get("subclass")
                        kind = "barrier" if cls in BARRIERS or sub in BARRIERS else "bus_stop" if sub == "bus_stop" else None
                        if kind:
                            for part in f.parts:
                                for x, y in part[:1]:
                                    lon, lat = lonlat(tx * EXTENT + x * scale, ty * EXTENT + y * scale)
                                    out.append((kind, lat, lon))
            self._cache[(tx, ty)] = out
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
        return self._cache[(tx, ty)]

    def near(self, lat: float, lon: float) -> list[tuple[str, float, float]]:
        n = 2**ZOOM
        tx = int((lon + 180) / 360 * n)
        ty = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        return [p for dx in (-1, 0, 1) for dy in (-1, 0, 1) for p in self._tile(tx + dx, ty + dy)]


def _junctions(g: Graph) -> list[tuple[float, float]]:
    """(lat, lon) of every node where three or more segments meet."""
    out = []
    for node, segs in g.adj.items():
        if len(set(segs)) >= 3:
            lon, lat = lonlat(*node)
            out.append((lat, lon))
    return out


def explain(lat: float, lon: float, candidates: list[tuple[str, float, float]]) -> tuple[str, float] | None:
    """The reason for a stop at (lat, lon) among (kind, lat, lon) candidates, with its distance."""
    best: dict[str, float] = {}
    for kind, la, lo in candidates:
        d = _dist_m(lat, lon, la, lo)
        if d < best.get(kind, math.inf):
            best[kind] = d
    for kind, radius in REASONS:
        if best.get(kind, math.inf) <= radius:
            return kind, best[kind]
    return None


def explain_ride(conn: sqlite3.Connection, ride_id: int, source: RoadSource | None, pois: _Pois | None) -> int:
    """Set stops.reason for a ride's stops and slowdowns. Returns how many got a reason."""
    stops = conn.execute(
        f"SELECT id, lat, lon FROM stops WHERE ride_id = ? AND kind IN ({','.join('?' * len(EXPLAINED_KINDS))})",
        (ride_id, *EXPLAINED_KINDS),
    ).fetchall()
    found = []
    if stops:
        m = 0.0005  # ~50 m around the stops
        s = min(r["lat"] for r in stops) - m
        n = max(r["lat"] for r in stops) + m
        w = min(r["lon"] for r in stops) - m
        e = max(r["lon"] for r in stops) + m
        cands: list[tuple[str, float, float]] = [
            (k, la, lo) for k, la, lo in conn.execute(
                "SELECT kind, lat, lon FROM osm_nodes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?", (s, n, w, e)
            )
        ]
        if source is not None:
            cands += [("junction", la, lo) for la, lo in _junctions(source.graph(w, s, e, n))]
        for r in stops:
            near = cands + (pois.near(r["lat"], r["lon"]) if pois is not None else [])
            res = explain(r["lat"], r["lon"], near)
            found.append((res[0] if res else None, round(res[1], 1) if res else None, r["id"]))
    with tx(conn):
        conn.executemany("UPDATE stops SET reason = ?, reason_m = ? WHERE id = ?", found)
        conn.execute("UPDATE rides SET reason_version = ? WHERE id = ?", (REASON_VERSION, ride_id))
    return sum(1 for f in found if f[0])


def explain_pending(conn: sqlite3.Connection, source: RoadSource | None, should_stop=lambda: False) -> int:
    pois = _Pois(source) if source is not None else None
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM rides WHERE reason_version < ? AND trashed_at IS NULL ORDER BY started_at DESC", (REASON_VERSION,)
    )]
    for rid in ids:
        if should_stop():
            break
        try:
            explain_ride(conn, rid, source, pois)
        except Exception:  # one bad ride must not stop the others; logged
            log.exception("explaining stops of ride %s failed", rid)
            conn.execute("UPDATE rides SET reason_version = ? WHERE id = ?", (REASON_VERSION, rid))
    return len(ids)
