"""Turn raw GPX points into a clean, monotonic series with a usable speed per point.

Principles:
* never invent positions, only drop clearly broken ones;
* prefer the device's Doppler speed (much less noisy than position differences) when present;
* everything here is derived data: the raw file is kept untouched and can be reprocessed.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from .geo import haversine_m
from .gpx import RawPoint


@dataclass(frozen=True, slots=True)
class CleanParams:
    max_hacc_m: float = 30.0  # points reported less accurate than this are dropped
    max_speed_mps: float = 25.0  # ~90 km/h; faster jumps between fixes are GPS glitches
    # Plausibility gate (step 3b): a bike cannot speed up from 15 to 60 km/h in a second. After a
    # GPS dropout the first fixes can be tens of metres off for several seconds (seen: "60.8 km/h"
    # across a roundabout); a single-spike rule at 90 km/h does not catch a run of them.
    max_accel_mps2: float = 2.5  # hard sprint on a bike
    accel_horizon_s: float = 3.0  # after a gap you are still riding at about the same speed
    # GPS error drifts slowly: consecutive fixes rarely differ by more than a few metres of error,
    # more the longer apart they are
    noise_m: float = 5.0
    noise_per_s: float = 1.5
    noise_max_m: float = 15.0
    gap_factor: float = 3.0  # a gap = more than this x the recording's usual interval (and > 3 s)
    settle_points: int = 3  # after a gap, fixes count once this many move consistently again
    reanchor_points: int = 3  # this many mutually consistent fixes override the last good one
    device_speed_min_share: float = 0.8  # use device speed if this share of points has it
    derived_speed_half_window_s: float = 5.0  # chord over +-5 s; robust to jitter at stops
    speed_median_window: int = 5  # odd number of points
    ele_half_window_s: float = 10.0


@dataclass(slots=True)
class Point:
    t: float
    lat: float
    lon: float
    ele: float | None
    hacc: float | None
    device_speed: float | None
    segment: int
    speed: float = 0.0  # final speed estimate, m/s
    ele_smooth: float | None = None
    dist: float = 0.0  # cumulative ridden distance, m (filled by process)
    moving: bool = True  # filled by stop detection


@dataclass(slots=True)
class CleanReport:
    input_points: int = 0
    dropped_time: int = 0
    dropped_accuracy: int = 0
    dropped_spikes: int = 0
    speed_source: str = "derived"
    notes: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return self.dropped_time + self.dropped_accuracy + self.dropped_spikes


def _seg_speed(a: Point, b: Point) -> float:
    dt = b.t - a.t
    return haversine_m(a.lat, a.lon, b.lat, b.lon) / dt if dt > 0 else float("inf")


def _drop_spikes(pts: list[Point], max_v: float) -> tuple[list[Point], int]:
    """Drop isolated points that are unreachable from both neighbours but whose neighbours are
    reachable from each other (the classic single-fix GPS jump). Repeats a few passes."""
    dropped = 0
    for _ in range(5):
        n = len(pts)
        if n < 3:
            break
        keep = [True] * n
        for i in range(n):
            prev_ok = i > 0 and pts[i - 1].segment == pts[i].segment
            next_ok = i < n - 1 and pts[i + 1].segment == pts[i].segment
            v_in = _seg_speed(pts[i - 1], pts[i]) if prev_ok else None
            v_out = _seg_speed(pts[i], pts[i + 1]) if next_ok else None
            if v_in is not None and v_out is not None:
                if v_in > max_v and v_out > max_v and _seg_speed(pts[i - 1], pts[i + 1]) <= max_v:
                    keep[i] = False
            elif v_out is not None and v_out > max_v and i + 2 < n and pts[i + 2].segment == pts[i].segment:
                if _seg_speed(pts[i + 1], pts[i + 2]) <= max_v:  # bad first fix of a segment
                    keep[i] = False
            elif v_in is not None and v_in > max_v and i >= 2 and pts[i - 2].segment == pts[i].segment:
                if _seg_speed(pts[i - 2], pts[i - 1]) <= max_v:  # bad last fix of a segment
                    keep[i] = False
        removed = keep.count(False)
        if not removed:
            break
        dropped += removed
        pts = [p for p, k in zip(pts, keep) if k]
    return pts, dropped


def _ref_speed(acc: list[Point], horizon_s: float = 5.0) -> float:
    """Speed of the accepted track over its last few seconds."""
    if len(acc) < 2:
        return 0.0
    last = acc[-1]
    j = len(acc) - 2
    while j > 0 and last.t - acc[j].t < horizon_s and acc[j - 1].segment == last.segment:
        j -= 1
    a = acc[j]
    return haversine_m(a.lat, a.lon, last.lat, last.lon) / (last.t - a.t) if last.t > a.t else 0.0


def _reachable(a: Point, b: Point, v_ref: float, p: "CleanParams") -> bool:
    dt = b.t - a.t
    v = min(p.max_speed_mps, v_ref + p.max_accel_mps2 * min(dt, p.accel_horizon_s))
    noise = min(p.noise_m + p.noise_per_s * dt, p.noise_max_m)
    return haversine_m(a.lat, a.lon, b.lat, b.lon) <= v * dt + noise


def _settle_after_gaps(pts: list[Point], p: "CleanParams") -> tuple[list[Point], int]:
    """After a gap in the recording (GPS dropout), a phone's first fixes are often tens of metres
    off while it re-acquires. Drop fixes after a gap until `settle_points` consecutive fixes move
    consistently with the speed before the gap."""
    if len(pts) < 3:
        return pts, 0
    steps = sorted(b.t - a.t for a, b in zip(pts, pts[1:]) if a.segment == b.segment)
    usual = steps[len(steps) // 2] if steps else 1.0
    gap_s = max(3.0, p.gap_factor * usual)
    out: list[Point] = [pts[0]]
    dropped = 0
    i = 1
    while i < len(pts):
        pt = pts[i]
        if pt.segment != out[-1].segment or pt.t - out[-1].t <= gap_s:
            out.append(pt)
            i += 1
            continue
        v_ref = _ref_speed(out)
        j = i  # find the first fix from which settle_points fixes move consistently
        while j + p.settle_points - 1 < len(pts):
            run = pts[j:j + p.settle_points]
            if all(a.segment == run[0].segment for a in run) and all(
                _reachable(a, b, v_ref, p) for a, b in zip(run, run[1:])
            ):
                break
            j += 1
        else:
            j = i  # never settles: keep the rest as recorded
        dropped += j - i
        out.append(pts[j])
        i = j + 1
    return out, dropped


def _drop_implausible(pts: list[Point], p: "CleanParams") -> tuple[list[Point], int]:
    """Forward gate: keep a fix only if it can be reached from the last kept one at a speed close to
    the recent riding speed (+ acceleration + GPS noise). When `reanchor_points` rejected fixes in a
    row agree with each other, the gate re-anchors on them and drops the kept fixes since the last
    one they agree with (the bad fixes it had accepted)."""
    acc: list[Point] = []
    pending: list[Point] = []
    dropped = 0
    for pt in pts:
        if not acc or acc[-1].segment != pt.segment:
            dropped += len(pending)  # a run of rejected fixes at the end of a segment
            pending = []
            acc.append(pt)
            continue
        if _reachable(acc[-1], pt, _ref_speed(acc), p) and not pending:
            acc.append(pt)
            continue
        # rejected (or already collecting a run): does it continue the run of rejected fixes?
        if pending and not _reachable(pending[-1], pt, _ref_speed(pending), p):
            dropped += len(pending)
            pending = []
        pending.append(pt)
        if pending and _reachable(acc[-1], pending[0], _ref_speed(acc), p):
            acc.extend(pending)  # the run fits after all (e.g. after a long gap)
            pending = []
        elif len(pending) >= p.reanchor_points:
            # the run is consistent: trust it; drop kept fixes it cannot be reached from
            back = 0
            while len(acc) > 1 and back < 10 and not _reachable(acc[-1], pending[0], _ref_speed(acc[:-1]), p) \
                    and acc[-2].segment == pending[0].segment:
                acc.pop()
                back += 1
            dropped += back
            acc.extend(pending)
            pending = []
    dropped += len(pending)
    return acc, dropped


def _segments(pts: list[Point]) -> list[tuple[int, int]]:
    """[start, end) index ranges of runs with the same segment id."""
    out = []
    start = 0
    for i in range(1, len(pts) + 1):
        if i == len(pts) or pts[i].segment != pts[start].segment:
            out.append((start, i))
            start = i
    return out


def _derived_speed(pts: list[Point], lo: int, hi: int, half_w: float) -> list[float]:
    ts = [p.t for p in pts[lo:hi]]
    out = []
    for k in range(hi - lo):
        a = bisect_left(ts, ts[k] - half_w)
        b = bisect_right(ts, ts[k] + half_w) - 1
        if b <= a:  # isolated point: fall back to nearest neighbour interval
            a, b = max(0, k - 1), min(len(ts) - 1, k + 1)
        if b <= a:
            out.append(0.0)
            continue
        pa, pb = pts[lo + a], pts[lo + b]
        out.append(haversine_m(pa.lat, pa.lon, pb.lat, pb.lon) / (pb.t - pa.t))
    return out


def _median_filter(values: list[float], window: int) -> list[float]:
    if window < 3 or len(values) < 3:
        return list(values)
    h = window // 2
    n = len(values)
    return [statistics.median(values[max(0, i - h) : min(n, i + h + 1)]) for i in range(n)]


def _smooth_ele(pts: list[Point], lo: int, hi: int, half_w: float) -> None:
    """Centred moving average. The window shrinks symmetrically near segment ends; a one-sided
    window there would bias the first/last values towards the middle and eat climb metres."""
    ts = [p.t for p in pts[lo:hi]]
    for k in range(hi - lo):
        h = min(half_w, ts[k] - ts[0], ts[-1] - ts[k])
        a = bisect_left(ts, ts[k] - h)
        b = bisect_right(ts, ts[k] + h)
        vals = [pts[lo + j].ele for j in range(a, b) if pts[lo + j].ele is not None]
        pts[lo + k].ele_smooth = sum(vals) / len(vals) if vals else None


def clean(raw: list[RawPoint], params: CleanParams = CleanParams()) -> tuple[list[Point], CleanReport]:
    rep = CleanReport(input_points=len(raw))

    # 1. Monotonic time. Stable sort keeps recorder order for equal timestamps; keep the first.
    pts: list[Point] = []
    for r in sorted(raw, key=lambda r: r.t):
        if pts and r.t <= pts[-1].t:
            rep.dropped_time += 1
            continue
        pts.append(Point(r.t, r.lat, r.lon, r.ele, r.hacc, r.speed, r.segment))

    # 2. Accuracy filter (only where the recorder reports accuracy).
    kept = [p for p in pts if p.hacc is None or p.hacc <= params.max_hacc_m]
    if len(kept) >= 2:
        rep.dropped_accuracy = len(pts) - len(kept)
        pts = kept
    else:
        rep.notes.append("accuracy_filter_skipped_all_points_inaccurate")

    # 3. Isolated position spikes, then runs of implausible fixes (e.g. after a GPS dropout).
    pts, rep.dropped_spikes = _drop_spikes(pts, params.max_speed_mps)
    pts, unsettled = _settle_after_gaps(pts, params)
    pts, implausible = _drop_implausible(pts, params)
    rep.dropped_spikes += unsettled + implausible

    # 4. Speed per point, computed within each recorder segment.
    with_dev = sum(1 for p in pts if p.device_speed is not None)
    use_device = bool(pts) and with_dev / len(pts) >= params.device_speed_min_share
    rep.speed_source = "device" if use_device else "derived"
    for lo, hi in _segments(pts):
        derived = _derived_speed(pts, lo, hi, params.derived_speed_half_window_s)
        raw_speed = [
            pts[lo + k].device_speed if use_device and pts[lo + k].device_speed is not None else derived[k]
            for k in range(hi - lo)
        ]
        for k, v in enumerate(_median_filter(raw_speed, params.speed_median_window)):
            pts[lo + k].speed = min(v, params.max_speed_mps)
        _smooth_ele(pts, lo, hi, params.ele_half_window_s)

    return pts, rep
