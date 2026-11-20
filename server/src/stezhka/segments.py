"""Ridden road segments: map matching of stored rides, and per-segment statistics.

Rides are matched in the background (MatchWorker): local CPU work on the self-hosted basemap, no
network. Only segments that were ridden are stored (road_segments), plus every pass over them
(ride_segments). When the basemap is refreshed, segment geometry and ids can change, so every ride
is matched again (rides.match_version reset) and stats follow the new map.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import sqlite3
import statistics
import threading
from pathlib import Path

from . import mvt
from .db import get_setting, set_setting, tx
from .matching import MATCH_VERSION, Points, match, segment_geometry
from .roads import EXTENT, ZOOM, RoadSource, Segment

log = logging.getLogger(__name__)

SOURCE_KEY = "roads_source"  # settings: which basemap the stored matches were made on
FULL_PASS = 0.6  # a pass counts for speed when it covers this share of the segment
MIN_PASS_S = 2.0  # ... and lasts at least this long (shorter ones are timing noise)
MAX_PASS_MPS = 16.0  # ... and is not faster than this (a bad match, never a real ride here)
NAME_NEAR_UNITS = 10  # ~4 m: a street name line this close to a segment's middle names it


def basemap_file(basemap_dir: Path) -> Path:
    return basemap_dir / "region.pmtiles"


def basemap_version(basemap_dir: Path) -> str | None:
    meta = basemap_dir / "region.json"
    try:
        info = json.loads(meta.read_text())
        return f"{info.get('ofm_version')}|{info.get('created_at')}"
    except (OSError, ValueError):
        return None


class _Names:
    """Street names from the tiles' transportation_name layer, looked up near a segment's middle."""

    def __init__(self, source: RoadSource):
        self._reader = source._reader  # noqa: SLF001 (same file, separate layer)
        self._cache: dict[tuple[int, int], list[tuple[dict, list[tuple[int, int]]]]] = {}

    def _tile(self, tx: int, ty: int):
        key = (tx, ty)
        if key not in self._cache:
            data = self._reader.get(ZOOM, tx, ty)
            lines = []
            if data:
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                layer = mvt.decode(data, {"transportation_name"}).get("transportation_name")
                if layer:
                    scale = EXTENT // layer.extent if layer.extent <= EXTENT else 1
                    for f in layer.features:
                        for part in f.parts:
                            lines.append((f.props, [(tx * EXTENT + x * scale, ty * EXTENT + y * scale) for x, y in part]))
            self._cache[key] = lines
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
        return self._cache[key]

    def lookup(self, seg: Segment) -> tuple[str | None, str | None]:
        mx, my = seg.coords[len(seg.coords) // 2]
        if len(seg.coords) == 2:
            (x0, y0), (x1, y1) = seg.coords
            mx, my = (x0 + x1) // 2, (y0 + y1) // 2
        best = None
        for props, coords in self._tile(mx // EXTENT, my // EXTENT):
            if props.get("class") != seg.props.get("class"):
                continue
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                dx, dy = x1 - x0, y1 - y0
                ll = dx * dx + dy * dy
                t = 0.0 if ll == 0 else max(0.0, min(1.0, ((mx - x0) * dx + (my - y0) * dy) / ll))
                d = math.hypot(x0 + t * dx - mx, y0 + t * dy - my)
                if d <= NAME_NEAR_UNITS and (best is None or d < best[0]):
                    best = (d, props)
        if best is None:
            return None, None
        p = best[1]
        local = p.get("name:uk") or p.get("name")
        latin = p.get("name:en") or p.get("name_en") or p.get("name:latin")
        return local, (latin if latin != local else None)


def _points(conn: sqlite3.Connection, ride_id: int) -> Points:
    rows = conn.execute(
        "SELECT t, lat, lon, dist, segment, moving FROM points WHERE ride_id = ? ORDER BY idx", (ride_id,)
    ).fetchall()
    return Points(*[list(col) for col in zip(*rows)]) if rows else Points([], [], [], [], [], [])


def match_ride(conn: sqlite3.Connection, source: RoadSource, names: _Names, ride_id: int) -> int:
    """Match one ride and store its passes. Returns the number of passes."""
    pts = _points(conn, ride_id)
    trs = []
    if pts.t:
        m = 0.003  # ~300 m around the ride
        g = source.graph(min(pts.lon) - m, min(pts.lat) - m, max(pts.lon) + m, max(pts.lat) + m)
        trs = match(g, pts)
        known = {r[0] for r in conn.execute(
            f"SELECT id FROM road_segments WHERE id IN ({','.join('?' * len({t.segment_id for t in trs}))})",
            list({t.segment_id for t in trs}),
        )} if trs else set()
        new_segments = []
        for sid in {t.segment_id for t in trs} - known:
            seg = g.segments[sid]
            name, name_en = names.lookup(seg)
            geom = [[round(lon, 6), round(lat, 6)] for lon, lat in segment_geometry(seg)]
            p = seg.props
            new_segments.append((sid, p.get("class"), p.get("subclass"), p.get("surface"), p.get("brunnel"),
                                 name, name_en, seg.length_m, json.dumps(geom, separators=(",", ":"))))
    with tx(conn):
        conn.execute("DELETE FROM ride_segments WHERE ride_id = ?", (ride_id,))
        if trs:
            conn.executemany(
                "INSERT OR IGNORE INTO road_segments(id, class, subclass, surface, brunnel, name, name_en, length_m, geom)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                new_segments,
            )
            conn.executemany(
                "INSERT INTO ride_segments(ride_id, seq, segment_id, enter_t, exit_t, from_off, to_off, dist_m)"
                " VALUES (?,?,?,?,?,?,?,?)",
                ((ride_id, i, t.segment_id, t.enter_t, t.exit_t, t.from_off, t.to_off, t.dist_m) for i, t in enumerate(trs)),
            )
        conn.execute("UPDATE rides SET match_version = ?, match_error = NULL WHERE id = ?", (MATCH_VERSION, ride_id))
    return len(trs)


def pending(conn: sqlite3.Connection, limit: int = 50) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM rides WHERE match_version < ? AND trashed_at IS NULL ORDER BY started_at DESC LIMIT ?",
        (MATCH_VERSION, limit),
    )]


def match_pending(conn: sqlite3.Connection, basemap_dir: Path, should_stop=lambda: False) -> int:
    """Match every ride that needs it. Re-matches everything after a basemap refresh."""
    path = basemap_file(basemap_dir)
    if not path.is_file():
        return 0
    version = basemap_version(basemap_dir)
    if version and get_setting(conn, SOURCE_KEY) != version:
        with tx(conn):
            conn.execute("UPDATE rides SET match_version = 0")
            set_setting(conn, SOURCE_KEY, version)
        log.info("basemap changed (%s): matching all rides again", version)
    source = RoadSource(path)
    names = _Names(source)
    done = 0
    try:
        seen: set[int] = set()
        while not should_stop():
            ids = [i for i in pending(conn) if i not in seen]
            if not ids:
                break
            for rid in ids:
                seen.add(rid)
                try:
                    match_ride(conn, source, names, rid)
                    done += 1
                except Exception as e:  # one bad ride must not stop the others; logged
                    log.exception("matching ride %s failed", rid)
                    conn.execute("UPDATE rides SET match_version = ?, match_error = ? WHERE id = ?",
                                 (MATCH_VERSION, str(e)[:500], rid))
        # segments no ride uses any more (after a refresh or deletions)
        conn.execute("DELETE FROM road_segments WHERE id NOT IN (SELECT segment_id FROM ride_segments)")
    finally:
        source.close()
    if done:
        log.info("matched %d ride(s) to the road network", done)
    return done


def road_stats(conn: sqlite3.Connection) -> list[dict]:
    """Every ridden segment with how often and how fast (main recordings of non-trashed rides)."""
    rows = conn.execute(
        """
        SELECT rs.segment_id, rs.ride_id, rs.enter_t, rs.exit_t, rs.dist_m, s.length_m
        FROM ride_segments rs
        JOIN rides r ON r.id = rs.ride_id AND r.primary_id IS NULL AND r.trashed_at IS NULL
        JOIN road_segments s ON s.id = rs.segment_id
        """
    ).fetchall()
    per: dict[str, dict] = {}
    for r in rows:
        st = per.setdefault(r["segment_id"], {"rides": set(), "passes": 0, "speeds": [], "last_t": 0.0})
        st["rides"].add(r["ride_id"])
        st["passes"] += 1
        st["last_t"] = max(st["last_t"], r["exit_t"])
        dt = r["exit_t"] - r["enter_t"]
        if r["dist_m"] >= FULL_PASS * r["length_m"] and dt >= MIN_PASS_S and r["dist_m"] / dt <= MAX_PASS_MPS:
            st["speeds"].append(r["dist_m"] / dt)
    if not per:
        return []
    segs = {s["id"]: s for s in conn.execute(
        f"SELECT * FROM road_segments WHERE id IN ({','.join('?' * len(per))})", list(per)
    )}
    out = []
    for sid, st in per.items():
        s = segs[sid]
        sp = st["speeds"]
        out.append({
            "id": sid, "class": s["class"], "subclass": s["subclass"], "surface": s["surface"], "brunnel": s["brunnel"],
            "name": s["name"], "name_en": s["name_en"], "length_m": round(s["length_m"], 1),
            "rides": len(st["rides"]), "passes": st["passes"], "last_t": st["last_t"],
            "speed_mps": round(statistics.median(sp), 2) if sp else None,
            "best_mps": round(max(sp), 2) if sp else None,
            "geom": json.loads(s["geom"]),
        })
    return out


class MatchWorker:
    """Background thread: match pending rides every `poll_s`, or right after wake()."""

    def __init__(self, connect, basemap_dir: Path, poll_s: float = 600.0):
        self._connect = connect
        self._basemap_dir = basemap_dir
        self._poll_s = poll_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._connect()
                try:
                    match_pending(conn, self._basemap_dir, self._stop.is_set)
                finally:
                    conn.close()
            except Exception:  # keep the worker alive; logged with traceback
                log.exception("map matching worker failed")
            self._wake.wait(self._poll_s)
            self._wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="stezhka-match", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)
