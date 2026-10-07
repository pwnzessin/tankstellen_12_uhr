"""Command line interface: poll / daemon / report."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

from . import api, collector, db, history, report

DEFAULTS = {
    "api_key": None,
    "db_path": "tankstellen.db",
    "lat": None,
    "lng": None,
    "radius_km": 5.0,
    "poll_interval_seconds": 300,
    "poll_jitter_seconds": 45,
}


def load_config(path: str) -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    return cfg


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tankstellen", description=__doc__)
    p.add_argument("--config", default="config.json", help="path to config.json")
    p.add_argument("--db", help="SQLite file (overrides config)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def area(sp):
        sp.add_argument("--lat", type=float, help="latitude")
        sp.add_argument("--lng", type=float, help="longitude")
        sp.add_argument("--radius", type=float, help="radius in km (max 25)")
        sp.add_argument("--api-key", help="Tankerkoenig API key (or env TANKERKOENIG_API_KEY)")

    sp = sub.add_parser("poll", help="query the API once and store the result")
    area(sp)

    sp = sub.add_parser("daemon", help="poll repeatedly (default every ~5 min with jitter)")
    area(sp)
    sp.add_argument("--interval", type=int, help="seconds between polls (min 300 recommended)")

    sp = sub.add_parser(
        "import",
        help="import Tankerkoenig historical CSV files (single CSV, directory or .zip)",
    )
    sp.add_argument("path", help="prices/stations CSV, a directory containing them, or a .zip")
    sp.add_argument("--lat", type=float, help="latitude of the area centre")
    sp.add_argument("--lng", type=float, help="longitude of the area centre")
    sp.add_argument("--radius", type=float, help="radius in km (no API limit applies here)")

    sp = sub.add_parser("report", help="analyse the stored data")
    sp.add_argument("--since", help="only count increases from this Berlin date on (YYYY-MM-DD)")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument(
        "--tolerance",
        type=int,
        default=history.DEFAULT_TOLERANCE_SECONDS,
        help="seconds after 12:00:00 in which an increase still counts as 'at 12:00' (historical data only)",
    )
    return p


def resolve_area(args, cfg) -> tuple[str, float, float, float]:
    key = args.api_key or os.environ.get("TANKERKOENIG_API_KEY") or cfg["api_key"]
    lat = args.lat if args.lat is not None else cfg["lat"]
    lng = args.lng if args.lng is not None else cfg["lng"]
    rad = args.radius if args.radius is not None else cfg["radius_km"]
    if not key:
        sys.exit("error: no API key (use --api-key, TANKERKOENIG_API_KEY or config.json)")
    if lat is None or lng is None:
        sys.exit("error: lat/lng required (use --lat/--lng or config.json)")
    if not 0 < rad <= api.MAX_RADIUS_KM:
        sys.exit(f"error: radius must be in (0, {api.MAX_RADIUS_KM}] km")
    return key, float(lat), float(lng), float(rad)


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _poll(conn, key, lat, lng, rad) -> None:
    c = collector.poll_once(conn, key, lat, lng, rad)
    _log(
        f"{c['stations']} Tankstellen, {c['increases']} Preiserhoehungen, "
        f"{c['violations']} Verstoesse, {c['skipped_gap']} wegen Luecke uebersprungen"
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(args.db or cfg["db_path"])

    if args.cmd == "report":
        exact = report.format_exact_report(conn, args.since, args.tolerance, args.limit)
        has_polls = conn.execute("SELECT 1 FROM polls LIMIT 1").fetchone() is not None
        if exact:
            print(exact)
        if exact and has_polls:
            print()
        if has_polls or not exact:
            print(report.format_report(conn, args.since, args.limit))
        return 0

    if args.cmd == "import":
        lat = args.lat if args.lat is not None else cfg["lat"]
        lng = args.lng if args.lng is not None else cfg["lng"]
        rad = args.radius if args.radius is not None else cfg["radius_km"]
        if lat is None or lng is None:
            sys.exit("error: lat/lng required (use --lat/--lng or config.json)")
        try:
            t = history.import_path(conn, args.path, float(lat), float(lng), float(rad), progress=_log)
        except (ValueError, OSError) as e:
            sys.exit(f"import error: {e}")
        _log(
            f"fertig: {t['files']} Dateien importiert ({t['skipped_files']} uebersprungen), "
            f"{t['rows_in_area']} von {t['rows']} Zeilen im Gebiet, {t['changes']} Aenderungen, "
            f"{t['stations']} Tankstellen"
        )
        if t["files"] and not conn.execute("SELECT 1 FROM stations LIMIT 1").fetchone():
            _log("Hinweis: keine Tankstellen bekannt - zuerst eine stations-CSV importieren oder 'poll' ausfuehren")
        return 0

    key, lat, lng, rad = resolve_area(args, cfg)
    if args.cmd == "poll":
        try:
            _poll(conn, key, lat, lng, rad)
        except api.ApiError as e:
            sys.exit(f"API error: {e}")
        return 0

    # daemon
    interval = args.interval or cfg["poll_interval_seconds"]
    jitter = cfg["poll_jitter_seconds"]
    _log(f"daemon gestartet: lat={lat} lng={lng} radius={rad}km interval={interval}s (+/-{jitter}s)")
    while True:
        try:
            _poll(conn, key, lat, lng, rad)
        except api.ApiError as e:
            _log(f"API-Fehler: {e}")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # keep the collector alive on transient errors
            _log(f"Fehler: {e!r}")
        try:
            time.sleep(max(60, interval + random.uniform(-jitter, jitter)))
        except KeyboardInterrupt:
            _log("beendet")
            return 0
