"""Road network from vector tiles, map matching, and per-segment statistics (synthetic data)."""

import math
import random

from stezhka import matching, mvt, roads, segments
from stezhka.db import connect
from stezhka.matching import Points, match
from stezhka.roads import EXTENT, Line, build_graph, clip_line, grid, lonlat, metres_per_unit

LAT0, LON0 = 50.45, 30.52  # Kyiv: synthetic streets only


# --- a tiny MVT encoder, enough to test the decoder ---------------------------------------------


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(num, wire, payload):
    key = _varint((num << 3) | wire)
    if wire == 0:
        return key + _varint(payload)
    return key + _varint(len(payload)) + payload


def _zz(n):
    return (n << 1) ^ (n >> 63)


def _encode_line(coords):
    cmds = [(1 & 7) | (1 << 3), _zz(coords[0][0]), _zz(coords[0][1]), (2 & 7) | ((len(coords) - 1) << 3)]
    x, y = coords[0]
    for cx, cy in coords[1:]:
        cmds += [_zz(cx - x), _zz(cy - y)]
        x, y = cx, cy
    return b"".join(_varint(c) for c in cmds)


def _tile(name, features):
    keys, values, feats = [], [], b""
    for props, coords in features:
        tags = []
        for k, v in props.items():
            if k not in keys:
                keys.append(k)
            if v not in values:
                values.append(v)
            tags += [keys.index(k), values.index(v)]
        f = _field(2, 2, b"".join(_varint(t) for t in tags)) + _field(3, 0, 2) + _field(4, 2, _encode_line(coords))
        feats += _field(2, 2, f)
    layer = _field(15, 0, 2) + _field(1, 2, name.encode()) + feats
    layer += b"".join(_field(3, 2, k.encode()) for k in keys)
    layer += b"".join(_field(4, 2, _field(1, 2, v.encode())) for v in values)
    layer += _field(5, 0, EXTENT)
    return _field(3, 2, layer)


def test_mvt_decoder_and_tile_lines():
    data = _tile("transportation", [
        ({"class": "minor", "surface": "paved"}, [(-50, 100), (2000, 100), (5000, 100)]),
        ({"class": "rail"}, [(0, 500), (4000, 500)]),
        ({"class": "path", "subclass": "steps"}, [(0, 900), (100, 900)]),
    ])
    layer = mvt.decode(data)["transportation"]
    assert layer.extent == EXTENT and len(layer.features) == 3
    assert layer.features[0].props == {"class": "minor", "surface": "paved"}
    assert layer.features[0].parts == [[(-50, 100), (2000, 100), (5000, 100)]]
    lines = roads.tile_lines(data, 3, 5)  # rail and steps are not ridden; the street is clipped to the tile
    assert len(lines) == 1
    ox, oy = 3 * EXTENT, 5 * EXTENT
    assert lines[0].coords == [(ox, oy + 100), (ox + 2000, oy + 100), (ox + EXTENT, oy + 100)]


def test_clip_line_splits_at_the_box():
    pieces = clip_line([(-10, 5), (5, 5), (20, 5), (20, 20), (5, 30)], 0, 0, 10, 10)
    assert pieces == [[(0, 5), (5, 5), (10, 5)]]


# --- synthetic street grids ---------------------------------------------------------------------


def _g(east_m, north_m):
    """Grid coordinates of a point east/north of the origin (metres)."""
    x0, y0 = grid(LON0, LAT0)
    mpu = metres_per_unit(LAT0)
    return (round(x0 + east_m / mpu), round(y0 - north_m / mpu))


def _street(cls, *pts_m, sub=None):
    props = {"class": cls} | ({"subclass": sub} if sub else {})
    return Line(props, [_g(e, n) for e, n in pts_m])


def test_tee_junction_without_shared_vertex_is_snapped():
    main = _street("secondary", (0, 0), (400, 0))  # no vertex where the side street meets it
    side = _street("minor", (200, 0.5), (200, 300))  # ends 0.5 m off the main street's line
    g = build_graph([main, side])
    assert len(g.segments) == 3  # main split in two at the junction + the side street
    junction = max(g.adj, key=lambda n: len(g.adj[n]))
    assert len(g.adj[junction]) == 3


def _ride(path_m, noise_m=4.0, step_m=4.5, seed=1):
    """Points along a polyline (metres), one per second, with GPS noise."""
    rnd = random.Random(seed)
    pts = Points([], [], [], [], [])
    mpu = metres_per_unit(LAT0)
    d_total = 0.0
    t = 1_000_000.0
    for (e0, n0), (e1, n1) in zip(path_m, path_m[1:]):
        seg = math.hypot(e1 - e0, n1 - n0)
        k = max(1, int(seg / step_m))
        for i in range(k):
            f = i / k
            e = e0 + (e1 - e0) * f + rnd.gauss(0, noise_m)
            n = n0 + (n1 - n0) * f + rnd.gauss(0, noise_m)
            x, y = _g(e, n)
            lon, lat = lonlat(x, y)
            pts.t.append(t)
            pts.lat.append(lat)
            pts.lon.append(lon)
            pts.dist.append(d_total + seg * f)
            pts.segment.append(0)
            t += 1.0
        d_total += seg
    del mpu
    return pts


def test_matching_follows_the_ridden_street_not_the_parallel_one():
    # two parallel streets 40 m apart joined at both ends; the rider takes the northern one
    lines = [
        _street("tertiary", (0, 0), (600, 0)),
        _street("minor", (0, 40), (600, 40)),
        _street("minor", (0, 0), (0, 40)),
        _street("minor", (600, 0), (600, 40)),
    ]
    g = build_graph(lines)
    trs = match(g, _ride([(0, 0), (0, 40), (600, 40), (600, 0)]))
    by_class = {}
    for tr in trs:
        cls = g.segments[tr.segment_id].props["class"]
        by_class[cls] = by_class.get(cls, 0) + tr.dist_m
    assert by_class.get("minor", 0) > 600 and by_class.get("tertiary", 0) < 30
    assert all(a.exit_t <= b.enter_t + 1e-6 for a, b in zip(trs, trs[1:]))  # in time order
    assert abs(sum(tr.dist_m for tr in trs) - 680) < 40


def test_matching_breaks_where_there_is_no_road():
    g = build_graph([_street("minor", (0, 0), (300, 0))])
    pts = _ride([(0, 0), (300, 0), (300, 400)], noise_m=2)  # the last 400 m are off the map
    trs = match(g, pts)
    assert 250 < sum(tr.dist_m for tr in trs) < 320


# --- storing matches and statistics --------------------------------------------------------------


class _Source:
    def __init__(self, g):
        self.g = g

    def graph(self, *_bbox):
        return self.g


class _NoNames:
    def lookup(self, _seg):
        return "Test street", None


def test_match_ride_stores_passes_and_stats(tmp_path):
    from stezhka.ingest import import_bytes
    from synth import commute

    conn = connect(tmp_path / "db.sqlite3")
    rid = import_bytes(conn, tmp_path / "raw", commute().gpx(), "a.gpx").ride_id
    pts = segments._points(conn, rid)
    # a street right under the synthetic ride (it runs east from Uzhhorod's synth origin)
    x0, y0 = grid(pts.lon[0], pts.lat[0])
    x1, y1 = grid(pts.lon[-1], pts.lat[-1])
    mid = (int((x0 + x1) // 2), int((y0 + y1) // 2))
    g = build_graph([Line({"class": "minor"}, [(int(x0) - 200, int(y0)), mid, (int(x1) + 200, int(y1))])])
    n = segments.match_ride(conn, _Source(g), _NoNames(), rid)
    assert n >= 1
    stats = segments.road_stats(conn)
    assert stats and stats[0]["name"] == "Test street" and stats[0]["rides"] == 1
    assert conn.execute("SELECT match_version FROM rides WHERE id = ?", (rid,)).fetchone()[0] == matching.MATCH_VERSION
    # a trashed ride no longer counts
    conn.execute("UPDATE rides SET trashed_at = 1 WHERE id = ?", (rid,))
    assert segments.road_stats(conn) == []
