"""Importing rides: raw file storage, dedupe, (re)processing, inbox watching."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .db import tx
from .gpx import GpxError, ParsedTrack, parse_gpx
from .process import PROCESSING_VERSION, ProcessedRide, process
from .recordings import link_new

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
TRASH_DAYS = 30  # a deleted ride can be restored this long, then it is erased with its GPX file


@dataclass(frozen=True, slots=True)
class ImportResult:
    ride_id: int  # 0 when skipped
    created: bool
    skipped_deleted: bool = False  # the file belongs to a ride the user deleted


def _store_raw(raw_dir: Path, sha: str, data: bytes) -> str:
    """Write the original bytes once, atomically. Returns the path relative to raw_dir."""
    rel = Path(sha[:2]) / f"{sha}.gpx"
    dest = raw_dir / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return rel.as_posix()


def _ride_values(track: ParsedTrack, pr: ProcessedRide) -> dict:
    s = pr.summary
    return {
        "creator": track.creator,
        "name": track.name,
        "activity": track.activity,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "elapsed_s": s.elapsed_s,
        "moving_s": s.moving_s,
        "distance_m": s.distance_m,
        "avg_moving_mps": s.avg_moving_mps,
        "max_mps": s.max_mps,
        "ascent_m": s.ascent_m,
        "descent_m": s.descent_m,
        "point_count": s.point_count,
        "dropped_points": pr.report.dropped,
        "speed_source": pr.report.speed_source,
        "processing_version": PROCESSING_VERSION,
        # derived points are rewritten, so enrichment must run again
        "ele_source": "gps",
        "dem_version": 0,
        "weather_version": 0,
        "enrich_error": None,
    }


def _write_derived(conn: sqlite3.Connection, ride_id: int, pr: ProcessedRide) -> None:
    conn.execute("DELETE FROM points WHERE ride_id = ?", (ride_id,))
    conn.execute("DELETE FROM stops WHERE ride_id = ?", (ride_id,))
    conn.executemany(
        "INSERT INTO points(ride_id, idx, t, lat, lon, ele, ele_smooth, speed, hacc, dist, segment, moving)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (ride_id, i, p.t, p.lat, p.lon, p.ele, p.ele_smooth, p.speed, p.hacc, p.dist, p.segment, int(p.moving))
            for i, p in enumerate(pr.points)
        ),
    )
    conn.executemany(
        "INSERT INTO stops(ride_id, kind, start_idx, end_idx, start_t, end_t, lat, lon) VALUES (?,?,?,?,?,?,?,?)",
        ((ride_id, s.kind, s.start_idx, s.end_idx, s.start_t, s.end_t, s.lat, s.lon) for s in pr.stops),
    )


def import_bytes(
    conn: sqlite3.Connection, raw_dir: Path, data: bytes, source_name: str | None, *, allow_deleted: bool = False
) -> ImportResult:
    """Import one GPX file. Idempotent: the same bytes always map to the same ride.

    Files of deleted rides are skipped unless allow_deleted (an explicit upload/import by the user,
    as opposed to the inbox re-scanning a folder), which also forgets the deletion.
    """
    if len(data) > MAX_FILE_BYTES:
        raise GpxError("file too large")
    sha = hashlib.sha256(data).hexdigest()
    if conn.execute("SELECT 1 FROM deleted_rides WHERE sha256 = ?", (sha,)).fetchone():
        if not allow_deleted:
            return ImportResult(0, created=False, skipped_deleted=True)
        conn.execute("DELETE FROM deleted_rides WHERE sha256 = ?", (sha,))
    row = conn.execute("SELECT id, trashed_at FROM rides WHERE sha256 = ?", (sha,)).fetchone()
    if row:
        if row["trashed_at"] is None:
            return ImportResult(row["id"], created=False)
        if not allow_deleted:
            return ImportResult(0, created=False, skipped_deleted=True)
        restore_ride(conn, row["id"])  # uploading a trashed ride's file again brings it back
        return ImportResult(row["id"], created=True)

    track = parse_gpx(data)
    pr = process(track)
    rel = _store_raw(raw_dir, sha, data)
    vals = _ride_values(track, pr)
    vals.update(sha256=sha, raw_path=rel, source_name=source_name, imported_at=time.time())
    cols = ", ".join(vals)
    marks = ", ".join(f":{k}" for k in vals)
    with tx(conn):
        cur = conn.execute(f"INSERT INTO rides({cols}) VALUES ({marks})", vals)
        ride_id = cur.lastrowid
        _write_derived(conn, ride_id, pr)
    log.info("imported ride %s from %s (%d points)", ride_id, source_name, pr.summary.point_count)
    try:
        link_new(conn, ride_id)  # a second phone's recording of the same trip
    except Exception:  # linking is a convenience: never lose an import over it
        log.exception("linking ride %s failed", ride_id)
    return ImportResult(ride_id, created=True)


def trash_ride(conn: sqlite3.Connection, ride_id: int) -> bool:
    """Move a ride (with its other recordings) to the trash: hidden everywhere, restorable for
    TRASH_DAYS. False if not found."""
    if not conn.execute("SELECT 1 FROM rides WHERE id = ? AND trashed_at IS NULL", (ride_id,)).fetchone():
        return False
    with tx(conn):
        conn.execute(
            "UPDATE rides SET trashed_at = ? WHERE (id = ? OR primary_id = ?) AND trashed_at IS NULL",
            (time.time(), ride_id, ride_id),
        )
    log.info("trashed ride %s", ride_id)
    return True


def restore_ride(conn: sqlite3.Connection, ride_id: int) -> bool:
    """Take a ride out of the trash, with the recordings trashed together with it. A recording whose
    main ride is in the trash brings that whole ride back. False if it is not in the trash."""
    row = conn.execute("SELECT primary_id, trashed_at FROM rides WHERE id = ?", (ride_id,)).fetchone()
    if row is None:
        return False
    main = row["primary_id"] or ride_id
    when = conn.execute("SELECT trashed_at FROM rides WHERE id = ?", (main,)).fetchone()["trashed_at"]
    if when is None:
        if row["trashed_at"] is None:
            return False
        main, when = ride_id, row["trashed_at"]  # trashed on its own
    with tx(conn):
        conn.execute(
            "UPDATE rides SET trashed_at = NULL WHERE id = ? OR (primary_id = ? AND trashed_at = ?) OR id = ?",
            (main, main, when, ride_id),
        )
    log.info("restored ride %s", main)
    return True


def purge_trash(conn: sqlite3.Connection, raw_dir: Path, now: float | None = None) -> int:
    """Erase rides that have been in the trash for TRASH_DAYS. Returns how many."""
    cutoff = (now if now is not None else time.time()) - TRASH_DAYS * 86400
    ids = [r["id"] for r in conn.execute("SELECT id FROM rides WHERE trashed_at < ?", (cutoff,))]
    return sum(delete_ride(conn, raw_dir, i) for i in ids)


def delete_ride(conn: sqlite3.Connection, raw_dir: Path, ride_id: int) -> bool:
    """Erase a ride now: derived data and raw file; remember the hash (see import_bytes)."""
    row = conn.execute("SELECT sha256, raw_path FROM rides WHERE id = ?", (ride_id,)).fetchone()
    if not row:
        return False
    with tx(conn):
        conn.execute(
            "INSERT OR REPLACE INTO deleted_rides(sha256, deleted_at) VALUES (?, ?)", (row["sha256"], time.time())
        )
        conn.execute("DELETE FROM rides WHERE id = ?", (ride_id,))  # points, stops, weather cascade
    (raw_dir / row["raw_path"]).unlink(missing_ok=True)
    log.info("deleted ride %s", ride_id)
    return True


def reprocess(conn: sqlite3.Connection, raw_dir: Path, *, all_rides: bool = False) -> tuple[int, list[int]]:
    """Rebuild derived data from raw files. Keeps ride ids and user edits (bike, notes).

    Returns (number reprocessed, ids that failed). A failing ride keeps its old derived data.
    """
    where = "" if all_rides else " WHERE processing_version < ?"
    args = () if all_rides else (PROCESSING_VERSION,)
    rows = conn.execute(f"SELECT id, raw_path FROM rides{where}", args).fetchall()
    done, failed = 0, []
    for r in rows:
        try:
            track = parse_gpx((raw_dir / r["raw_path"]).read_bytes())
            pr = process(track)
        except (GpxError, OSError) as e:
            log.error("reprocess ride %s failed: %s", r["id"], e)
            failed.append(r["id"])
            continue
        vals = _ride_values(track, pr)
        sets = ", ".join(f"{k} = :{k}" for k in vals)
        with tx(conn):
            conn.execute(f"UPDATE rides SET {sets} WHERE id = :id", {**vals, "id": r["id"]})
            _write_derived(conn, r["id"], pr)
        done += 1
    return done, failed


def _is_candidate(p: Path) -> bool:
    name = p.name
    return (
        p.is_file()
        and name.lower().endswith(".gpx")
        and not name.startswith((".", "~"))  # Syncthing temp files start with ".syncthing."
    )


class InboxWatcher:
    """Polls a folder (e.g. a Syncthing receive-only folder) and imports new GPX files.

    Files are never moved or deleted: with Syncthing that would sync back to the phone.
    Already-seen files are remembered by (name, size, mtime); content dedupe is by SHA-256.
    A file is imported only once its size and mtime are unchanged across two polls.
    """

    def __init__(self, connect, raw_dir: Path, inbox: Path, poll_s: float = 30.0, on_import=None):
        self._on_import = on_import  # called (no args) after a scan that created new rides
        self._connect = connect
        self._raw_dir = raw_dir
        self._inbox = inbox
        self._poll_s = poll_s
        self._seen: dict[Path, tuple[int, float]] = {}
        self._pending: dict[Path, tuple[int, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def scan_once(self) -> list[ImportResult]:
        results = []
        if not self._inbox.is_dir():
            return results
        conn = self._connect()
        try:
            for p in sorted(self._inbox.rglob("*")):
                # skip hidden trees: Syncthing's .stfolder / .stversions (old copies of files)
                if any(part.startswith(".") for part in p.relative_to(self._inbox).parts[:-1]):
                    continue
                if not _is_candidate(p):
                    continue
                st = p.stat()
                sig = (st.st_size, st.st_mtime)
                if self._seen.get(p) == sig:
                    continue
                if self._pending.get(p) != sig:
                    self._pending[p] = sig  # wait one more poll for the file to settle
                    continue
                self._pending.pop(p, None)
                self._seen[p] = sig
                try:
                    results.append(import_bytes(conn, self._raw_dir, p.read_bytes(), p.name))
                except (GpxError, OSError) as e:
                    log.warning("inbox: cannot import %s: %s", p, e)
        finally:
            conn.close()
        if self._on_import and any(r.created for r in results):
            self._on_import()
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:  # keep the watcher alive; the error is logged with traceback
                log.exception("inbox scan failed")
            self._stop.wait(self._poll_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="stezhka-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class TrashPurger:
    """Background thread: purge_trash() at start and then every `interval_s`."""

    def __init__(self, connect, raw_dir: Path, interval_s: float = 6 * 3600):
        self._connect = connect
        self._raw_dir = raw_dir
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._connect()
                try:
                    n = purge_trash(conn, self._raw_dir)
                finally:
                    conn.close()
                if n:
                    log.info("erased %d ride(s) from the trash", n)
            except Exception:  # keep the thread alive; logged with traceback
                log.exception("trash purge failed")
            self._stop.wait(self._interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="stezhka-trash", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
