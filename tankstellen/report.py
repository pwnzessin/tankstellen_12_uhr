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



def _classified_cte(tolerance_seconds: int, since: str | None) -> tuple[str, list]:
    """CTE `inc` with one row per price increase and its verdict.

    Every increase is judged on its own merits:
      * outside [12:00:00, 12:00:00 + tolerance) Berlin time -> violation ('outside')
      * inside the slot but not the first in-slot increase of that
        station+fuel+day                                       -> violation ('repeat')
      * otherwise                                              -> legal
    An early illegal increase therefore does not turn the legal noon increase
    of the same day into a second violation.
    """
    tol = int(tolerance_seconds)
    where = " AND berlin_day >= ?" if since else ""
    params = [since] if since else []
    cte = f"""
    WITH inc AS (
        SELECT c.*,
               (c.secs_from_noon >= 0 AND c.secs_from_noon < {tol}) AS in_slot,
               -- rank within the in-slot group (the partition separates in-slot
               -- from out-of-slot rows; rank is only used where in_slot is true)
               ROW_NUMBER() OVER (
                   PARTITION BY c.station_id, c.fuel, c.berlin_day,
                                (c.secs_from_noon >= 0 AND c.secs_from_noon < {tol})
                   ORDER BY c.ts_utc) AS slot_rank
        FROM exact_changes c
        WHERE c.is_increase = 1{where}
    ),
    v AS (
        SELECT inc.*,
               CASE WHEN NOT in_slot THEN 'outside'
                    WHEN slot_rank > 1 THEN 'repeat'
               END AS verdict
        FROM inc
    )"""
    return cte, params


def exact_summary(
    conn: sqlite3.Connection, since: str | None = None, tolerance_seconds: int = 300
) -> dict:
    """Exact analysis on historical data (see _classified_cte for the rules)."""
    where = " WHERE berlin_day >= ?" if since else ""
    params: list = [since] if since else []

    span = conn.execute(
        f"SELECT MIN(berlin_day), MAX(berlin_day), COUNT(DISTINCT berlin_day) FROM exact_changes{where}",
        params,
    ).fetchone()
    stations_total = conn.execute(
        f"SELECT COUNT(DISTINCT station_id) FROM exact_changes{where}", params
    ).fetchone()[0]
    changed = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT fuel, COUNT(DISTINCT station_id) FROM exact_changes{where} GROUP BY fuel", params
        )
    }

    cte, cparams = _classified_cte(tolerance_seconds, since)
    increases = {r[0]: r[1] for r in conn.execute(cte + " SELECT fuel, COUNT(*) FROM v GROUP BY fuel", cparams)}
    violations_ = {
        r[0]: r[1]
        for r in conn.execute(
            cte + " SELECT fuel, COUNT(*) FROM v WHERE verdict IS NOT NULL GROUP BY fuel", cparams
        )
    }
    by_kind = {
        r[0]: r[1]
        for r in conn.execute(
            cte + " SELECT verdict, COUNT(*) FROM v WHERE verdict IS NOT NULL GROUP BY verdict", cparams
        )
    }
    violating_stations = {
        r[0]: r[1]
        for r in conn.execute(
            cte + " SELECT fuel, COUNT(DISTINCT station_id) FROM v WHERE verdict IS NOT NULL GROUP BY fuel",
            cparams,
        )
    }
    violating_any = conn.execute(
        cte + " SELECT COUNT(DISTINCT station_id) FROM v WHERE verdict IS NOT NULL", cparams
    ).fetchone()[0]
    return {
        "first_day": span[0],
        "last_day": span[1],
        "days": span[2],
        "stations_with_changes": stations_total,
        "stations_changed_by_fuel": changed,
        "increases_by_fuel": increases,
        "violations_by_fuel": violations_,
        "violations_by_kind": by_kind,
        "violating_stations_by_fuel": violating_stations,
        "violating_stations_any_fuel": violating_any,
        "tolerance_seconds": tolerance_seconds,
    }


def exact_top_offenders(
    conn: sqlite3.Connection, since: str | None = None, tolerance_seconds: int = 300, limit: int = 20
):
    cte, params = _classified_cte(tolerance_seconds, since)
    return conn.execute(
        cte
        + f"""
        SELECT s.name, s.brand, s.street, s.house_number, s.post_code, s.place,
               SUM(v.verdict IS NOT NULL) AS n,
               COUNT(*) AS inc_total,
               SUM(v.fuel='diesel' AND v.verdict IS NOT NULL) AS diesel_n,
               SUM(v.fuel IN ('e5','e10') AND v.verdict IS NOT NULL) AS benzin_n
        FROM v JOIN stations s ON s.id = v.station_id
        GROUP BY s.id HAVING n > 0 ORDER BY n DESC, inc_total DESC LIMIT {int(limit)}""",
        params,
    ).fetchall()


def exact_recent_violations(
    conn: sqlite3.Connection, since: str | None = None, tolerance_seconds: int = 300, limit: int = 20
):
    cte, params = _classified_cte(tolerance_seconds, since)
    return conn.execute(
        cte
        + f"""
        SELECT s.name, s.brand, s.place, v.fuel, v.ts_utc, v.prev_price, v.price,
               v.secs_from_noon, v.verdict
        FROM v JOIN stations s ON s.id = v.station_id
        WHERE v.verdict IS NOT NULL
        ORDER BY v.ts_utc DESC LIMIT {int(limit)}""",
        params,
    ).fetchall()


def format_exact_report(
    conn: sqlite3.Connection, since: str | None = None, tolerance_seconds: int = 300, limit: int = 20
) -> str:
    s = exact_summary(conn, since, tolerance_seconds)
    if not s["days"]:
        return ""
    out = []
    out.append("=== Exakte Auswertung (historische Tankerkoenig-Daten) ===")
    out.append(f"Zeitraum: {s['first_day']} .. {s['last_day']} ({s['days']} Tage mit Aenderungen)")
    out.append(
        f"Toleranz: Erhoehung zaehlt als 'um 12 Uhr', wenn sie in [12:00:00, 12:00:00 + {tolerance_seconds}s) liegt"
    )
    out.append("")
    out.append("Tankstellen mit mind. einem Verstoss:")
    for fuel in ("diesel", "e5", "e10"):
        v = s["violating_stations_by_fuel"].get(fuel, 0)
        o = s["stations_changed_by_fuel"].get(fuel, 0)
        inc = s["increases_by_fuel"].get(fuel, 0)
        viol = s["violations_by_fuel"].get(fuel, 0)
        pct = f"{100 * v / o:.1f}%" if o else "n/a"
        out.append(
            f"  {fuel:7s}: {v:4d} von {o:4d} Tankstellen ({pct}); {viol} von {inc} Erhoehungen regelwidrig"
        )
    out.append(
        f"  gesamt (beliebiger Kraftstoff): {s['violating_stations_any_fuel']} von {s['stations_with_changes']}"
    )
    k = s["violations_by_kind"]
    out.append(
        f"  Verstoesse: {k.get('outside', 0)}x ausserhalb des 12-Uhr-Fensters, "
        f"{k.get('repeat', 0)}x weitere Erhoehung im 12-Uhr-Fenster am selben Tag"
    )
    rows = exact_top_offenders(conn, since, tolerance_seconds, limit)
    if rows:
        out.append("")
        out.append(f"Top-Verstoesse pro Tankstelle (max {limit}):")
        for r in rows:
            addr = f"{r['street']} {r['house_number']}, {r['post_code']} {r['place']}".strip()
            out.append(
                f"  {r['n']:4d} von {r['inc_total']:4d} Erhoehungen  {r['brand'] or r['name']} - {addr}"
                f"  [Diesel {r['diesel_n']}, Benzin {r['benzin_n']}]"
            )
        out.append("")
        out.append("Letzte Verstoesse (Berliner Zeit):")
        for r in exact_recent_violations(conn, since, tolerance_seconds, limit):
            when = detect.parse_ts(r["ts_utc"]).astimezone(detect.BERLIN).strftime("%Y-%m-%d %H:%M:%S")
            if r["verdict"] == "outside":
                mins = abs(r["secs_from_noon"]) / 60
                rel = "nach" if r["secs_from_noon"] >= 0 else "vor"
                why = f"{mins:.0f} min {rel} 12:00"
            else:
                why = "weitere Erhoehung im 12-Uhr-Fenster"
            out.append(
                f"  {when}  {r['fuel']:6s} {r['prev_price']:.3f} -> {r['price']:.3f}  ({why})  "
                f"{r['brand'] or r['name']}, {r['place']}"
            )
    return "\n".join(out)


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
