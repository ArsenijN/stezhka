"""GPX 1.0/1.1 parsing.

Parsing is namespace-agnostic on purpose: recorders (OpenTracks, Strava, Komoot, Garmin...)
put speed and accuracy into different extension namespaces, and some files use GPX 1.0.
We only look at local element names.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

# Extension element local names (lower-cased) that carry device speed in m/s.
_SPEED_NAMES = {"speed"}
# ... and horizontal accuracy in metres.
_HACC_NAMES = {"accuracy_horizontal", "horizontal_accuracy", "hacc", "accuracy"}


class GpxError(ValueError):
    """The file is not a usable GPX track."""


@dataclass(frozen=True, slots=True)
class RawPoint:
    t: float  # unix seconds, UTC
    lat: float
    lon: float
    ele: float | None
    speed: float | None  # device-reported (Doppler) speed, m/s
    hacc: float | None  # horizontal accuracy, m
    segment: int  # global segment index; a new segment = recorder pause


@dataclass(frozen=True, slots=True)
class ParsedTrack:
    name: str | None
    activity: str | None
    creator: str | None
    points: list[RawPoint]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    s = el.text.strip()
    return s or None


def _float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None  # reject NaN/inf


def parse_time(s: str) -> float:
    """ISO 8601 timestamp -> unix seconds. Naive timestamps are treated as UTC (GPX spec)."""
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _extensions(pt: ET.Element) -> tuple[float | None, float | None]:
    speed = hacc = None
    ext = _child(pt, "extensions")
    if ext is None:
        return None, None
    for el in ext.iter():
        name = _local(el.tag)
        if speed is None and name in _SPEED_NAMES:
            speed = _float(_text(el))
        elif hacc is None and name in _HACC_NAMES:
            hacc = _float(_text(el))
    return speed, hacc


def parse_gpx(data: bytes) -> ParsedTrack:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise GpxError(f"invalid XML: {e}") from e
    if _local(root.tag) != "gpx":
        raise GpxError("root element is not <gpx>")

    creator = root.get("creator")
    points: list[RawPoint] = []
    name = activity = None
    segment = -1

    for trk in _children(root, "trk"):
        name = name or _text(_child(trk, "name"))
        activity = activity or _text(_child(trk, "type"))
        for seg in _children(trk, "trkseg"):
            segment += 1
            for pt in _children(seg, "trkpt"):
                lat = _float(pt.get("lat"))
                lon = _float(pt.get("lon"))
                ts = _text(_child(pt, "time"))
                if lat is None or lon is None or ts is None:
                    continue  # without time we cannot derive speed; skip
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                try:
                    t = parse_time(ts)
                except ValueError:
                    continue
                speed, hacc = _extensions(pt)
                # GPX 1.0 puts <speed> directly on the point.
                if speed is None:
                    speed = _float(_text(_child(pt, "speed")))
                points.append(
                    RawPoint(
                        t=t,
                        lat=lat,
                        lon=lon,
                        ele=_float(_text(_child(pt, "ele"))),
                        speed=speed if speed is not None and speed >= 0 else None,
                        hacc=hacc if hacc is not None and hacc >= 0 else None,
                        segment=segment,
                    )
                )

    if len(points) < 2:
        raise GpxError("track has fewer than 2 timestamped points")
    return ParsedTrack(name=name, activity=activity, creator=creator, points=points)
