"""How a ride felt: like / neutral / dislike + reasons, and marks on stretches of a ride."""

import pytest
from fastapi.testclient import TestClient
from synth import T0, commute

from stezhka import api, auth
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


@pytest.fixture
def ride(client):
    return client.post("/api/rides", content=commute().gpx()).json()["id"]


def test_feeling_and_reasons(client, ride):
    r = client.get(f"/api/rides/{ride}").json()
    assert r["feeling"] is None and r["feeling_tags"] == [] and r["marks"] == []
    body = {"feeling": -1, "feeling_tags": ["traffic", "puddles", "traffic"]}
    r = client.patch(f"/api/rides/{ride}", json=body).json()
    assert r["feeling"] == -1 and r["feeling_tags"] == ["puddles", "traffic"]  # canonical, once each
    assert client.get("/api/rides").json()["rides"][0]["feeling"] == -1
    assert client.patch(f"/api/rides/{ride}", json={"feeling": 0}).json()["feeling"] == 0  # neutral is a value
    assert client.patch(f"/api/rides/{ride}", json={"feeling": 2}).json() == {"error": "bad_request"}
    assert client.patch(f"/api/rides/{ride}", json={"feeling_tags": ["nonsense"]}).json() == {"error": "bad_request"}
    r = client.patch(f"/api/rides/{ride}", json={"feeling": None}).json()  # not rated: no reasons left
    assert r["feeling"] is None and r["feeling_tags"] == []


def test_marks(client, ride):
    m = client.post(f"/api/rides/{ride}/marks", json={"start_t": T0 + 80, "end_t": T0 + 40, "tags": ["surface"],
                                                      "note": "  deep pothole by the bus stop "})
    assert m.status_code == 201
    spot = client.post(f"/api/rides/{ride}/marks", json={"start_t": T0 + 10, "end_t": T0 + 10, "tags": ["scenic"]})
    marks = client.get(f"/api/rides/{ride}").json()["marks"]
    assert [x["id"] for x in marks] == [spot.json()["id"], m.json()["id"]]  # in order along the ride
    assert marks[1]["start_t"] < marks[1]["end_t"] and marks[1]["note"] == "deep pothole by the bus stop"

    mid = m.json()["id"]
    assert client.patch(f"/api/marks/{mid}", json={"tags": ["puddles"], "note": ""}).json() == {"ok": True}
    after = client.get(f"/api/rides/{ride}").json()["marks"][1]
    assert after["tags"] == ["puddles"] and after["note"] is None
    assert client.delete(f"/api/marks/{mid}").json() == {"ok": True}
    assert client.delete(f"/api/marks/{mid}").json() == {"error": "not_found"}

    # outside the ride, or unknown reasons
    assert client.post(f"/api/rides/{ride}/marks", json={"start_t": T0 - 600, "end_t": T0}).json() == {"error": "bad_request"}
    assert client.post(f"/api/rides/{ride}/marks", json={"start_t": T0, "end_t": T0 + 5, "tags": ["x"]}).json() == {"error": "bad_request"}


def test_marks_and_feeling_follow_the_main_recording(client):
    from dataclasses import replace

    from synth import to_gpx

    b = client.post("/api/rides", content=to_gpx([replace(p, north=p.north + 8) for p in commute().points[::3]])).json()["id"]
    client.patch(f"/api/rides/{b}", json={"feeling": 1, "feeling_tags": ["quiet"]})
    client.post(f"/api/rides/{b}/marks", json={"start_t": T0 + 20, "end_t": T0 + 50, "note": "nice"})
    a = client.post("/api/rides", content=commute().gpx()).json()["id"]  # denser: becomes main
    r = client.get(f"/api/rides/{a}").json()
    assert r["feeling"] == 1 and r["feeling_tags"] == ["quiet"]
    assert [m["note"] for m in r["marks"]] == ["nice"]
    client.post(f"/api/rides/{b}/main")
    assert [m["note"] for m in client.get(f"/api/rides/{b}").json()["marks"]] == ["nice"]
