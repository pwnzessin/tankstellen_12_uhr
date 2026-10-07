"""Poll the API, store observations, detect price increases."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import api, detect
from .db import transaction


def ingest(
    conn: sqlite3.Connection,
    stations: list[api.StationPrices],
    now: datetime,
    lat: float,
    lng: float,
    radius_km: float,
    max_gap: timedelta = detect.DEFAULT_MAX_GAP,
) -> dict:
    """Store one poll result. Returns summary counters."""
    now = now.astimezone(timezone.utc)
    ts = detect.fmt_ts(now)
    counters = {"stations": len(stations), "increases": 0, "violations": 0, "skipped_gap": 0}

    with transaction(conn):
        conn.execute(
            "INSERT INTO polls(ts_utc, lat, lng, radius_km, stations) VALUES (?,?,?,?,?)",
            (ts, lat, lng, radius_km, len(stations)),
        )
        for s in stations:
            conn.execute(
                """INSERT INTO stations(id,name,brand,street,house_number,post_code,place,lat,lng,first_seen,last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, brand=excluded.brand, street=excluded.street,
                     house_number=excluded.house_number, post_code=excluded.post_code,
                     place=excluded.place, lat=excluded.lat, lng=excluded.lng,
                     last_seen=excluded.last_seen""",
                (s.id, s.name, s.brand, s.street, s.house_number, s.post_code,
                 s.place, s.lat, s.lng, ts, ts),
            )
            for fuel in api.FUELS:
                price = s.prices[fuel]
                prev = conn.execute(
                    """SELECT ts_utc, price FROM price_observations
                       WHERE station_id=? AND fuel=? ORDER BY ts_utc DESC LIMIT 1""",
                    (s.id, fuel),
                ).fetchone()
                conn.execute(
                    """INSERT INTO price_observations(station_id,fuel,ts_utc,price,is_open)
                       VALUES (?,?,?,?,?)""",
                    (s.id, fuel, ts, price, int(s.is_open)),
                )
                if prev is None or not detect.find_increase(prev["price"], price):
                    continue
                prev_dt = detect.parse_ts(prev["ts_utc"])
                if now - prev_dt > max_gap:
                    counters["skipped_gap"] += 1
                    continue
                day = detect.berlin_day(now).isoformat()
                earlier = conn.execute(
                    """SELECT COUNT(*) FROM price_increases
                       WHERE station_id=? AND fuel=? AND berlin_day=?""",
                    (s.id, fuel, day),
                ).fetchone()[0]
                verdict = detect.classify_increase(prev_dt, now, earlier)
                conn.execute(
                    """INSERT OR IGNORE INTO price_increases
                       (station_id,fuel,prev_ts_utc,ts_utc,prev_price,price,verdict,berlin_day)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (s.id, fuel, prev["ts_utc"], ts, prev["price"], price, verdict, day),
                )
                counters["increases"] += 1
                if verdict != detect.OK_NOON:
                    counters["violations"] += 1
    return counters


def poll_once(
    conn: sqlite3.Connection,
    api_key: str,
    lat: float,
    lng: float,
    radius_km: float,
    max_gap: timedelta = detect.DEFAULT_MAX_GAP,
) -> dict:
    stations = api.fetch_radius(api_key, lat, lng, radius_km)
    return ingest(conn, stations, datetime.now(timezone.utc), lat, lng, radius_km, max_gap)
