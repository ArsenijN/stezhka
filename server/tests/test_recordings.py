"""Two phones on one trip: linked into one ride, switchable, splittable, trashed together."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from synth import commute, to_gpx

from stezhka import api, auth, recordings
from stezhka.config import Config
from stezhka.db import connect


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path, cookie_secure=False, enrich=False)


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)
    with TestClient(api.create_app(cfg)) as c:
        auth.set_password(connect(cfg.db_path), "correct horse")
        assert c.post("/api/login", json={"password": "correct horse"}).status_code == 200
        yield c


def phone_a() -> bytes:  # 1 Hz, like the iPhone
    return commute().gpx()


def phone_b(north: float = 8.0, dt_shift: float = 0.0) -> bytes:
    """The same trip seen by a second phone: every 3rd fix, a few metres off."""
    pts = [replace(p, north=p.north + north, t=p.t + dt_shift) for p in commute().points[::3]]
    return to_gpx(pts, name="2026-09-01T07:30+02")


def upload(client, data) -> int:
    r = client.post("/api/rides", content=data)
    assert r.status_code == 201
    return r.json()["id"]


def listed(client) -> list[int]:
    return [r["id"] for r in client.get("/api/rides").json()["rides"]]


def test_second_phone_is_linked_and_the_denser_one_is_main(client):
    b = upload(client, phone_b())
    a = upload(client, phone_a())
    assert listed(client) == [a]  # one trip, the 1 Hz recording is main even though it came second
    ride = client.get(f"/api/rides/{a}").json()
    assert [r["id"] for r in ride["recordings"]] == [b]
    assert client.get("/api/rides").json()["rides"][0]["other_creators"] == "OpenTracks"
    other = client.get(f"/api/rides/{b}").json()  # still reachable for comparing
    assert other["primary_id"] == a and [r["id"] for r in other["recordings"]] == [a]


def test_other_trips_are_not_linked(client):
    a = upload(client, phone_a())
    far = upload(client, phone_b(north=2000))  # same time, 2 km away: a friend's ride
    later = upload(client, phone_b(dt_shift=3600))  # same place, an hour later
    assert sorted(listed(client)) == sorted([a, far, later])


def test_user_fields_follow_the_main_ride(client):
    b = upload(client, phone_b())
    client.patch(f"/api/rides/{b}", json={"name": "Evening, phones swapped", "notes": "rain"})
    a = upload(client, phone_a())
    ride = client.get(f"/api/rides/{a}").json()
    assert (ride["name"], ride["notes"]) == ("Evening, phones swapped", "rain")

    assert client.post(f"/api/rides/{b}/main").json() == {"ok": True}
    assert listed(client) == [b]
    assert client.get(f"/api/rides/{b}").json()["notes"] == "rain"


def test_split_is_permanent(client, cfg):
    a = upload(client, phone_a())
    b = upload(client, phone_b())
    assert client.post(f"/api/rides/{b}/split").json() == {"ok": True}
    assert sorted(listed(client)) == sorted([a, b])
    assert recordings.link_all(connect(cfg.db_path)) == 0  # never linked again automatically
    assert client.post(f"/api/rides/{b}/split").json() == {"error": "not_found"}


def test_existing_rides_are_linked_at_startup(cfg):
    from stezhka.ingest import import_bytes

    conn = connect(cfg.db_path)
    a = import_bytes(conn, cfg.raw_dir, phone_a(), "a.gpx").ride_id
    b = import_bytes(conn, cfg.raw_dir, phone_b(), "b.gpx").ride_id
    conn.execute("UPDATE rides SET primary_id = NULL")  # as if imported before linking existed
    assert recordings.link_all(conn) == 1
    assert conn.execute("SELECT primary_id FROM rides WHERE id = ?", (b,)).fetchone()[0] == a


def test_trip_is_trashed_restored_and_erased_together(client, cfg):
    a = upload(client, phone_a())
    b = upload(client, phone_b())
    client.delete(f"/api/rides/{a}")
    assert listed(client) == []
    assert [r["id"] for r in client.get("/api/trash").json()["rides"]] == [a]  # one entry for the trip
    client.post(f"/api/trash/{a}/restore")
    assert listed(client) == [a]
    assert [r["id"] for r in client.get(f"/api/rides/{a}").json()["recordings"]] == [b]

    client.delete(f"/api/rides/{a}")
    assert client.delete(f"/api/trash/{a}").json() == {"ok": True}
    assert list(cfg.raw_dir.rglob("*.gpx")) == []
    assert client.get("/api/trash").json()["rides"] == []
