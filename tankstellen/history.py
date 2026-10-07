"""Import of the Tankerkoenig historical CSV price files + exact 12:00 analysis.

Expected formats (as published in the Tankerkoenig data repository):

  prices (one file per day, e.g. 2026/04/2026-04-01-prices.csv):
      date,station_uuid,diesel,e5,e10,dieselchange,e5change,e10change
      2026-04-01 00:00:11+02,<uuid>,1.859,1.999,1.939,0,1,0

  stations (e.g. stations/2026/04/2026-04-01-stations.csv):
      uuid,name,brand,street,house_number,post_code,city,latitude,longitude,...

Change flags: 0 = unchanged, 1 = changed, 2 = removed (no longer offered),
3 = new (first price, or first price after an MTS-K outage). Only flag 1 can be
a "price increase" in the sense of the 12:00 rule; 3 is a (re)start of a price
series and is never counted.

The parser is tolerant: column order is taken from the header, decimal commas
and prices in tenth-of-cent integers (e.g. 1859) are normalised.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Iterator

from . import detect
from .db import transaction

FUELS = ("diesel", "e5", "e10")
EPS = detect.EPS

# Default tolerance after 12:00:00 in which an increase still counts as "at 12:00".
DEFAULT_TOLERANCE_SECONDS = 300


# ---------------------------------------------------------------- parsing ---

_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$"
)


def parse_csv_ts(value: str) -> datetime:
    """Parse '2026-04-01 12:00:03+02' (also +02:00, +0200, Z, or no offset).

    Without an offset the time is interpreted as Europe/Berlin local time.
    Returns an aware UTC datetime.
    """
    m = _TS_RE.match(value.strip())
    if not m:
        raise ValueError(f"unrecognised timestamp: {value!r}")
    day, clock, off = m.groups()
    naive = datetime.fromisoformat(f"{day}T{clock}")
    if off is None:
        return naive.replace(tzinfo=detect.BERLIN).astimezone(timezone.utc)
    if off == "Z":
        return naive.replace(tzinfo=timezone.utc)
    sign = 1 if off[0] == "+" else -1
    digits = off[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4]) if len(digits) >= 4 else 0
    tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def parse_price(value: str | None) -> float | None:
    """Return the price in EUR or None when empty/invalid.

    Accepts '1.859', '1,859' and tenth-of-cent integers like '1859'.
    -1 (invalid price in the DB dump) and 0 are treated as missing.
    """
    if value is None:
        return None
    v = value.strip().replace(",", ".")
    if not v or v.lower() in ("nan", "null", "none"):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    if f <= 0:
        return None
    if f >= 100:  # tenth-of-cent integer
        f = f / 1000.0
    return f


@dataclass(frozen=True)
class PriceRow:
    ts: datetime  # UTC
    station_id: str
    prices: dict[str, float | None]
    change: dict[str, int]


def read_price_rows(fh: Iterable[str]) -> Iterator[PriceRow]:
    reader = csv.DictReader(fh)
    if reader.fieldnames is None:
        return
    reader.fieldnames = [f.strip().lstrip("\ufeff").lower() for f in reader.fieldnames]
    required = {"date", "station_uuid"}
    missing = required - set(reader.fieldnames)
    if missing:
        raise ValueError(f"price CSV is missing columns: {sorted(missing)}")
    for row in reader:
        try:
            ts = parse_csv_ts(row["date"])
        except ValueError:
            continue
        prices = {f: parse_price(row.get(f)) for f in FUELS}
        change = {}
        for f in FUELS:
            raw = (row.get(f + "change") or "").strip()
            try:
                change[f] = int(float(raw)) if raw else 0
            except ValueError:
                change[f] = 0
        yield PriceRow(ts, row["station_uuid"].strip(), prices, change)


@dataclass(frozen=True)
class StationRow:
    id: str
    name: str
    brand: str
    street: str
    house_number: str
    post_code: str
    place: str
    lat: float
    lng: float


def read_station_rows(fh: Iterable[str]) -> Iterator[StationRow]:
    reader = csv.DictReader(fh)
    if reader.fieldnames is None:
        return
    reader.fieldnames = [f.strip().lstrip("\ufeff").lower() for f in reader.fieldnames]
    for row in reader:
        try:
            lat = float(row.get("latitude") or row.get("lat") or "")
            lng = float(row.get("longitude") or row.get("lng") or row.get("lon") or "")
        except ValueError:
            continue
        sid = (row.get("uuid") or row.get("id") or "").strip()
        if not sid:
            continue
        yield StationRow(
            id=sid,
            name=(row.get("name") or "").strip(),
            brand=(row.get("brand") or "").strip(),
            street=(row.get("street") or "").strip(),
            house_number=(row.get("house_number") or "").strip(),
            post_code=(row.get("post_code") or "").strip(),
            place=(row.get("city") or row.get("place") or "").strip(),
            lat=lat,
            lng=lng,
        )


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ----------------------------------------------------------- file sources ---

def _kind(name: str) -> str | None:
    base = os.path.basename(name).lower()
    if not base.endswith(".csv"):
        return None
    if "prices" in base:
        return "prices"
    if "stations" in base:
        return "stations"
    return None


def iter_sources(path: str) -> Iterator[tuple[str, str, io.TextIOBase]]:
    """Yield (kind, name, text handle) for every CSV in a file, directory or zip.

    Order: all stations files first (so the area is known), then all prices
    files. Within each group sorted by name; Tankerkoenig names start with the
    date so this is chronological, which the change detection relies on.
    """
    def order(items):
        # (kind, full name) -> stations before prices, then by basename (= date)
        return sorted(items, key=lambda it: (it[0] != "stations", os.path.basename(it[1]), it[1]))

    if os.path.isdir(path):
        files = []
        for root, _dirs, names in os.walk(path):
            for n in names:
                k = _kind(n)
                if k:
                    files.append((k, os.path.join(root, n)))
        for k, f in order(files):
            with open(f, encoding="utf-8-sig", newline="") as fh:
                yield k, os.path.basename(f), fh
    elif path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            entries = [(_kind(n), n) for n in z.namelist() if _kind(n)]
            for k, n in order(entries):
                with z.open(n) as raw:
                    yield k, os.path.basename(n), io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    else:
        k = _kind(path)
        if not k:
            raise ValueError(f"cannot tell whether {path!r} is a prices or stations CSV (name must contain 'prices' or 'stations')")
        with open(path, encoding="utf-8-sig", newline="") as fh:
            yield k, os.path.basename(path), fh


# ------------------------------------------------------------- exact logic ---

def secs_from_noon(ts_utc: datetime) -> int:
    local = ts_utc.astimezone(detect.BERLIN)
    noon = datetime.combine(local.date(), time(12, 0), tzinfo=detect.BERLIN)
    return int((local - noon).total_seconds())


def in_noon_slot(secs: int, tolerance_seconds: int) -> bool:
    return 0 <= secs < tolerance_seconds


# ----------------------------------------------------------------- import ---

def import_path(
    conn: sqlite3.Connection,
    path: str,
    lat: float,
    lng: float,
    radius_km: float,
    progress=None,
) -> dict:
    """Import prices (and stations) from `path`, keeping only stations within the area.

    Stations must be known (via a stations CSV, an earlier import or live polling)
    to be inside the area; price rows of unknown stations are ignored.
    """
    totals = {"files": 0, "skipped_files": 0, "rows": 0, "rows_in_area": 0, "changes": 0, "stations": 0}
    area_ids: set[str] = {
        r[0]
        for r in conn.execute("SELECT id, lat, lng FROM stations")
        if haversine_km(lat, lng, r[1], r[2]) <= radius_km
    }

    for kind, name, fh in iter_sources(path):
        if kind == "stations":
            n = 0
            with transaction(conn):
                for s in read_station_rows(fh):
                    if haversine_km(lat, lng, s.lat, s.lng) > radius_km:
                        continue
                    now = detect.fmt_ts(datetime.now(timezone.utc))
                    conn.execute(
                        """INSERT INTO stations(id,name,brand,street,house_number,post_code,place,lat,lng,first_seen,last_seen)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET
                             name=excluded.name, brand=excluded.brand, street=excluded.street,
                             house_number=excluded.house_number, post_code=excluded.post_code,
                             place=excluded.place, lat=excluded.lat, lng=excluded.lng""",
                        (s.id, s.name, s.brand, s.street, s.house_number, s.post_code, s.place, s.lat, s.lng, now, now),
                    )
                    area_ids.add(s.id)
                    n += 1
            totals["stations"] += n
            if progress:
                progress(f"{name}: {n} Tankstellen im Gebiet")
            continue

        if conn.execute("SELECT 1 FROM imported_files WHERE name=?", (name,)).fetchone():
            totals["skipped_files"] += 1
            if progress:
                progress(f"{name}: bereits importiert, uebersprungen")
            continue

        rows_total = rows_area = changes = 0
        with transaction(conn):
            for row in read_price_rows(fh):
                rows_total += 1
                if row.station_id not in area_ids:
                    continue
                rows_area += 1
                changes += _apply_row(conn, row)
            conn.execute(
                "INSERT INTO imported_files(name, imported_at, rows_total, rows_in_area) VALUES (?,?,?,?)",
                (name, detect.fmt_ts(datetime.now(timezone.utc)), rows_total, rows_area),
            )
        totals["files"] += 1
        totals["rows"] += rows_total
        totals["rows_in_area"] += rows_area
        totals["changes"] += changes
        if progress:
            progress(f"{name}: {rows_total} Zeilen, {rows_area} im Gebiet, {changes} Aenderungen")
    return totals


def _apply_row(conn: sqlite3.Connection, row: PriceRow) -> int:
    n = 0
    ts = detect.fmt_ts(row.ts)
    for fuel in FUELS:
        price = row.prices[fuel]
        flag = row.change[fuel]
        state = conn.execute(
            "SELECT price, ts_utc FROM import_state WHERE station_id=? AND fuel=?",
            (row.station_id, fuel),
        ).fetchone()
        prev_price = state["price"] if state else None

        # Only a real change (flag 1) with a valid price on both sides is analysed.
        if flag == 1 and price is not None:
            day = detect.berlin_day(row.ts).isoformat()
            is_inc = prev_price is not None and price > prev_price + EPS
            # Whether an increase is the 1st/2nd of its day is decided at report
            # time (it depends on the configurable noon tolerance).
            cur = conn.execute(
                """INSERT OR IGNORE INTO exact_changes
                   (station_id,fuel,ts_utc,berlin_day,prev_price,price,is_increase,secs_from_noon)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row.station_id, fuel, ts, day, prev_price, price, int(is_inc), secs_from_noon(row.ts)),
            )
            n += cur.rowcount

        # Track last known price. Flag 2 (removed) clears it, so a later "new"
        # price is never compared to a stale one.
        if flag == 2 or (flag != 0 and price is None):
            conn.execute(
                "INSERT INTO import_state(station_id,fuel,price,ts_utc) VALUES (?,?,NULL,?) "
                "ON CONFLICT(station_id,fuel) DO UPDATE SET price=NULL, ts_utc=excluded.ts_utc",
                (row.station_id, fuel, ts),
            )
        elif price is not None and (flag in (1, 3) or state is None):
            conn.execute(
                "INSERT INTO import_state(station_id,fuel,price,ts_utc) VALUES (?,?,?,?) "
                "ON CONFLICT(station_id,fuel) DO UPDATE SET price=excluded.price, ts_utc=excluded.ts_utc",
                (row.station_id, fuel, price, ts),
            )
    return n
