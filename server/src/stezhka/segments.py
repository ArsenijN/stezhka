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

from . import feelings, mvt
from .db import get_setting, set_setting, tx
from .matching import MATCH_VERSION, Points, match, segment_geometry
from .roads import EXTENT, ZOOM, RoadSource, Segment

log = logging.getLogger(__name__)

SOURCE_KEY = "roads_source"  # settings: which basemap the stored matches were made on
FULL_PASS = 0.6  # a pass counts for speed when it covers this share of the segment
FULL_PASS_MISSING_M = 8.0  # ... or misses at most this much of it (short pieces at junctions)
MIN_PASS_S = 2.0  # kept for reference: shorter passes borrow time from their neighbours
CONTEXT_S = 4.0  # a pass's speed is measured over at least this much of the ride around it
MAX_PASS_MPS = 16.0  # faster than this is a bad match, never a real ride here
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
    """Every ridden segment with how often and how fast (main recordings of non-trashed rides).

    A pass's speed is its covered distance over its time. A pass under MIN_PASS_S (a few metres at a
    junction) is too coarse alone - time between two fixes is split by distance - so it takes the
    speed of the surrounding stretch of the same ride (neighbouring passes up to CONTEXT_S).
    """
    rows = conn.execute(
        """
        SELECT rs.segment_id, rs.ride_id, rs.seq, rs.enter_t, rs.exit_t, rs.dist_m, s.length_m
        FROM ride_segments rs
        JOIN rides r ON r.id = rs.ride_id AND r.primary_id IS NULL AND r.trashed_at IS NULL
        JOIN road_segments s ON s.id = rs.segment_id
        ORDER BY rs.ride_id, rs.seq
        """
    ).fetchall()
    per: dict[str, dict] = {}
    by_ride: dict[int, list] = {}
    for r in rows:
        by_ride.setdefault(r["ride_id"], []).append(r)
    for seq in by_ride.values():
        for k, r in enumerate(seq):
            st = per.setdefault(r["segment_id"], {"rides": set(), "passes": 0, "speeds": [], "last_t": 0.0})
            st["rides"].add(r["ride_id"])
            st["passes"] += 1
            st["last_t"] = max(st["last_t"], r["exit_t"])
            full = r["dist_m"] >= FULL_PASS * r["length_m"] or r["length_m"] - r["dist_m"] <= FULL_PASS_MISSING_M
            if not full:
                continue
            d, t = r["dist_m"], r["exit_t"] - r["enter_t"]
            lo = hi = k
            while t < CONTEXT_S and (lo > 0 or hi < len(seq) - 1):
                for j in (lo - 1, hi + 1):
                    if 0 <= j < len(seq) and abs(seq[j]["enter_t"] - r["enter_t"]) < 60:
                        d += seq[j]["dist_m"]
                        t += seq[j]["exit_t"] - seq[j]["enter_t"]
                lo, hi = max(lo - 1, 0), min(hi + 1, len(seq) - 1)
            if t > 0 and d / t <= MAX_PASS_MPS:
                st["speeds"].append(d / t)
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


MARK_PASS_SLACK_S = 3.0  # a spot between two passes takes the nearest one this close in time


def _slice(geom: list[list[float]], length_m: float, a: float, b: float) -> list[list[float]]:
    """The part of a segment's lon/lat line between `a` and `b` metres from its start (either order).

    Offsets are in the matcher's metres (seg.length_m); the stored line is measured again here and
    scaled to that length, so the cut lands where the matcher meant it.
    """
    a, b = sorted((max(0.0, min(a, length_m)), max(0.0, min(b, length_m))))
    k = math.cos(math.radians(geom[0][1]))
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(geom, geom[1:]):
        cum.append(cum[-1] + math.hypot((x1 - x0) * k, y1 - y0) * 111_320)
    scale = cum[-1] / length_m if length_m > 0 and cum[-1] > 0 else 1.0
    a, b = a * scale, b * scale

    def at(d: float) -> list[float]:
        for i in range(1, len(cum)):
            if d <= cum[i] or i == len(cum) - 1:
                f = 0.0 if cum[i] == cum[i - 1] else (d - cum[i - 1]) / (cum[i] - cum[i - 1])
                f = max(0.0, min(1.0, f))
                (x0, y0), (x1, y1) = geom[i - 1], geom[i]
                return [round(x0 + f * (x1 - x0), 6), round(y0 + f * (y1 - y0), 6)]
        return list(geom[0])

    out = [at(a)] + [list(geom[i]) for i in range(len(cum)) if a < cum[i] < b] + [at(b)]
    return out


def road_marks(conn: sqlite3.Connection) -> list[dict]:
    """Every mark of a non-trashed main recording, placed on the road segments its ride was matched to.

    A stretch becomes the pieces of each pass it overlaps (the time inside a pass is mapped linearly
    onto the pass's offsets); a spot becomes the point on the pass it falls in. A ride that is not
    matched (yet), or a mark off the road network, keeps the recorded track instead.
    """
    rows = conn.execute(
        """
        SELECT m.id, m.ride_id, m.start_t, m.end_t, m.start_lat, m.start_lon, m.tags, m.note, r.started_at
        FROM ride_marks m JOIN rides r ON r.id = m.ride_id AND r.primary_id IS NULL AND r.trashed_at IS NULL
        ORDER BY m.ride_id, m.start_t
        """
    ).fetchall()
    out = []
    geoms: dict[str, tuple[list, float]] = {}
    for m in rows:
        spot = m["end_t"] - m["start_t"] < 0.5
        lo = m["start_t"] - (MARK_PASS_SLACK_S if spot else 0)
        hi = m["end_t"] + (MARK_PASS_SLACK_S if spot else 0)
        passes = conn.execute(
            "SELECT segment_id, enter_t, exit_t, from_off, to_off FROM ride_segments"
            " WHERE ride_id = ? AND exit_t >= ? AND enter_t <= ? ORDER BY seq",
            (m["ride_id"], lo, hi),
        ).fetchall()
        for p in passes:
            if p["segment_id"] not in geoms:
                s = conn.execute("SELECT geom, length_m FROM road_segments WHERE id = ?", (p["segment_id"],)).fetchone()
                geoms[p["segment_id"]] = (json.loads(s["geom"]), s["length_m"])

        def off(p, t: float) -> float:
            span = p["exit_t"] - p["enter_t"]
            f = 0.5 if span <= 0 else max(0.0, min(1.0, (t - p["enter_t"]) / span))
            return p["from_off"] + f * (p["to_off"] - p["from_off"])

        geometry = None
        on: set[str] = set()  # the segments the mark lies on
        if spot and passes:
            t = m["start_t"]
            p = min(passes, key=lambda p: 0 if p["enter_t"] <= t <= p["exit_t"] else min(abs(t - p["enter_t"]), abs(t - p["exit_t"])))
            line, length = geoms[p["segment_id"]]
            geometry = {"type": "Point", "coordinates": _slice(line, length, off(p, t), off(p, t))[0]}
            on.add(p["segment_id"])
        elif passes:
            parts = []
            for p in passes:
                line, length = geoms[p["segment_id"]]
                a, b = off(p, max(m["start_t"], p["enter_t"])), off(p, min(m["end_t"], p["exit_t"]))
                if abs(b - a) >= 0.5:
                    parts.append(_slice(line, length, a, b))
                    on.add(p["segment_id"])
            if parts:
                geometry = {"type": "MultiLineString", "coordinates": parts}
        if geometry is None:  # not matched: the recorded track itself
            pts = conn.execute(
                "SELECT lon, lat FROM points WHERE ride_id = ? AND t BETWEEN ? AND ? ORDER BY idx",
                (m["ride_id"], m["start_t"], m["end_t"]),
            ).fetchall()
            if spot or len(pts) < 2:
                geometry = {"type": "Point", "coordinates": [m["start_lon"], m["start_lat"]]}
            else:
                geometry = {"type": "MultiLineString", "coordinates": [[[r["lon"], r["lat"]] for r in pts]]}
        out.append({
            "id": m["id"], "ride_id": m["ride_id"], "ride_started_at": m["started_at"],
            "tags": feelings.load_tags(m["tags"]), "note": m["note"],
            "segments": sorted(on),
            "geom": geometry,
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
