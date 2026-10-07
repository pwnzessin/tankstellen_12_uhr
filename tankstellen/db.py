"""SQLite storage layer."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    brand        TEXT,
    street       TEXT,
    house_number TEXT,
    post_code    TEXT,
    place        TEXT,
    lat          REAL,
    lng          REAL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

-- One row per poll x station x fuel. price is NULL if the fuel is not offered.
CREATE TABLE IF NOT EXISTS price_observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL REFERENCES stations(id),
    fuel       TEXT NOT NULL CHECK (fuel IN ('diesel','e5','e10')),
    ts_utc     TEXT NOT NULL,          -- ISO-8601 UTC
    price      REAL,
    is_open    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_obs_station_fuel_ts
    ON price_observations(station_id, fuel, ts_utc);

-- Every detected price increase between two consecutive observations.
-- The real increase happened somewhere in (prev_ts_utc, ts_utc].
CREATE TABLE IF NOT EXISTS price_increases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id    TEXT NOT NULL REFERENCES stations(id),
    fuel          TEXT NOT NULL,
    prev_ts_utc   TEXT NOT NULL,
    ts_utc        TEXT NOT NULL,
    prev_price    REAL NOT NULL,
    price         REAL NOT NULL,
    -- classification, see detect.py
    verdict       TEXT NOT NULL CHECK (verdict IN ('violation','ok_noon','multiple_per_day')),
    berlin_day    TEXT NOT NULL,       -- calendar day of ts_utc in Europe/Berlin
    UNIQUE (station_id, fuel, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_inc_verdict ON price_increases(verdict, berlin_day);

-- Exact price changes from the Tankerkoenig historical CSV files.
-- One row per real price change (change flag 1 = changed) with the real timestamp.
-- prev_price is the last known price of that station+fuel (may be from an earlier day).
CREATE TABLE IF NOT EXISTS exact_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id  TEXT NOT NULL,
    fuel        TEXT NOT NULL CHECK (fuel IN ('diesel','e5','e10')),
    ts_utc      TEXT NOT NULL,
    berlin_day  TEXT NOT NULL,
    prev_price  REAL,
    price       REAL NOT NULL,
    is_increase INTEGER NOT NULL,
    -- seconds after 12:00:00 Berlin time on that day (negative = before noon)
    secs_from_noon INTEGER NOT NULL,
    UNIQUE (station_id, fuel, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_exact_inc ON exact_changes(is_increase, berlin_day);

-- Last known price per station+fuel from the import, so imports can be resumed
-- file by file in date order.
CREATE TABLE IF NOT EXISTS import_state (
    station_id TEXT NOT NULL,
    fuel       TEXT NOT NULL,
    price      REAL,
    ts_utc     TEXT NOT NULL,
    PRIMARY KEY (station_id, fuel)
);

CREATE TABLE IF NOT EXISTS imported_files (
    name        TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL,
    rows_total  INTEGER NOT NULL,
    rows_in_area INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT NOT NULL,
    lat        REAL NOT NULL,
    lng        REAL NOT NULL,
    radius_km  REAL NOT NULL,
    stations   INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
