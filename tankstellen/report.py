"""Reporting on collected data."""
from __future__ import annotations

import sqlite3

from . import detect


def _since_clause(since: str | None) -> tuple[str, list]:
    if since:
        return " AND i.berlin_day >= ?", [since]
    return "", []


def summary(conn: sqlite3.Connection, since: str | None = None) -> dict:
    extra, params = _since_clause(since)

    n_stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    n_polls = conn.execute("SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM polls").fetchone()

    # Stations that were observed at all in the period (denominator).
    obs_where = ""
    obs_params: list = []
    if since:
        obs_where = " WHERE ts_utc >= ?"
        obs_params = [since]
    observed = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT fuel, COUNT(DISTINCT station_id) FROM price_observations{obs_where} GROUP BY fuel",
            obs_params,
        )
    }

    violators = {
        r["fuel"]: r["n"]
        for r in conn.execute(
            f"""SELECT i.fuel, COUNT(DISTINCT i.station_id) AS n
                FROM price_increases i
                WHERE i.verdict IN ('{detect.VIOLATION}','{detect.MULTIPLE}'){extra}
                GROUP BY i.fuel""",
            params,
        )
    }
    violators_any = conn.execute(
        f"""SELECT COUNT(DISTINCT i.station_id) FROM price_increases i
            WHERE i.verdict IN ('{detect.VIOLATION}','{detect.MULTIPLE}'){extra}""",
        params,
    ).fetchone()[0]

    return {
        "stations_known": n_stations,
        "polls": n_polls[0],
        "first_poll": n_polls[1],
        "last_poll": n_polls[2],
        "stations_observed_by_fuel": observed,
        "violating_stations_by_fuel": violators,
        "violating_stations_any_fuel": violators_any,
    }


def violations(conn: sqlite3.Connection, since: str | None = None, limit: int = 50):
    extra, params = _since_clause(since)
    return conn.execute(
        f"""SELECT s.name, s.brand, s.street, s.house_number, s.post_code, s.place,
                   i.fuel, i.prev_ts_utc, i.ts_utc, i.prev_price, i.price, i.verdict
            FROM price_increases i JOIN stations s ON s.id = i.station_id
            WHERE i.verdict IN ('{detect.VIOLATION}','{detect.MULTIPLE}'){extra}
            ORDER BY i.ts_utc DESC LIMIT ?""",
        params + [limit],
    ).fetchall()


def top_offenders(conn: sqlite3.Connection, since: str | None = None, limit: int = 20):
    extra, params = _since_clause(since)
    return conn.execute(
        f"""SELECT s.name, s.brand, s.street, s.house_number, s.post_code, s.place,
                   COUNT(*) AS n,
                   SUM(i.fuel='diesel') AS diesel_n,
                   SUM(i.fuel IN ('e5','e10')) AS benzin_n
            FROM price_increases i JOIN stations s ON s.id = i.station_id
            WHERE i.verdict IN ('{detect.VIOLATION}','{detect.MULTIPLE}'){extra}
            GROUP BY s.id ORDER BY n DESC LIMIT ?""",
        params + [limit],
    ).fetchall()


def _local(ts: str) -> str:
    return detect.parse_ts(ts).astimezone(detect.BERLIN).strftime("%Y-%m-%d %H:%M:%S")


def format_report(conn: sqlite3.Connection, since: str | None = None, limit: int = 20) -> str:
    s = summary(conn, since)
    out = []
    out.append("=== 12-Uhr-Regel Auswertung ===")
    if since:
        out.append(f"Zeitraum ab: {since}")
    out.append(f"Bekannte Tankstellen: {s['stations_known']}")
    out.append(f"Abfragen (Polls): {s['polls']}  ({s['first_poll']} .. {s['last_poll']} UTC)")
    out.append("")
    out.append("Tankstellen mit mind. einem Verstoss (Preiserhoehung nicht um 12:00 / mehrfach am Tag):")
    for fuel in ("diesel", "e5", "e10"):
        v = s["violating_stations_by_fuel"].get(fuel, 0)
        o = s["stations_observed_by_fuel"].get(fuel, 0)
        pct = f"{100 * v / o:.1f}%" if o else "n/a"
        out.append(f"  {fuel:7s}: {v:4d} von {o:4d} beobachteten  ({pct})")
    out.append(f"  gesamt (beliebiger Kraftstoff): {s['violating_stations_any_fuel']}")
    out.append("")
    rows = top_offenders(conn, since, limit)
    if rows:
        out.append(f"Top-Verstoesse pro Tankstelle (max {limit}):")
        for r in rows:
            addr = f"{r['street']} {r['house_number']}, {r['post_code']} {r['place']}".strip()
            out.append(
                f"  {r['n']:3d}x  {r['brand'] or r['name']} - {addr}"
                f"  [Diesel {r['diesel_n']}, Benzin {r['benzin_n']}]"
            )
        out.append("")
        out.append("Letzte Verstoesse (Berliner Zeit; Zeitfenster der Erhoehung):")
        for r in violations(conn, since, limit):
            out.append(
                f"  {r['fuel']:6s} {r['prev_price']:.3f} -> {r['price']:.3f}  "
                f"zwischen {_local(r['prev_ts_utc'])} und {_local(r['ts_utc'])}  "
                f"[{r['verdict']}]  {r['brand'] or r['name']}, {r['place']}"
            )
    else:
        out.append("Keine Verstoesse erkannt.")
    out.append("")
    out.append(
        "Hinweis: Erhoehungen werden nur zwischen zwei Abfragen erkannt. Ein Verstoss gilt nur als "
        "belegt, wenn das Zeitfenster zwischen den Abfragen kein 12:00 enthaelt (oder es die 2. "
        "Erhoehung am Tag ist)."
    )
    out.append("Datenquelle: Tankerkoenig.de / MTS-K, CC BY 4.0 (https://creativecommons.tankerkoenig.de)")
    return "\n".join(out)
