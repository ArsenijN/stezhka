"""SQLite storage. One file, WAL mode, forward-only migrations tracked in PRAGMA user_version.

Raw track files are the source of truth; `points` and `stops` are derived and can be rebuilt.
Never put this database on a network share (SMB/NFS): SQLite locking is unreliable there.
Back it up with `stezhka backup` (online, consistent snapshot) instead of copying the file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        user_agent TEXT
    );

    CREATE TABLE bikes (
        id      INTEGER PRIMARY KEY,
        name    TEXT NOT NULL UNIQUE,
        kind    TEXT,
        notes   TEXT,
        retired INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE rides (
        id                 INTEGER PRIMARY KEY,
        sha256             TEXT NOT NULL UNIQUE,
        raw_path           TEXT NOT NULL,
        source_name        TEXT,
        creator            TEXT,
        name               TEXT,
        activity           TEXT,
        bike_id            INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
        notes              TEXT,
        started_at         REAL NOT NULL,
        ended_at           REAL NOT NULL,
        elapsed_s          REAL NOT NULL,
        moving_s           REAL NOT NULL,
        distance_m         REAL NOT NULL,
        avg_moving_mps     REAL NOT NULL,
        max_mps            REAL NOT NULL,
        ascent_m           REAL,
        descent_m          REAL,
        point_count        INTEGER NOT NULL,
        dropped_points     INTEGER NOT NULL,
        speed_source       TEXT NOT NULL,
        processing_version INTEGER NOT NULL,
        imported_at        REAL NOT NULL
    );
    CREATE INDEX rides_started_at ON rides(started_at);

    CREATE TABLE points (
        ride_id    INTEGER NOT NULL REFERENCES rides(id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,
        t          REAL NOT NULL,
        lat        REAL NOT NULL,
        lon        REAL NOT NULL,
        ele        REAL,
        ele_smooth REAL,
        speed      REAL NOT NULL,
        hacc       REAL,
        dist       REAL NOT NULL,
        segment    INTEGER NOT NULL,
        moving     INTEGER NOT NULL,
        PRIMARY KEY (ride_id, idx)
    ) WITHOUT ROWID;

    CREATE TABLE stops (
        id        INTEGER PRIMARY KEY,
        ride_id   INTEGER NOT NULL REFERENCES rides(id) ON DELETE CASCADE,
        kind      TEXT NOT NULL,
        start_idx INTEGER NOT NULL,
        end_idx   INTEGER NOT NULL,
        start_t   REAL NOT NULL,
        end_t     REAL NOT NULL,
        lat       REAL NOT NULL,
        lon       REAL NOT NULL,
        reason    TEXT
    );
    CREATE INDEX stops_ride ON stops(ride_id);
    """,
    # 2: enrichment - DEM elevation and weather
    """
    ALTER TABLE points ADD COLUMN ele_dem REAL;
    ALTER TABLE rides ADD COLUMN ele_source TEXT NOT NULL DEFAULT 'gps';
    ALTER TABLE rides ADD COLUMN dem_version INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE rides ADD COLUMN weather_version INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE rides ADD COLUMN enrich_error TEXT;
    ALTER TABLE rides ADD COLUMN enrich_attempt_at REAL;

    CREATE TABLE weather_hourly (
        cell_lat         REAL NOT NULL,
        cell_lon         REAL NOT NULL,
        t                REAL NOT NULL,
        temperature_c    REAL,
        humidity_pct     REAL,
        precipitation_mm REAL,
        rain_mm          REAL,
        snowfall_cm      REAL,
        weather_code     INTEGER,
        wind_speed_mps   REAL,
        wind_dir_deg     REAL,
        wind_gust_mps    REAL,
        source           TEXT NOT NULL,
        fetched_at       REAL NOT NULL,
        PRIMARY KEY (cell_lat, cell_lon, t)
    ) WITHOUT ROWID;

    CREATE TABLE ride_weather (
        ride_id          INTEGER PRIMARY KEY REFERENCES rides(id) ON DELETE CASCADE,
        temperature_c    REAL,
        humidity_pct     REAL,
        wind_speed_mps   REAL,
        wind_dir_deg     REAL,
        wind_gust_mps    REAL,
        headwind_mps     REAL,
        precip_ride_mm   REAL,
        precip_before_mm REAL,
        wetness_mm       REAL,
        wetness          TEXT,
        weather_code     INTEGER,
        source           TEXT NOT NULL
    );
    """,
    # 3: deleted rides are remembered by file hash so the inbox never re-imports them
    """
    CREATE TABLE deleted_rides (
        sha256     TEXT PRIMARY KEY,
        deleted_at REAL NOT NULL
    ) WITHOUT ROWID;
    """,
    # 4: per-device upload keys (e.g. an iOS Shortcut on the share sheet); upload only
    """
    CREATE TABLE upload_tokens (
        id           INTEGER PRIMARY KEY,
        name         TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   REAL NOT NULL,
        last_used_at REAL
    );
    """,
    # 5: deleting moves a ride to the trash first (undo, "recently deleted"); purged after TRASH_DAYS
    """
    ALTER TABLE rides ADD COLUMN trashed_at REAL;
    CREATE INDEX rides_trashed ON rides(trashed_at) WHERE trashed_at IS NOT NULL;
    """,
    # 6: several recordings of one trip (two phones): the others point to the main ride (recordings.py)
    """
    ALTER TABLE rides ADD COLUMN primary_id INTEGER REFERENCES rides(id) ON DELETE SET NULL;
    ALTER TABLE rides ADD COLUMN unlinked INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE rides ADD COLUMN name_edited INTEGER NOT NULL DEFAULT 0; -- typed by the user, not from the GPX
    CREATE INDEX rides_primary ON rides(primary_id) WHERE primary_id IS NOT NULL;
    """,
    # 7: how a ride felt (feelings.py): -1 / 0 / 1 + reason tags, and marks on stretches of it
    """
    ALTER TABLE rides ADD COLUMN feeling INTEGER CHECK (feeling IN (-1, 0, 1));
    ALTER TABLE rides ADD COLUMN feeling_tags TEXT; -- JSON array of feelings.TAGS
    CREATE TABLE ride_marks (
        id         INTEGER PRIMARY KEY,
        ride_id    INTEGER NOT NULL REFERENCES rides(id) ON DELETE CASCADE,
        start_t    REAL NOT NULL,  -- by time and position: point indices change on reprocessing
        end_t      REAL NOT NULL,
        start_lat  REAL NOT NULL,
        start_lon  REAL NOT NULL,
        end_lat    REAL NOT NULL,
        end_lon    REAL NOT NULL,
        tags       TEXT,           -- JSON array of feelings.TAGS
        note       TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX ride_marks_ride ON ride_marks(ride_id, start_t);
    """,
    # 8: the user picked this recording as main ("Make main"): the preferred-app setting leaves it be
    """
    ALTER TABLE rides ADD COLUMN main_chosen INTEGER NOT NULL DEFAULT 0;
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise RuntimeError(
            f"database schema v{version} is newer than this program (v{len(MIGRATIONS)}); upgrade stezhka"
        )
    for i in range(version, len(MIGRATIONS)):
        with tx(conn):
            for stmt in _split(MIGRATIONS[i]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {i + 1}")
    return len(MIGRATIONS)


def _split(script: str) -> list[str]:
    # Migrations contain no semicolons inside literals, so a plain split is safe.
    return [s.strip() for s in script.split(";") if s.strip()]


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit write transaction (BEGIN IMMEDIATE avoids lock-upgrade deadlocks)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def backup(conn: sqlite3.Connection, dest: Path) -> None:
    """Consistent online snapshot, safe while the server is running."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    out = sqlite3.connect(tmp)
    try:
        conn.backup(out)
    finally:
        out.close()
    tmp.replace(dest)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
