import io
import os
import tempfile
import unittest
import zipfile
from datetime import timezone

from tankstellen import db, history, report

# Berlin Alexanderplatz area
LAT, LNG = 52.521, 13.438

STATIONS_CSV = """uuid,name,brand,street,house_number,post_code,city,latitude,longitude
near-1,Near One,ARAL,Str,1,10407,Berlin,52.53,13.44
near-2,Near Two,SHELL,Weg,2,10405,Berlin,52.52,13.43
far-1,Far Away,JET,Allee,3,80331,Muenchen,48.137,11.575
"""

HEADER = "date,station_uuid,diesel,e5,e10,dieselchange,e5change,e10change\n"


def prices_csv(*rows):
    return HEADER + "\n".join(rows) + "\n"


class ParsingTests(unittest.TestCase):
    def test_timestamp_variants(self):
        a = history.parse_csv_ts("2026-04-01 12:00:03+02")
        b = history.parse_csv_ts("2026-04-01 12:00:03+02:00")
        c = history.parse_csv_ts("2026-04-01T10:00:03Z")
        d = history.parse_csv_ts("2026-04-01 12:00:03")  # naive -> Berlin
        e = history.parse_csv_ts("2026-04-01 12:00:03.250+0200")
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a, d)
        self.assertEqual(a, e.replace(microsecond=0))
        self.assertEqual(a.tzinfo, timezone.utc)
        with self.assertRaises(ValueError):
            history.parse_csv_ts("garbage")

    def test_naive_winter_time(self):
        # January: Berlin = UTC+1
        self.assertEqual(history.parse_csv_ts("2026-01-10 12:00:00").hour, 11)

    def test_price_variants(self):
        self.assertEqual(history.parse_price("1.859"), 1.859)
        self.assertEqual(history.parse_price("1,859"), 1.859)
        self.assertEqual(history.parse_price("1859"), 1.859)  # tenth-of-cent integer
        self.assertIsNone(history.parse_price(""))
        self.assertIsNone(history.parse_price("-1"))
        self.assertIsNone(history.parse_price("0"))
        self.assertIsNone(history.parse_price("abc"))
        self.assertIsNone(history.parse_price(None))

    def test_column_order_and_bom(self):
        text = "\ufeffstation_uuid,date,e10,e5,diesel,e10change,e5change,dieselchange\n" \
               "x,2026-04-01 12:00:00+02,1.9,2.0,1.8,1,0,0\n"
        (row,) = list(history.read_price_rows(io.StringIO(text)))
        self.assertEqual(row.prices, {"diesel": 1.8, "e5": 2.0, "e10": 1.9})
        self.assertEqual(row.change, {"diesel": 0, "e5": 0, "e10": 1})

    def test_missing_columns(self):
        with self.assertRaises(ValueError):
            list(history.read_price_rows(io.StringIO("foo,bar\n1,2\n")))

    def test_bad_rows_skipped(self):
        text = HEADER + "not-a-date,x,1.8,1.9,1.9,1,1,1\n2026-04-01 12:00:00+02,x,1.8,1.9,1.9,1,1,1\n"
        self.assertEqual(len(list(history.read_price_rows(io.StringIO(text)))), 1)

    def test_haversine(self):
        self.assertAlmostEqual(history.haversine_km(52.5, 13.4, 52.5, 13.4), 0.0)
        # Berlin - Munich is ~504 km
        self.assertTrue(500 < history.haversine_km(52.52, 13.405, 48.137, 11.575) < 510)

    def test_secs_from_noon(self):
        ts = history.parse_csv_ts("2026-04-01 12:00:07+02")
        self.assertEqual(history.secs_from_noon(ts), 7)
        ts = history.parse_csv_ts("2026-04-01 11:59:00+02")
        self.assertEqual(history.secs_from_noon(ts), -60)
        # DST: 2026-03-29 CEST begins; 12:00 local is 10:00 UTC
        ts = history.parse_csv_ts("2026-03-29 10:00:00Z")
        self.assertEqual(history.secs_from_noon(ts), 0)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def write(self, name, text):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def do_import(self, path=None, radius=10):
        return history.import_path(self.conn, path or self.tmp.name, LAT, LNG, radius)

    def changes(self, **where):
        q = "SELECT * FROM exact_changes"
        if where:
            q += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        q += " ORDER BY ts_utc"
        return self.conn.execute(q, list(where.values())).fetchall()

    def test_area_filter_stations_and_prices(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.8,1.9,1.85,3,3,3",
            "2026-04-01 00:00:05+02,far-1,1.8,1.9,1.85,3,3,3",
        ))
        t = self.do_import()
        self.assertEqual(t["stations"], 2)
        self.assertEqual(t["rows"], 2)
        self.assertEqual(t["rows_in_area"], 1)
        ids = {r["id"] for r in self.conn.execute("SELECT id FROM stations")}
        self.assertEqual(ids, {"near-1", "near-2"})

    def test_increase_exact_classification(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 09:14:22+02,near-1,1.850,1.900,1.850,1,0,0",   # violation (09:14)
            "2026-04-01 10:00:00+02,near-1,1.700,1.900,1.850,1,0,0",   # decrease: fine
            "2026-04-01 12:00:07+02,near-1,1.900,1.900,1.850,1,0,0",   # legal: first at noon
            "2026-04-01 12:00:09+02,near-1,1.950,1.900,1.850,1,0,0",   # 2nd increase same day
            "2026-04-01 12:20:00+02,near-1,1.960,1.900,1.850,1,0,0",   # 3rd, and not noon
        ))
        self.do_import()
        diesel = self.changes(fuel="diesel")
        # the flag-3 "new" row only seeds the price state and is not stored
        self.assertEqual([c["is_increase"] for c in diesel], [1, 0, 1, 1, 1])
        self.assertEqual(diesel[0]["secs_from_noon"], -(2 * 3600 + 45 * 60 + 38))
        self.assertEqual(diesel[2]["secs_from_noon"], 7)

        s = report.exact_summary(self.conn, tolerance_seconds=300)
        # 09:14 outside, 12:00:07 legal (first in slot), 12:00:09 repeat in slot,
        # 12:20 outside -> 3 violations of 4 increases. The early illegal
        # increase does not turn the legal noon increase into a violation.
        self.assertEqual(s["violations_by_fuel"]["diesel"], 3)
        self.assertEqual(s["increases_by_fuel"]["diesel"], 4)
        self.assertEqual(s["violations_by_kind"], {"outside": 2, "repeat": 1})
        self.assertEqual(s["violating_stations_any_fuel"], 1)

    def test_legal_noon_increase_is_not_flagged(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 12:00:03+02,near-1,1.900,1.900,1.850,1,0,0",
        ))
        self.do_import()
        s = report.exact_summary(self.conn)
        self.assertEqual(s["violating_stations_any_fuel"], 0)
        self.assertEqual(s["increases_by_fuel"], {"diesel": 1})

    def test_tolerance_is_configurable(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 12:03:00+02,near-1,1.900,1.900,1.850,1,0,0",
        ))
        self.do_import()
        self.assertEqual(report.exact_summary(self.conn, tolerance_seconds=300)["violating_stations_any_fuel"], 0)
        self.assertEqual(report.exact_summary(self.conn, tolerance_seconds=60)["violating_stations_any_fuel"], 1)

    def test_just_before_noon_is_violation(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 11:59:58+02,near-1,1.900,1.900,1.850,1,0,0",
        ))
        self.do_import()
        self.assertEqual(report.exact_summary(self.conn)["violating_stations_any_fuel"], 1)

    def test_prev_price_carries_across_days_and_files(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 23:00:00+02,near-1,1.800,1.900,1.850,3,3,3",
        ))
        self.write("2026-04-02-prices.csv", prices_csv(
            "2026-04-02 06:00:00+02,near-1,1.850,1.900,1.850,1,0,0",
        ))
        self.do_import()
        (inc,) = [c for c in self.changes(fuel="diesel") if c["is_increase"]]
        self.assertEqual(inc["prev_price"], 1.8)
        self.assertEqual(inc["berlin_day"], "2026-04-02")

    def test_each_day_counts_separately(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 12:00:05+02,near-1,1.900,1.900,1.850,1,0,0",
        ))
        self.write("2026-04-02-prices.csv", prices_csv(
            "2026-04-02 12:00:05+02,near-1,2.000,1.900,1.850,1,0,0",
        ))
        self.do_import()
        s = report.exact_summary(self.conn)
        self.assertEqual(s["violating_stations_any_fuel"], 0)
        self.assertEqual(s["increases_by_fuel"]["diesel"], 2)

    def test_removed_fuel_resets_state(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 08:00:00+02,near-1,,1.900,1.850,2,0,0",         # diesel removed
            "2026-04-01 09:00:00+02,near-1,2.500,1.900,1.850,3,0,0",    # diesel new: not an increase
        ))
        self.do_import()
        self.assertEqual(report.exact_summary(self.conn)["violating_stations_any_fuel"], 0)

    def test_flag_zero_rows_are_ignored_for_changes(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            # only e5 changed; diesel/e10 repeated with flag 0 -> must not count
            "2026-04-01 09:00:00+02,near-1,1.800,1.950,1.850,0,1,0",
        ))
        self.do_import()
        self.assertEqual(report.exact_summary(self.conn)["violating_stations_by_fuel"], {"e5": 1})

    def test_reimport_is_idempotent(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 09:00:00+02,near-1,1.900,1.900,1.850,1,0,0",
        ))
        t1 = self.do_import()
        n1 = len(self.changes())
        t2 = self.do_import()
        self.assertEqual(t1["files"], 1)
        self.assertEqual(t2["files"], 0)
        self.assertEqual(t2["skipped_files"], 1)
        self.assertEqual(len(self.changes()), n1)

    def test_unknown_stations_ignored_without_station_file(self):
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
        ))
        t = self.do_import()
        self.assertEqual(t["rows_in_area"], 0)
        self.assertEqual(len(self.changes()), 0)

    def test_stations_from_poll_are_used(self):
        # stations already known (e.g. from live polling) need no stations CSV
        self.conn.execute(
            "INSERT INTO stations(id,name,lat,lng,first_seen,last_seen) VALUES ('near-1','N',52.53,13.44,'x','x')"
        )
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
        ))
        self.assertEqual(self.do_import()["rows_in_area"], 1)

    def test_files_processed_chronologically(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        # written in reverse order on purpose
        self.write("2026-04-02-prices.csv", prices_csv(
            "2026-04-02 06:00:00+02,near-1,1.850,1.900,1.850,1,0,0",
        ))
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 23:00:00+02,near-1,1.800,1.900,1.850,3,3,3",
        ))
        self.do_import()
        self.assertEqual(sum(c["is_increase"] for c in self.changes(fuel="diesel")), 1)

    def test_zip_import(self):
        zp = os.path.join(self.tmp.name, "data.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("stations/2026-04-01-stations.csv", STATIONS_CSV)
            z.writestr("prices/2026/04/2026-04-01-prices.csv", prices_csv(
                "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
                "2026-04-01 09:00:00+02,near-1,1.900,1.900,1.850,1,0,0",
            ))
        t = self.do_import(zp)
        self.assertEqual(t["files"], 1)
        self.assertEqual(report.exact_summary(self.conn)["violating_stations_any_fuel"], 1)

    def test_single_file_needs_recognisable_name(self):
        p = self.write("data.csv", "a,b\n")
        with self.assertRaises(ValueError):
            self.do_import(p)

    def test_since_filter(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 09:00:00+02,near-1,1.900,1.900,1.850,1,0,0",
        ))
        self.write("2026-04-05-prices.csv", prices_csv(
            "2026-04-05 12:00:01+02,near-1,2.000,1.900,1.850,1,0,0",
        ))
        self.do_import()
        self.assertEqual(report.exact_summary(self.conn)["violating_stations_any_fuel"], 1)
        self.assertEqual(report.exact_summary(self.conn, since="2026-04-02")["violating_stations_any_fuel"], 0)

    def test_report_text(self):
        self.write("2026-04-01-stations.csv", STATIONS_CSV)
        self.write("2026-04-01-prices.csv", prices_csv(
            "2026-04-01 00:00:05+02,near-1,1.800,1.900,1.850,3,3,3",
            "2026-04-01 09:14:22+02,near-1,1.850,1.900,1.850,1,0,0",
        ))
        self.do_import()
        text = report.format_exact_report(self.conn)
        self.assertIn("Exakte Auswertung", text)
        self.assertIn("ARAL", text)
        self.assertIn("09:14:22", text)

    def test_empty_exact_report(self):
        self.assertEqual(report.format_exact_report(self.conn), "")


if __name__ == "__main__":
    unittest.main()
