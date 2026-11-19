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
            res = import_bytes(conn, cfg.raw_dir, p.read_bytes(), p.name, allow_deleted=True)
        except (GpxError, OSError) as e:
            print(f"FAIL {p}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"{'new ' if res.created else 'dup '} ride {res.ride_id}  {p}")
    if cfg.enrich and not args.no_enrich:
        _enrich(cfg, conn)
    return 1 if failed else 0


def _enrich(cfg: Config, conn) -> int:
    from .dem import Dem
    from .enrich import enrich_all_pending

    n = enrich_all_pending(conn, Dem(cfg.dem_dir))
    errors = conn.execute("SELECT id, enrich_error FROM rides WHERE enrich_error IS NOT NULL").fetchall()
    print(f"enriched {n} ride(s) with DEM elevation and weather")
    for r in errors:
        print(f"  ride {r['id']}: {r['enrich_error']}", file=sys.stderr)
    return 1 if errors else 0


def _cmd_enrich(cfg: Config, args) -> int:
    conn = connect(cfg.db_path)
    if args.all:
        conn.execute("UPDATE rides SET dem_version = 0, weather_version = 0, enrich_error = NULL")
    return _enrich(cfg, conn)


def _cmd_match(cfg: Config, args) -> int:
    from .segments import basemap_file, match_pending

    if not basemap_file(cfg.basemap_dir).is_file():
        print("no local basemap: run `stezhka basemap` first", file=sys.stderr)
        return 1
    conn = connect(cfg.db_path)
    if args.all:
        conn.execute("UPDATE rides SET match_version = 0")
    n = match_pending(conn, cfg.basemap_dir)
    print(f"matched {n} ride(s) to the road network")
    errors = conn.execute("SELECT id, match_error FROM rides WHERE match_error IS NOT NULL").fetchall()
    for r in errors:
        print(f"  ride {r['id']}: {r['match_error']}", file=sys.stderr)
    return 1 if errors else 0


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


def _bbox(s: str) -> tuple[float, float, float, float]:
    try:
        w, so, e, n = (float(v) for v in s.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected W,S,E,N in degrees") from None
    if not (-180 <= w < e <= 180 and -85 <= so < n <= 85):
        raise argparse.ArgumentTypeError("bbox out of range or inverted")
    return w, so, e, n


def _cmd_basemap(cfg: Config, args) -> int:
    from . import basemap

    stats = basemap.fetch_region(cfg.basemap_dir, args.bbox, args.maxzoom, args.version, args.dry_run)
    verb = "would download" if args.dry_run else "wrote"
    print(f"{verb} {stats.tiles_found} tiles ({stats.unique_tiles} unique, {stats.tile_bytes / 1e6:.1f} MB)")
    if not args.dry_run and not args.skip_assets:
        basemap.fetch_assets(cfg.basemap_dir)
        print(f"styles, sprites and fonts updated in {cfg.basemap_dir}")
    return 0 if stats.tiles_found else 1


def main(argv: list[str] | None = None) -> int:
    from .basemap import DEFAULT_BBOX, DEFAULT_MAXZOOM

    parser = argparse.ArgumentParser(prog="stezhka", description="Stezhka ride statistics server")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web server")
    p = sub.add_parser("import", help="import GPX files")
    p.add_argument("files", nargs="+")
    p.add_argument("--no-enrich", action="store_true", help="skip DEM elevation and weather")
    p = sub.add_parser("enrich", help="add DEM elevation and weather to rides that lack it")
    p.add_argument("--all", action="store_true", help="redo every ride")
    p = sub.add_parser("match", help="match rides to the road network (the server does this by itself)")
    p.add_argument("--all", action="store_true", help="redo every ride")
    p = sub.add_parser("reprocess", help="rebuild derived data from raw files")
    p.add_argument("--all", action="store_true", help="all rides, not only outdated ones")
    sub.add_parser("set-password", help="set the login password")
    p = sub.add_parser("bike-add", help="register a bike")
    p.add_argument("name")
    p.add_argument("--kind", help="e.g. mtb, trekking, gravel")
    p = sub.add_parser("backup", help="write a consistent database snapshot")
    p.add_argument("dest")
    p = sub.add_parser("basemap", help="download/refresh the self-hosted map (OpenFreeMap extract)")
    p.add_argument("--bbox", type=_bbox, default=DEFAULT_BBOX, help="W,S,E,N (default: Zakarpattia + margin)")
    p.add_argument("--maxzoom", type=int, default=DEFAULT_MAXZOOM)
    p.add_argument("--version", help="OpenFreeMap planet build, e.g. 20261115_080022_pt (default: latest)")
    p.add_argument("--dry-run", action="store_true", help="only report what would be downloaded")
    p.add_argument("--skip-assets", action="store_true", help="do not refresh styles/sprites/fonts")
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
        "basemap": _cmd_basemap,
        "enrich": _cmd_enrich,
        "match": _cmd_match,
    }[args.cmd]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
