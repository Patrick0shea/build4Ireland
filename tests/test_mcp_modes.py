from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import db
from mcp_tools import find_stops, get_departures


class MCPModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "transport.sqlite3"
        db.initialize(self.database)
        self.env = patch.dict(os.environ, {"TRANSPORT_DB_PATH": str(self.database)})
        self.env.start()

        local_now = datetime.now(ZoneInfo("Europe/Dublin"))
        departure_secs = (local_now.hour * 3600 + local_now.minute * 60 + local_now.second + 1200) % 86400
        with db.transaction(self.database) as con:
            con.execute("INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES ('S1', 'Central Interchange', 53.35, -6.26)")
            con.executemany("INSERT INTO agencies (agency_id, agency_name) VALUES (?, ?)", [
                ("bus-ie", "Bus Éireann"), ("rail-ie", "Iarnród Éireann / Irish Rail"), ("luas-ie", "LUAS")
            ])
            con.executemany("INSERT INTO routes (route_id, agency_id, route_short_name, route_type) VALUES (?, ?, ?, ?)", [
                ("bus-route", "bus-ie", "X1", 3), ("rail-route", "rail-ie", "IC", 2), ("luas-route", "luas-ie", "Red", 0)
            ])
            weekdays = (1, 1, 1, 1, 1, 1, 1, "2020-01-01", "2030-12-31")
            con.executemany("INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                ("bus-service", *weekdays), ("rail-service", *weekdays), ("luas-service", *weekdays)
            ])
            con.executemany("INSERT INTO trips (trip_id, route_id, service_id, trip_headsign) VALUES (?, ?, ?, ?)", [
                ("bus-trip", "bus-route", "bus-service", "Bus destination"),
                ("rail-trip", "rail-route", "rail-service", "Rail destination"),
                ("luas-trip", "luas-route", "luas-service", "Luas destination"),
            ])
            con.executemany("INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_secs, departure_secs) VALUES (?, 1, 'S1', ?, ?)", [
                (trip, departure_secs, departure_secs) for trip in ("bus-trip", "rail-trip", "luas-trip")
            ])

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def test_stop_search_and_departures_cover_bus_rail_and_luas(self):
        all_modes = find_stops("Central Interchange")
        self.assertEqual({service["mode"] for service in all_modes["stops"][0]["served_by"]}, {"bus", "rail", "tram"})
        rail_stops = find_stops("Central Interchange", mode="Irish Rail")
        self.assertEqual([service["mode"] for service in rail_stops["stops"][0]["served_by"]], ["rail"])

        for mode, expected_trip, operator in (
            ("bus", "bus-trip", "Bus Éireann"),
            ("rail", "rail-trip", "Iarnród Éireann / Irish Rail"),
            ("Luas", "luas-trip", "LUAS"),
        ):
            result = get_departures("S1", mode=mode)
            self.assertEqual([item["trip_id"] for item in result["departures"]], [expected_trip])
            self.assertEqual(result["departures"][0]["operator"], operator)

    def test_operator_filter_is_applied_within_bus_mode(self):
        result = get_departures("S1", mode="bus", operator="bus éireann")
        self.assertEqual([item["trip_id"] for item in result["departures"]], ["bus-trip"])
        self.assertEqual(get_departures("S1", mode="ferry")["departures"], [])


if __name__ == "__main__":
    unittest.main()
