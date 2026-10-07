import unittest
from datetime import datetime, timedelta, timezone

from tankstellen import api, collector, db, detect, report

BERLIN = detect.BERLIN


def berlin(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=BERLIN)


def station(sid="s1", diesel=1.80, e5=1.90, e10=1.85):
    return api.StationPrices(
        id=sid, name="Test", brand="TEST", street="Str", house_number="1",
        post_code="10000", place="Berlin", lat=52.5, lng=13.4, is_open=True,
        prices={"diesel": diesel, "e5": e5, "e10": e10},
    )


class NoonWindowTests(unittest.TestCase):
    def test_window_contains_noon(self):
        w = detect.noons_in_window(berlin(2026, 5, 4, 11, 55), berlin(2026, 5, 4, 12, 3))
        self.assertEqual(len(w), 1)

    def test_window_without_noon(self):
        self.assertEqual(
            detect.noons_in_window(berlin(2026, 5, 4, 12, 1), berlin(2026, 5, 4, 12, 6)), []
        )

    def test_noon_boundary_start_exclusive_end_inclusive(self):
        # observation exactly at 12:00:00 sees the increase made at 12:00:00
        self.assertEqual(
            len(detect.noons_in_window(berlin(2026, 5, 4, 11, 55), berlin(2026, 5, 4, 12, 0))), 1
        )
        # previous observation exactly at 12:00 already saw the old/new price
        self.assertEqual(
            detect.noons_in_window(berlin(2026, 5, 4, 12, 0), berlin(2026, 5, 4, 12, 5)), []
        )

    def test_dst_day_noon_is_local(self):
        # 2026-03-29 is the spring DST change (CET -> CEST). 12:00 local = 10:00 UTC.
        noon_utc = datetime(2026, 3, 29, 10, 0, tzinfo=timezone.utc)
        w = detect.noons_in_window(noon_utc - timedelta(minutes=3), noon_utc + timedelta(minutes=2))
        self.assertEqual(len(w), 1)
        # 11:00 UTC would be 13:00 local, so no noon in there
        self.assertEqual(
            detect.noons_in_window(noon_utc + timedelta(hours=1), noon_utc + timedelta(hours=1, minutes=5)),
            [],
        )


class ClassifyTests(unittest.TestCase):
    def test_violation_outside_noon(self):
        self.assertEqual(
            detect.classify_increase(berlin(2026, 5, 4, 8, 0), berlin(2026, 5, 4, 8, 5), 0),
            detect.VIOLATION,
        )

    def test_ok_at_noon(self):
        self.assertEqual(
            detect.classify_increase(berlin(2026, 5, 4, 11, 57), berlin(2026, 5, 4, 12, 2), 0),
            detect.OK_NOON,
        )

    def test_second_increase_same_day_is_multiple(self):
        self.assertEqual(
            detect.classify_increase(berlin(2026, 5, 4, 11, 57), berlin(2026, 5, 4, 12, 2), 1),
            detect.MULTIPLE,
        )

    def test_find_increase(self):
        self.assertTrue(detect.find_increase(1.800, 1.801))
        self.assertFalse(detect.find_increase(1.800, 1.800))
        self.assertFalse(detect.find_increase(1.800, 1.700))
        self.assertFalse(detect.find_increase(None, 1.8))
        self.assertFalse(detect.find_increase(1.8, None))


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def ingest(self, st, when):
        return collector.ingest(self.conn, [st], when.astimezone(timezone.utc), 52.5, 13.4, 5.0)

    def test_increase_outside_noon_is_violation_and_decrease_ignored(self):
        self.ingest(station(diesel=1.80), berlin(2026, 5, 4, 9, 0))
        c = self.ingest(station(diesel=1.85), berlin(2026, 5, 4, 9, 5))
        self.assertEqual(c["violations"], 1)
        c = self.ingest(station(diesel=1.70), berlin(2026, 5, 4, 9, 10))  # decrease: fine
        self.assertEqual(c["increases"], 0)
        s = report.summary(self.conn)
        self.assertEqual(s["violating_stations_by_fuel"], {"diesel": 1})
        self.assertEqual(s["violating_stations_any_fuel"], 1)

    def test_increase_at_noon_is_ok(self):
        self.ingest(station(diesel=1.80), berlin(2026, 5, 4, 11, 57))
        c = self.ingest(station(diesel=1.95), berlin(2026, 5, 4, 12, 2))
        self.assertEqual(c["increases"], 1)
        self.assertEqual(c["violations"], 0)
        self.assertEqual(report.summary(self.conn)["violating_stations_any_fuel"], 0)

    def test_two_increases_same_day_second_is_violation(self):
        self.ingest(station(diesel=1.80), berlin(2026, 5, 4, 11, 57))
        self.ingest(station(diesel=1.90), berlin(2026, 5, 4, 12, 2))  # ok
        # second increase, window contains no noon -> violation
        c = self.ingest(station(diesel=1.95), berlin(2026, 5, 4, 12, 7))
        self.assertEqual(c["violations"], 1)

    def test_large_gap_skipped(self):
        self.ingest(station(diesel=1.80), berlin(2026, 5, 4, 6, 0))
        c = self.ingest(station(diesel=1.90), berlin(2026, 5, 4, 9, 0))
        self.assertEqual(c["skipped_gap"], 1)
        self.assertEqual(c["increases"], 0)

    def test_each_fuel_tracked_separately(self):
        self.ingest(station(diesel=1.80, e5=1.90), berlin(2026, 5, 4, 9, 0))
        self.ingest(station(diesel=1.80, e5=1.95), berlin(2026, 5, 4, 9, 5))
        s = report.summary(self.conn)
        self.assertEqual(s["violating_stations_by_fuel"], {"e5": 1})

    def test_missing_fuel_price_is_not_an_increase(self):
        self.ingest(station(diesel=None), berlin(2026, 5, 4, 9, 0))
        c = self.ingest(station(diesel=1.80), berlin(2026, 5, 4, 9, 5))  # None -> price
        self.assertEqual(c["increases"], 0)

    def test_report_runs(self):
        self.ingest(station(diesel=1.80), berlin(2026, 5, 4, 9, 0))
        self.ingest(station(diesel=1.85), berlin(2026, 5, 4, 9, 5))
        text = report.format_report(self.conn)
        self.assertIn("12-Uhr-Regel", text)
        self.assertIn("diesel", text)


class ParseTests(unittest.TestCase):
    def test_parse_stations(self):
        payload = {
            "ok": True,
            "stations": [{
                "id": "x", "name": "N", "brand": "B", "street": "S", "houseNumber": "2",
                "postCode": 10407, "place": "P", "lat": 52.5, "lng": 13.4, "isOpen": True,
                "diesel": 1.109, "e5": False, "e10": None,
            }],
        }
        (s,) = api.parse_stations(payload)
        self.assertEqual(s.prices, {"diesel": 1.109, "e5": None, "e10": None})
        self.assertEqual(s.post_code, "10407")

    def test_api_error(self):
        with self.assertRaises(api.ApiError):
            api.parse_stations({"ok": False, "message": "parameter error"})


if __name__ == "__main__":
    unittest.main()
