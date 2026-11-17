"""Command line: `stezhka serve | import | reprocess | set-password | bike-add | backup`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from . import auth
from .config import Config
from .db import backup, connect
from .gpx import GpxError
from .ingest import import_bytes, reprocess


def _cmd_serve(cfg: Config, _args) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, proxy_headers=True, log_level="info")
    return 0


def _cmd_import(cfg: Config, args) -> int:
    conn = connect(cfg.db_path)
    failed = 0
    for f in args.files:
        p = Path(f)
        try:
            res = import_bytes(conn, cfg.raw_dir, p.read_bytes(), p.name)
        except (GpxError, OSError) as e:
            print(f"FAIL {p}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"{'new ' if res.created else 'dup '} ride {res.ride_id}  {p}")
    return 1 if failed else 0


def _cmd_reprocess(cfg: Config, args) -> int:
    done, failed = reprocess(connect(cfg.db_path), cfg.raw_dir, all_rides=args.all)
    print(f"reprocessed {done} ride(s)")
    if failed:
        print(f"failed: {failed}", file=sys.stderr)
    return 1 if failed else 0


def _cmd_set_password(cfg: Config, _args) -> int:
    pw = getpass.getpass("New password: ")
    if pw != getpass.getpass("Repeat: "):
        print("passwords differ", file=sys.stderr)
        return 1
    try:
        auth.set_password(connect(cfg.db_path), pw)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print("password set; all sessions logged out")
    return 0


def _cmd_bike_add(cfg: Config, args) -> int:
    conn = connect(cfg.db_path)
    cur = conn.execute("INSERT INTO bikes(name, kind) VALUES (?, ?)", (args.name, args.kind))
    print(f"bike {cur.lastrowid}: {args.name}")
    return 0


def _cmd_backup(cfg: Config, args) -> int:
    backup(connect(cfg.db_path), Path(args.dest))
    print(f"database snapshot written to {args.dest} (raw files: copy {cfg.raw_dir})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stezhka", description="Stezhka ride statistics server")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web server")
    p = sub.add_parser("import", help="import GPX files")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("reprocess", help="rebuild derived data from raw files")
    p.add_argument("--all", action="store_true", help="all rides, not only outdated ones")
    sub.add_parser("set-password", help="set the login password")
    p = sub.add_parser("bike-add", help="register a bike")
    p.add_argument("name")
    p.add_argument("--kind", help="e.g. mtb, trekking, gravel")
    p = sub.add_parser("backup", help="write a consistent database snapshot")
    p.add_argument("dest")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    handler = {
        "serve": _cmd_serve,
        "import": _cmd_import,
        "reprocess": _cmd_reprocess,
        "set-password": _cmd_set_password,
        "bike-add": _cmd_bike_add,
        "backup": _cmd_backup,
    }[args.cmd]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
