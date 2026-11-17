import pytest
from synth import Builder, commute

from stezhka.gpx import parse_gpx
from stezhka.process import process


def run(b: Builder):
    return process(parse_gpx(b.gpx()))


def test_commute_with_red_light():
    r = run(commute())
    stops = [s for s in r.stops if s.kind == "stop"]
    assert len(stops) == 1
    assert stops[0].duration_s == pytest.approx(31, abs=3)  # 30 s standing + the arrival interval
    assert r.summary.distance_m == pytest.approx(600, rel=0.03)  # jitter while stopped adds nothing
    assert r.summary.moving_s == pytest.approx(120, abs=3)
    assert r.summary.elapsed_s == pytest.approx(150, abs=1)
    assert r.summary.avg_moving_mps == pytest.approx(5.0, rel=0.05)
    assert r.report.speed_source == "device"


def test_stop_detected_without_device_speed():
    b = Builder().ride(60, 5.0, device_speed=False).stand(30, device_speed=False).ride(60, 5.0, device_speed=False)
    r = run(b)
    assert r.report.speed_source == "derived"
    assert len([s for s in r.stops if s.kind == "stop"]) == 1
    assert r.summary.distance_m == pytest.approx(600, rel=0.03)


def test_silent_recorder_gap_is_a_stop():
    # OpenTracks may log nothing while standing: one 40 s gap with no displacement.
    b = Builder().ride(60, 5.0).stand(1).silent(40).stand(1).ride(60, 5.0)
    r = run(b)
    stops = [s for s in r.stops if s.kind == "stop"]
    assert len(stops) == 1
    assert stops[0].duration_s >= 40


def test_signal_loss_while_moving_is_not_a_stop():
    b = Builder().ride(60, 5.0).silent(20).teleport(east=100).ride(60, 5.0)
    r = run(b)
    assert not [s for s in r.stops if s.kind in ("stop", "break")]
    assert r.summary.moving_s == pytest.approx(140, abs=3)


def test_pushing_the_bike_uphill_is_not_a_stop():
    b = Builder().ride(30, 5.0).ride(80, 0.8, heading="north").ride(30, 5.0)
    r = run(b)
    assert not [s for s in r.stops if s.kind == "stop"]


def test_brief_slowdown_is_not_a_stop():
    b = Builder().ride(30, 5.0).ride(3, 0.5).ride(30, 5.0)
    assert not [s for s in run(b).stops if s.kind == "stop"]


def test_long_stationary_is_a_break():
    r = run(Builder().ride(30, 5.0).stand(300).ride(30, 5.0))
    assert [s.kind for s in r.stops] == ["break"]


def test_recorder_pause_splits_and_excludes_distance():
    b = Builder().ride(60, 5.0).new_segment().silent(600).teleport(east=2000).ride(60, 5.0)
    r = run(b)
    assert [s.kind for s in r.stops] == ["pause"]
    assert r.summary.distance_m == pytest.approx(600, rel=0.03)  # the 2 km jump is not ridden distance
    assert r.summary.moving_s == pytest.approx(118, abs=3)


def test_single_gps_spike_is_dropped():
    b = Builder().ride(60, 5.0).mutate(30, north=800.0)
    r = run(b)
    assert r.report.dropped_spikes == 1
    assert r.summary.distance_m == pytest.approx(300, rel=0.03)
    assert r.summary.max_mps == pytest.approx(5.0, rel=0.05)


def test_inaccurate_points_are_dropped():
    b = Builder().ride(60, 5.0)
    for i in range(10, 15):
        b.mutate(i, hacc=80.0)
    r = run(b)
    assert r.report.dropped_accuracy == 5
    assert r.summary.point_count == 55


def test_duplicate_timestamps_are_dropped():
    b = Builder().ride(10, 5.0)
    b.points.insert(5, b.points[4])
    assert run(b).report.dropped_time == 1


def test_climb_uses_hysteresis():
    b = Builder().ride(100, 3.0)
    for i, p in enumerate(b.points):
        # 50 m steady climb with +-1 m noise on top
        b.mutate(i, ele=100 + i * 0.5 + (1 if i % 2 else -1))
    s = run(b).summary
    assert s.ascent_m == pytest.approx(49.5, abs=4)
    assert s.descent_m < 3
