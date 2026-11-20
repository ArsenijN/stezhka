"""Stop reasons (OSM nodes, basemap, junctions) and sharp slowdowns, all offline."""

from synth import Builder

from stezhka import osm, reasons
from stezhka.db import connect
from stezhka.ingest import import_bytes
from stezhka.process import process
from stezhka.gpx import parse_gpx as parse


def _brake_and_ride_on():
    """20 km/h, brake to ~6 km/h in 3 s (1.3 m/s2), ride on: a sharp slowdown, no stop."""
    b = Builder().ride(30, 5.5)
    for v in (4.5, 3.4, 2.4, 1.7):
        b.ride(1, v)
    return b.ride(10, 1.7).ride(30, 5.0)


def test_sharp_slowdown_while_riding_on_is_found():
    pr = process(parse(_brake_and_ride_on().gpx()))
    slow = [s for s in pr.stops if s.kind == "slowdown"]
    assert len(slow) == 1 and 28 <= slow[0].start_t - pr.points[0].t <= 32


def test_braking_into_a_stop_is_the_stop_not_a_slowdown():
    b = Builder().ride(30, 5.5)
    for v in (4.0, 2.5, 1.0):
        b.ride(1, v)
    pr = process(parse(b.stand(30).ride(30, 5.0).gpx()))
    assert [s.kind for s in pr.stops] == ["stop"]


def test_no_slowdowns_without_the_phones_own_speed():
    pr = process(parse(Builder().ride(30, 5.5).ride(3, 2.0, device_speed=False).ride(30, 5.0, device_speed=False).gpx()))
    assert pr.report.speed_source == "derived" and not [s for s in pr.stops if s.kind == "slowdown"]


def test_reason_priority_and_radius():
    lat, lon = 48.6, 22.3
    north = lambda m: lat + m / 111_320  # noqa: E731
    # a junction at 5 m loses to a traffic signal at 25 m (signals sit at the junction centre)
    assert reasons.explain(lat, lon, [("junction", north(5), lon), ("traffic_signals", north(25), lon)])[0] == "traffic_signals"
    # a crossing beyond its 15 m radius does not explain; the junction does
    assert reasons.explain(lat, lon, [("crossing", north(20), lon), ("junction", north(8), lon)])[0] == "junction"
    assert reasons.explain(lat, lon, [("bus_stop", north(40), lon)]) is None


def test_osm_kinds():
    assert osm.kind_of({"highway": "traffic_signals"}) == "traffic_signals"
    assert osm.kind_of({"highway": "crossing", "crossing": "traffic_signals"}) == "traffic_signals"
    assert osm.kind_of({"highway": "crossing", "crossing": "uncontrolled"}) == "crossing"
    assert osm.kind_of({"railway": "level_crossing"}) == "level_crossing"
    assert osm.kind_of({"highway": "give_way"}) == "stop_sign"
    assert osm.kind_of({"highway": "street_lamp"}) is None
    assert osm.cell_key(48.62, 22.29) == "486:222" and osm.cell_bbox("486:222") == (48.6, 22.2, 48.7, 22.3)


def test_osm_refresh_and_stop_reasons(tmp_path):
    conn = connect(tmp_path / "db.sqlite3")
    b = Builder().ride(30, 5.0).stand(30).ride(30, 5.0)
    rid = import_bytes(conn, tmp_path / "raw", b.gpx(), "r.gpx").ride_id
    stop = conn.execute("SELECT lat, lon FROM stops WHERE ride_id = ? AND kind = 'stop'", (rid,)).fetchone()
    calls = []

    def fake_fetch(key):
        calls.append(key)
        return [(1, "traffic_signals", stop["lat"] + 15 / 111_320, stop["lon"])]

    assert osm.refresh(conn, fake_fetch, now=1000.0) == len(calls) >= 1
    assert osm.refresh(conn, fake_fetch, now=2000.0) == 0  # fresh: not fetched again
    assert osm.refresh(conn, fake_fetch, now=1000.0 + osm.REFRESH_S + 1) >= 1  # stale: refetched

    def broken(key):
        raise OSError("down")

    assert osm.refresh(conn, broken, now=1000.0 + 3 * osm.REFRESH_S) == 0
    assert conn.execute("SELECT COUNT(*) FROM osm_nodes").fetchone()[0] == 1  # old data kept

    reasons.explain_pending(conn, None)
    row = conn.execute("SELECT reason, reason_m FROM stops WHERE ride_id = ? AND kind = 'stop'", (rid,)).fetchone()
    assert row["reason"] == "traffic_signals" and 10 < row["reason_m"] < 20
    assert conn.execute("SELECT reason_version FROM rides WHERE id = ?", (rid,)).fetchone()[0] == reasons.REASON_VERSION
