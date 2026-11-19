"""Road network from the self-hosted basemap: segments between junctions, built on demand.

The basemap's z14 vector tiles (OpenMapTiles schema, `transportation` layer) hold every road and
path at full detail, with class / subclass / surface / bridge, and street names in
`transportation_name`. So the road graph needs no download of its own, and it follows the monthly
map refresh. (Detailed OSM tags such as smoothness or mtb:scale are not in the tiles; see
docs/decisions.md D11.)

Coordinates are integers in a global grid: z14 tile x/y * 4096 + tile pixel, i.e. z26 pixels
(~0.4 m here). Within a tile, lines are clipped to the tile's own square, so neighbouring tiles
meet exactly on the border, where their border nodes are merged.

Junctions: a vertex shared by two lines, or a line end lying on another line. Simplified tiles
drop many junction vertices (measured: 27 % of street ends lie on the other street's line but not
on one of its vertices), so line ends are snapped onto any line within SNAP_UNITS.

A segment is a piece of road between two nodes (junctions, dead ends, tile borders). Its id is a
hash of its class and its geometry, so it is stable while the map data does not change there.
"""

from __future__ import annotations

import gzip
import hashlib
import heapq
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import mvt

ZOOM = 14
EXTENT = 4096
WORLD = EXTENT * (1 << ZOOM)  # global grid size
SNAP_UNITS = 4  # ~1.5 m: a line end this close to another line joins it

# Classes a bicycle can be on (OpenMapTiles `transportation.class`). Motorways stay in: a map match
# onto a motorway is a sign of a bad fix, and leaving them out would not make it better.
ROUTABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "minor", "service", "track", "path"}
NOT_RIDDEN_SUBCLASS = {"platform", "steps", "ski"}
ROADS_VERSION = 1


def lonlat(gx: float, gy: float) -> tuple[float, float]:
    lon = gx / WORLD * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / WORLD))))
    return lon, lat


def grid(lon: float, lat: float) -> tuple[float, float]:
    x = (lon + 180.0) / 360.0 * WORLD
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * WORLD
    return x, y


def metres_per_unit(lat: float) -> float:
    return 40_075_016.686 * math.cos(math.radians(lat)) / WORLD


def tiles_for_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[tuple[int, int]]:
    x0, y1 = grid(min_lon, min_lat)
    x1, y0 = grid(max_lon, max_lat)
    return [(tx, ty) for tx in range(int(x0) // EXTENT, int(x1) // EXTENT + 1)
            for ty in range(int(y0) // EXTENT, int(y1) // EXTENT + 1)]


# --- clipping ------------------------------------------------------------------------------------


def _clip_segment(a, b, lo_x, lo_y, hi_x, hi_y):
    """Liang-Barsky. Returns the clipped (a', b') or None. Computed from the lexicographically
    smaller end so two tiles clipping the same segment get the same border point."""
    swap = b < a
    if swap:
        a, b = b, a
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - lo_x), (dx, hi_x - a[0]), (-dy, a[1] - lo_y), (dy, hi_y - a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    pa = (round(a[0] + t0 * dx), round(a[1] + t0 * dy)) if t0 > 0 else a
    pb = (round(a[0] + t1 * dx), round(a[1] + t1 * dy)) if t1 < 1 else b
    return (pb, pa) if swap else (pa, pb)


def clip_line(line: list[tuple[int, int]], lo_x: int, lo_y: int, hi_x: int, hi_y: int) -> list[list[tuple[int, int]]]:
    """Clip a polyline to a box; may return several pieces."""
    pieces: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    for a, b in zip(line, line[1:]):
        c = _clip_segment(a, b, lo_x, lo_y, hi_x, hi_y)
        if c is None:
            if len(cur) > 1:
                pieces.append(cur)
            cur = []
            continue
        ca, cb = c
        if cur and cur[-1] == ca:
            cur.append(cb)
        else:
            if len(cur) > 1:
                pieces.append(cur)
            cur = [ca, cb]
        if cb != b:  # left the box
            pieces.append(cur)
            cur = []
    if len(cur) > 1:
        pieces.append(cur)
    return [[p for i, p in enumerate(pc) if i == 0 or p != pc[i - 1]] for pc in pieces if len(set(pc)) > 1]


# --- graph ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Line:
    props: dict
    coords: list[tuple[int, int]]  # global grid


@dataclass(slots=True)
class Segment:
    id: str
    a: tuple[int, int]  # end nodes (global grid)
    b: tuple[int, int]
    coords: list[tuple[int, int]]
    length_m: float
    props: dict
    cum: list[float] = field(default_factory=list)  # metres from `a` at each vertex


@dataclass
class Graph:
    segments: dict[str, Segment] = field(default_factory=dict)
    adj: dict[tuple[int, int], list[str]] = field(default_factory=lambda: defaultdict(list))

    def other_end(self, seg: Segment, node: tuple[int, int]) -> tuple[int, int]:
        return seg.b if node == seg.a else seg.a


def _length(coords: list[tuple[int, int]]) -> tuple[float, list[float]]:
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        mpu = metres_per_unit(lonlat(x0, y0)[1])
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0) * mpu)
    return cum[-1], cum


def segment_id(props: dict, coords: list[tuple[int, int]]) -> str:
    c = coords if coords[0] <= coords[-1] else coords[::-1]
    key = f"{props.get('class')}|{props.get('subclass')}|{c[0]}|{c[-1]}|{c[len(c) // 2]}|{len(c)}"
    return hashlib.blake2b(key.encode(), digest_size=8).hexdigest()


class _SegIndex:
    """Grid index of line pieces for snapping line ends."""

    CELL = 64

    def __init__(self):
        self.cells: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)  # (line, vertex i)

    def add(self, li: int, vi: int, a, b):
        c = self.CELL
        for cx in range(min(a[0], b[0]) // c, max(a[0], b[0]) // c + 1):
            for cy in range(min(a[1], b[1]) // c, max(a[1], b[1]) // c + 1):
                self.cells[(cx, cy)].append((li, vi))

    def near(self, p):
        c = self.CELL
        seen = set()
        for cx in (p[0] // c - 1, p[0] // c, p[0] // c + 1):
            for cy in (p[1] // c - 1, p[1] // c, p[1] // c + 1):
                for item in self.cells.get((cx, cy), ()):
                    if item not in seen:
                        seen.add(item)
                        yield item


def build_graph(lines: list[Line]) -> Graph:
    lines = [Line(ln.props, list(ln.coords)) for ln in lines if len(ln.coords) > 1]
    # 1. snap line ends onto nearby lines (insert a vertex there)
    idx = _SegIndex()
    for li, ln in enumerate(lines):
        for vi in range(len(ln.coords) - 1):
            idx.add(li, vi, ln.coords[vi], ln.coords[vi + 1])
    vertex_count: dict[tuple[int, int], int] = defaultdict(int)
    for ln in lines:
        for v in set(ln.coords):
            vertex_count[v] += 1
    inserts: dict[int, list[tuple[int, float, tuple[int, int]]]] = defaultdict(list)
    for li, ln in enumerate(lines):
        for end in (ln.coords[0], ln.coords[-1]):
            if vertex_count[end] > 1:
                continue
            best = None
            for lj, vj in idx.near(end):
                if lj == li:
                    continue
                a, b = lines[lj].coords[vj], lines[lj].coords[vj + 1]
                dx, dy = b[0] - a[0], b[1] - a[1]
                ll = dx * dx + dy * dy
                t = 0.0 if ll == 0 else max(0.0, min(1.0, ((end[0] - a[0]) * dx + (end[1] - a[1]) * dy) / ll))
                d = math.hypot(a[0] + t * dx - end[0], a[1] + t * dy - end[1])
                if d <= SNAP_UNITS and (best is None or d < best[0]):
                    best = (d, lj, vj, t)
            if best is None:
                continue
            _d, lj, vj, t = best
            a, b = lines[lj].coords[vj], lines[lj].coords[vj + 1]
            if t <= 0.0 or t >= 1.0:  # nearest is an existing vertex: move the end onto it
                target = a if t <= 0.0 else b
            else:
                target = end  # put a vertex into the other line at this line's end
                inserts[lj].append((vj, t, end))
            vertex_count[target] += 1
            if target != end:
                i = 0 if lines[li].coords[0] == end else -1
                lines[li].coords[i] = target
    for lj, ins in inserts.items():
        coords = lines[lj].coords
        for vj, _t, p in sorted(ins, key=lambda x: (x[0], x[1]), reverse=True):
            coords.insert(vj + 1, p)
            vertex_count[p] += 1

    # 2. nodes: vertices used more than once (any line), plus line ends
    use: dict[tuple[int, int], int] = defaultdict(int)
    for ln in lines:
        for i, v in enumerate(ln.coords):
            use[v] += 2 if i in (0, len(ln.coords) - 1) else 1
    g = Graph()
    for ln in lines:
        cur = [ln.coords[0]]
        for v in ln.coords[1:]:
            if v == cur[-1]:
                continue
            cur.append(v)
            if use[v] >= 2:
                _add_segment(g, ln.props, cur)
                cur = [v]
    return g


def _add_segment(g: Graph, props: dict, coords: list[tuple[int, int]]) -> None:
    if len(coords) < 2:
        return
    sid = segment_id(props, coords)
    if sid in g.segments:
        return
    length, cum = _length(coords)
    seg = Segment(sid, coords[0], coords[-1], list(coords), length, props, cum)
    g.segments[sid] = seg
    g.adj[seg.a].append(sid)
    if seg.b != seg.a:
        g.adj[seg.b].append(sid)


# --- tiles -> lines ------------------------------------------------------------------------------


def tile_lines(tile: bytes, tx: int, ty: int) -> list[Line]:
    """Routable lines of one z14 tile, in global grid units, clipped to the tile's own square."""
    if tile[:2] == b"\x1f\x8b":
        tile = gzip.decompress(tile)
    layer = mvt.decode(tile, {"transportation"}).get("transportation")
    if layer is None:
        return []
    scale = EXTENT // layer.extent if layer.extent and layer.extent <= EXTENT else 1
    ox, oy = tx * EXTENT, ty * EXTENT
    out = []
    for f in layer.features:
        p = f.props
        if f.type != mvt.LINESTRING or p.get("class") not in ROUTABLE or p.get("subclass") in NOT_RIDDEN_SUBCLASS:
            continue
        props = {k: p[k] for k in ("class", "subclass", "surface", "brunnel", "oneway", "service") if k in p}
        for part in f.parts:
            glob = [(ox + x * scale, oy + y * scale) for x, y in part]
            for piece in clip_line(glob, ox, oy, ox + EXTENT, oy + EXTENT):
                out.append(Line(props, piece))
    return out


def _merge_borders(lines: list[Line]) -> None:
    """Neighbouring tiles clip the same road to nearly the same border point; make them identical."""
    on_border: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    for ln in lines:
        for p in (ln.coords[0], ln.coords[-1]):
            if p[0] % EXTENT == 0:
                on_border[("x", p[0])].append(p)
            if p[1] % EXTENT == 0:
                on_border[("y", p[1])].append(p)
    remap: dict[tuple[int, int], tuple[int, int]] = {}
    for (axis, _pos), pts in on_border.items():
        k = 1 if axis == "x" else 0
        pts = sorted(set(pts), key=lambda q: q[k])
        anchor = None
        for q in pts:
            if anchor is not None and q[k] - anchor[k] <= 2:
                remap[q] = anchor
            else:
                anchor = q
    if remap:
        for ln in lines:
            ln.coords[0] = remap.get(ln.coords[0], ln.coords[0])
            ln.coords[-1] = remap.get(ln.coords[-1], ln.coords[-1])


class RoadSource:
    """Reads road lines from the basemap PMTiles (local file), caching decoded tiles."""

    def __init__(self, pmtiles_path: Path):
        from pmtiles.reader import MmapSource, Reader

        self._file = open(pmtiles_path, "rb")  # noqa: SIM115 (kept open for the mmap source)
        self._reader = Reader(MmapSource(self._file))
        self._cache: dict[tuple[int, int], list[Line]] = {}

    def close(self) -> None:
        self._file.close()

    def lines(self, tx: int, ty: int) -> list[Line]:
        key = (tx, ty)
        if key not in self._cache:
            data = self._reader.get(ZOOM, tx, ty)
            self._cache[key] = tile_lines(data, tx, ty) if data else []
            if len(self._cache) > 256:
                self._cache.pop(next(iter(self._cache)))
        return self._cache[key]

    def graph(self, min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> Graph:
        lines = [Line(ln.props, list(ln.coords)) for t in tiles_for_bbox(min_lon, min_lat, max_lon, max_lat)
                 for ln in self.lines(*t)]
        _merge_borders(lines)
        return build_graph(lines)


# --- shortest paths ------------------------------------------------------------------------------


def dijkstra(g: Graph, sources: dict[tuple[int, int], float], limit_m: float) -> dict[tuple[int, int], float]:
    """Network distance from several start nodes (with start costs) to every node within limit_m."""
    dist = dict(sources)
    heap = [(d, n) for n, d in sources.items()]
    heapq.heapify(heap)
    while heap:
        d, n = heapq.heappop(heap)
        if d > dist.get(n, math.inf) or d > limit_m:
            continue
        for sid in g.adj.get(n, ()):
            seg = g.segments[sid]
            m = g.other_end(seg, n)
            nd = d + seg.length_m
            if nd < dist.get(m, math.inf) and nd <= limit_m:
                dist[m] = nd
                heapq.heappush(heap, (nd, m))
    return dist
