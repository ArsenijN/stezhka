import pytest
from synth import Builder

from stezhka.gpx import GpxError, parse_gpx, parse_time


def test_parses_opentracks_extensions():
    tr = parse_gpx(Builder().ride(10, 4.0).gpx())
    assert tr.creator == "OpenTracks"
    assert tr.name == "Test ride"
    assert tr.activity == "cycling"
    assert len(tr.points) == 10
    p = tr.points[0]
    assert p.speed == 4.0
    assert p.hacc == 5.0
    assert p.ele == 120.0
    assert p.segment == 0


def test_segments_are_numbered_globally():
    tr = parse_gpx(Builder().ride(5, 4).new_segment().ride(5, 4).gpx())
    assert [p.segment for p in tr.points] == [0] * 5 + [1] * 5


def test_gpx_10_inline_speed_and_points_without_time_are_skipped():
    data = b"""<?xml version="1.0"?>
    <gpx version="1.0" xmlns="http://www.topografix.com/GPX/1/0"><trk><trkseg>
      <trkpt lat="48.6" lon="22.3"><time>2026-01-01T10:00:00Z</time><speed>3.5</speed></trkpt>
      <trkpt lat="48.6001" lon="22.3"></trkpt>
      <trkpt lat="48.6002" lon="22.3"><time>2026-01-01T10:00:02+00:00</time></trkpt>
    </trkseg></trk></gpx>"""
    tr = parse_gpx(data)
    assert len(tr.points) == 2
    assert tr.points[0].speed == 3.5
    assert tr.points[1].t - tr.points[0].t == 2


@pytest.mark.parametrize(
    "data",
    [b"not xml", b"<kml></kml>", b'<gpx><trk><trkseg><trkpt lat="1" lon="1"><time>2026-01-01T00:00:00Z</time></trkpt></trkseg></trk></gpx>'],
)
def test_rejects_unusable_files(data):
    with pytest.raises(GpxError):
        parse_gpx(data)


def test_time_parsing():
    assert parse_time("2026-01-01T00:00:00Z") == parse_time("2026-01-01T02:00:00+02:00")
    assert parse_time("2026-01-01T00:00:00") == parse_time("2026-01-01T00:00:00Z")  # naive = UTC
    assert parse_time("2026-01-01T00:00:00.500Z") % 1 == 0.5
