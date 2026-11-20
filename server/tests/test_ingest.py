from synth import commute

from stezhka.db import connect
from stezhka.ingest import InboxWatcher, import_bytes, reprocess


def test_import_is_idempotent_and_keeps_raw(tmp_path):
    conn = connect(tmp_path / "db.sqlite3")
    raw = tmp_path / "raw"
    data = commute().gpx()
    first = import_bytes(conn, raw, data, "a.gpx")
    again = import_bytes(conn, raw, data, "renamed.gpx")
    assert first.created and not again.created
    assert first.ride_id == again.ride_id
    stored = list(raw.rglob("*.gpx"))
    assert len(stored) == 1 and stored[0].read_bytes() == data
    n_points = conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    assert n_points == conn.execute("SELECT point_count FROM rides").fetchone()[0]


def test_reprocess_keeps_id_and_user_fields(tmp_path):
    conn = connect(tmp_path / "db.sqlite3")
    raw = tmp_path / "raw"
    rid = import_bytes(conn, raw, commute().gpx(), None).ride_id
    conn.execute("INSERT INTO bikes(name) VALUES ('CUBE')")
    conn.execute("UPDATE rides SET bike_id = 1, notes = 'wet', processing_version = 0")
    done, failed = reprocess(conn, raw)
    assert (done, failed) == (1, [])
    row = conn.execute("SELECT id, bike_id, notes, processing_version FROM rides").fetchone()
    from stezhka.process import PROCESSING_VERSION

    assert tuple(row) == (rid, 1, "wet", PROCESSING_VERSION)
    assert reprocess(conn, raw) == (0, [])  # nothing outdated any more


def test_inbox_waits_for_stable_file_and_ignores_temp(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "ride.gpx").write_bytes(commute().gpx())
    (inbox / ".syncthing.other.gpx.tmp").write_bytes(b"partial")
    (inbox / "notes.txt").write_text("x")
    w = InboxWatcher(lambda: connect(tmp_path / "db.sqlite3"), tmp_path / "raw", inbox)
    assert w.scan_once() == []  # first sighting: wait for the file to settle
    res = w.scan_once()
    assert len(res) == 1 and res[0].created
    assert w.scan_once() == []  # unchanged file is not re-read
    assert (inbox / "ride.gpx").exists()  # never moved: Syncthing would sync the deletion back


def test_inbox_ignores_syncthing_internal_folders(tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / ".stversions").mkdir(parents=True)
    (inbox / ".stfolder").mkdir()
    (inbox / ".stversions" / "old~20261118.gpx").write_bytes(commute().gpx())
    w = InboxWatcher(lambda: connect(tmp_path / "db.sqlite3"), tmp_path / "raw", inbox)
    assert w.scan_once() == [] and w.scan_once() == []
