"""Map matching: which road segments a ride used, and when.

Hidden Markov model after Newson & Krumm (2009): every GPS fix has a few candidate positions on
nearby segments (emission: how far the fix is from the road); consecutive candidates are linked by
the shortest route between them on the network (transition: how much that route differs from the
straight line between the fixes). Viterbi picks the most plausible sequence. This is what tells a
sidewalk (mapped as a footway) from the road next to it: the one that makes a consistent route.

Output: traversals - pieces of segments ridden, with entry/exit time and distance - from which
roads statistics are built (per-segment typical speed, explored roads).
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass

from .roads import Graph, Segment, grid, lonlat, metres_per_unit

MATCH_VERSION = 1
SIGMA_M = 10.0  # GPS noise (phones: ~5 m in the open, 15 m+ in a pocket or between buildings)
BETA_M = 10.0  # tolerance for route length vs straight-line difference
RADIUS_M = 35.0  # candidates within this distance of a fix
MAX_CANDIDATES = 8
STEP_M = 8.0  # use a fix only every ~8 m (1 Hz at walking pace adds nothing but time)
MAX_GAP_S = 60.0  # longer gap between fixes: start a new chain


@dataclass(slots=True)
class Candidate:
    seg: Segment
    offset: float  # metres from seg.a
    dist: float  # metres from the fix


@dataclass(slots=True)
class Traversal:
    segment_id: str
    enter_t: float
    exit_t: float
    from_off: float  # metres from seg.a where the ride entered / left it
    to_off: float
    dist_m: float


class _Index:
    CELL = 256  # grid units (~100 m)

    def __init__(self, g: Graph):
        self.cells: dict[tuple[int, int], list[tuple[Segment, int]]] = defaultdict(list)
        c = self.CELL
        for seg in g.segments.values():
            for i in range(len(seg.coords) - 1):
                (x0, y0), (x1, y1) = seg.coords[i], seg.coords[i + 1]
                for cx in range(min(x0, x1) // c, max(x0, x1) // c + 1):
                    for cy in range(min(y0, y1) // c, max(y0, y1) // c + 1):
                        self.cells[(cx, cy)].append((seg, i))

    def candidates(self, x: float, y: float, radius_units: float, mpu: float) -> list[Candidate]:
        c = self.CELL
        best: dict[str, Candidate] = {}
        r = int(radius_units // c) + 1
        for cx in range(int(x // c) - r, int(x // c) + r + 1):
            for cy in range(int(y // c) - r, int(y // c) + r + 1):
                for seg, i in self.cells.get((cx, cy), ()):
                    (x0, y0), (x1, y1) = seg.coords[i], seg.coords[i + 1]
                    dx, dy = x1 - x0, y1 - y0
                    ll = dx * dx + dy * dy
                    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / ll))
                    d = math.hypot(x0 + t * dx - x, y0 + t * dy - y)
                    if d > radius_units:
                        continue
                    dm = d * mpu
                    prev = best.get(seg.id)
                    if prev is None or dm < prev.dist:
                        off = seg.cum[i] + t * (seg.cum[i + 1] - seg.cum[i])
                        best[seg.id] = Candidate(seg, off, dm)
        return sorted(best.values(), key=lambda cd: cd.dist)[:MAX_CANDIDATES]


def _route(g: Graph, a: Candidate, limit_m: float):
    """Shortest network distances from candidate `a` to all nodes within limit_m, with predecessors."""
    starts = {a.seg.a: a.offset, a.seg.b: a.seg.length_m - a.offset}
    dist: dict = {}
    prev: dict = {}
    heap = []
    for n, d in starts.items():
        if d < dist.get(n, math.inf):
            dist[n] = d
            prev[n] = None
            heapq.heappush(heap, (d, n))
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
                prev[m] = (n, sid)
                heapq.heappush(heap, (nd, m))
    return dist, prev


def _distance(a: Candidate, b: Candidate, dist: dict) -> tuple[float, str | None]:
    """Route length from a to b, and through which end of b's segment ('a', 'b' or None = along it)."""
    best, via = math.inf, None
    if a.seg.id == b.seg.id:
        best, via = abs(b.offset - a.offset), None
    da = dist.get(b.seg.a, math.inf) + b.offset
    db = dist.get(b.seg.b, math.inf) + (b.seg.length_m - b.offset)
    if da < best:
        best, via = da, "a"
    if db < best:
        best, via = db, "b"
    return best, via


def _downsample(pts) -> list[int]:
    """Indices of fixes to match: one every STEP_M, always the first/last of each recording segment."""
    keep = []
    last = None
    n = len(pts.t)
    for i in range(n):
        boundary = i == 0 or i == n - 1 or pts.segment[i] != pts.segment[i - 1] or (i + 1 < n and pts.segment[i + 1] != pts.segment[i])
        if boundary or last is None or pts.dist[i] - pts.dist[last] >= STEP_M:
            keep.append(i)
            last = i
    return keep


@dataclass
class Points:
    """Columnar ride points (the subset matching needs)."""

    t: list[float]
    lat: list[float]
    lon: list[float]
    dist: list[float]
    segment: list[int]


def match(g: Graph, pts: Points) -> list[Traversal]:
    """Match a ride to the graph. Returns traversals in ride order (may skip unmatched stretches)."""
    if not pts.t or not g.segments:
        return []
    index = _Index(g)
    keep = _downsample(pts)
    cands: list[list[Candidate]] = []
    for i in keep:
        x, y = grid(pts.lon[i], pts.lat[i])
        mpu = metres_per_unit(pts.lat[i])
        cands.append(index.candidates(x, y, RADIUS_M / mpu, mpu))

    traversals: list[Traversal] = []
    chain: list[int] = []  # positions in `keep` of the current chain
    for k in range(len(keep)):
        if not cands[k]:
            traversals += _solve(g, pts, keep, cands, chain)
            chain = []
            continue
        if chain:
            i0, i1 = keep[chain[-1]], keep[k]
            if pts.segment[i0] != pts.segment[i1] or pts.t[i1] - pts.t[i0] > MAX_GAP_S:
                traversals += _solve(g, pts, keep, cands, chain)
                chain = []
        chain.append(k)
    traversals += _solve(g, pts, keep, cands, chain)
    return _merge(traversals)


def _solve(g: Graph, pts: Points, keep: list[int], cands: list[list[Candidate]], chain: list[int]) -> list[Traversal]:
    """Viterbi over one chain; breaks the chain where no candidate pair is connected."""
    if not chain:
        return []
    out: list[Traversal] = []
    start = 0
    while start < len(chain):
        end, path = _viterbi(g, pts, keep, cands, chain[start:])
        out += _traverse(g, pts, keep, chain[start:start + end], path)
        start += end
    return out


def _viterbi(g, pts, keep, cands, chain):
    """Returns (how many chain steps were matched, chosen candidate per step + routes)."""
    first = cands[chain[0]]
    score = [-0.5 * (c.dist / SIGMA_M) ** 2 for c in first]
    back: list[list[tuple[int, str | None]]] = []
    routes_cache: list[dict] = []
    steps = 1
    for s in range(1, len(chain)):
        ka, kb = chain[s - 1], chain[s]
        ia, ib = keep[ka], keep[kb]
        gc = _gc(pts, ia, ib)
        limit = gc * 2.5 + 60.0
        prev_c, cur_c = cands[ka], cands[kb]
        new = [-math.inf] * len(cur_c)
        bk: list[tuple[int, str | None]] = [(-1, None)] * len(cur_c)
        routes = {}
        for ai, a in enumerate(prev_c):
            if score[ai] == -math.inf:
                continue
            dist, prev = _route(g, a, limit)
            routes[ai] = prev
            for bi, b in enumerate(cur_c):
                r, via = _distance(a, b, dist)
                if r == math.inf or r > limit:
                    continue
                sc = score[ai] - abs(r - gc) / BETA_M - 0.5 * (b.dist / SIGMA_M) ** 2
                if sc > new[bi]:
                    new[bi] = sc
                    bk[bi] = (ai, via)
        if max(new) == -math.inf:
            break  # nothing connects: end this piece here, the next one starts at step s
        score = new
        back.append(bk)
        routes_cache.append(routes)
        steps += 1
    # backtrack
    bi = max(range(len(score)), key=lambda i: score[i])
    chosen = [bi]
    vias = []
    for s in range(len(back) - 1, -1, -1):
        ai, via = back[s][chosen[-1]]
        vias.append(via)
        chosen.append(ai)
    chosen.reverse()
    vias.reverse()
    picks = []
    for s in range(steps):
        c = cands[chain[s]][chosen[s]]
        if s == 0:
            picks.append((c, None))
        else:
            prev_tree = routes_cache[s - 1][chosen[s - 1]]
            picks.append((c, (vias[s - 1], prev_tree)))
    return steps, picks


def _gc(pts, i, j) -> float:
    lat = math.radians((pts.lat[i] + pts.lat[j]) / 2)
    dx = math.radians(pts.lon[j] - pts.lon[i]) * math.cos(lat) * 6_371_000
    dy = math.radians(pts.lat[j] - pts.lat[i]) * 6_371_000
    return math.hypot(dx, dy)


def _traverse(g: Graph, pts: Points, keep, chain, picks) -> list[Traversal]:
    """Turn chosen candidates + routes into per-segment pieces with times (by distance share)."""
    out: list[Traversal] = []
    for s in range(1, len(picks)):
        a, _ = picks[s - 1]
        b, (via, prev) = picks[s]
        ta, tb = pts.t[keep[chain[s - 1]]], pts.t[keep[chain[s]]]
        pieces: list[tuple[str, float, float, float]] = []  # (segment id, from_off, to_off, length)
        if via is None:  # along the same segment
            pieces.append((a.seg.id, a.offset, b.offset, abs(b.offset - a.offset)))
        else:
            node = b.seg.a if via == "a" else b.seg.b
            mid: list[tuple[str, tuple]] = []
            n = node
            while prev.get(n) is not None:
                pn, sid = prev[n]
                mid.append((sid, pn))
                n = pn
            mid.reverse()
            # leave a's segment through node n (one of its ends)
            exit_off = 0.0 if n == a.seg.a else a.seg.length_m
            pieces.append((a.seg.id, a.offset, exit_off, abs(exit_off - a.offset)))
            for sid, from_node in mid:
                seg = g.segments[sid]
                fwd = from_node == seg.a
                pieces.append((sid, 0.0 if fwd else seg.length_m, seg.length_m if fwd else 0.0, seg.length_m))
            enter_off = 0.0 if via == "a" else b.seg.length_m
            pieces.append((b.seg.id, enter_off, b.offset, abs(b.offset - enter_off)))
        total = sum(p[3] for p in pieces) or 1.0
        t = ta
        for sid, f, to, ln in pieces:
            dt = (tb - ta) * ln / total
            out.append(Traversal(sid, t, t + dt, f, to, ln))
            t += dt
    return out


def _merge(trs: list[Traversal]) -> list[Traversal]:
    """Join consecutive pieces on the same segment into one traversal."""
    out: list[Traversal] = []
    for tr in trs:
        if out and out[-1].segment_id == tr.segment_id and abs(out[-1].exit_t - tr.enter_t) < 1e-6:
            last = out[-1]
            last.exit_t = tr.exit_t
            last.to_off = tr.to_off
            last.dist_m += tr.dist_m
        elif tr.dist_m > 0 or tr.exit_t > tr.enter_t:
            out.append(tr)
    return out


def segment_geometry(seg: Segment) -> list[tuple[float, float]]:
    return [lonlat(x, y) for x, y in seg.coords]
