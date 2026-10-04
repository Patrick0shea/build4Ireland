"""Integration checks across the actual loader, poller and MCP handoff helpers."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone

import db
import gtfs_loader
from data_health import report
from offline_demo import capture, replay
from realtime_poller import ingest, poll_once, scheduled_trips, suspected_missing


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'transport.sqlite3'
        self.archive = self.root / 'static.zip'
        files = {
            'agency.txt': 'agency_id,agency_name,agency_url,agency_timezone\na,Dublin Bus,https://example.com,Europe/Dublin\n',
            'routes.txt': 'route_id,agency_id,route_short_name,route_type\n001,a,46A,3\n',
            'stops.txt': 'stop_id,stop_name,stop_lat,stop_lon\n0002,Example Stop,53.3,-6.2\n',
            'trips.txt': 'route_id,service_id,trip_id\n001,daily,0001\n',
            'stop_times.txt': 'trip_id,arrival_time,departure_time,stop_id,stop_sequence\n0001,12:00:00,12:00:00,0002,1\n0001,14:00:00,14:00:00,0002,2\n',
            'calendar.txt': 'service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\ndaily,1,1,1,1,1,1,1,20261001,20261031\n',
            'calendar_dates.txt': 'service_id,date,exception_type\ndaily,20261005,2\n',
        }
        with zipfile.ZipFile(self.archive,'w') as archive:
            for name, content in files.items():
                archive.writestr(name,content)
        gtfs_loader.load_gtfs(self.archive,database_path=self.database)
        self.now = datetime(2026,10,4,11,30,tzinfo=timezone.utc).timestamp()

    def payload(self, kind=None):
        entities = []
        if kind:
            entities = [{'id':'update','trip_update':{'trip':{'trip_id':'0001','schedule_relationship':kind}}}]
        return json.dumps({'header':{'timestamp':int(self.now)},'entity':entities}).encode()

    def test_loader_calendar_dates_are_compatible_and_refresh_preserves_history(self):
        with db.transaction(self.database) as con:
            self.assertEqual(con.execute('SELECT start_date FROM calendar').fetchone()[0],'2026-10-01')
            self.assertIn('0001',scheduled_trips(con,self.now))
            self.assertNotIn('0001',scheduled_trips(con,self.now+86400))
        ingest(self.database,self.payload('SCHEDULED'),self.now)
        gtfs_loader.load_gtfs(self.archive,database_path=self.database)
        with db.transaction(self.database) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM trips').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT status FROM trip_status_history').fetchone()[0],'seen')

    def test_health_matching_and_error_visibility(self):
        ingest(self.database,self.payload('SCHEDULED'),self.now)
        before = report(self.database,self.now)
        self.assertEqual(before['latest_success']['matching']['match_percent_of_nonempty_ids'],100)
        self.assertEqual(before['latest_success']['statuses'],{'seen':1})
        def fail(*args):
            raise TimeoutError()
        poll_once(self.database,'url','key',fetcher=fail,now=lambda:self.now+60)
        after = report(self.database,self.now+200)
        self.assertEqual(after['recent_polls'][0]['fetch_status'],'error')
        self.assertTrue(any('stale' in w for w in after['warnings']))
        self.assertEqual(after['latest_success']['statuses'],{'seen':1})

    def test_seen_missing_suspected_error_and_cancellation_after_import(self):
        ingest(self.database,self.payload('SCHEDULED'),self.now)
        for _ in range(5):
            self.now += 122
            ingest(self.database,self.payload(),self.now)
        self.assertTrue(suspected_missing(self.database,'0001'))
        def fail(*args):
            raise TimeoutError()
        poll_once(self.database,'url','key',fetcher=fail,now=lambda:self.now+60)
        self.assertFalse(suspected_missing(self.database,'0001'))
        self.now += 122
        ingest(self.database,self.payload('CANCELED'),self.now)
        self.assertEqual(report(self.database,self.now)['latest_success']['statuses'],{'cancelled':1})
        self.assertFalse(suspected_missing(self.database,'0001'))
        with db.transaction(self.database) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM trip_status_history h JOIN feed_snapshots f USING(snapshot_id) WHERE f.fetch_status='error'").fetchone()[0],0)

    def test_capture_and_replay_are_explicit_and_cannot_overwrite(self):
        ingest(self.database,self.payload('SCHEDULED'),self.now)
        fixture = self.root/'fixture.json'
        capture(self.database,fixture)
        dest = self.root/'demo.sqlite3'
        replay(fixture,dest)
        health = report(dest,self.now)
        self.assertEqual(health['mode'],'historical_replay')
        self.assertEqual(health['latest_success']['statuses'],{'seen':1})
        with self.assertRaises(FileExistsError):
            replay(fixture,dest)
        with self.assertRaises(FileExistsError):
            replay(fixture,self.database)
        self.assertEqual(report(self.database,self.now)['mode'],'live')

    def test_committed_recorded_fixture_and_handoff_queries(self):
        root = Path(__file__).resolve().parents[1]
        fixture = root/'fixtures/dublin_replay.json'
        destination = self.root/'recorded.sqlite3'
        replay(fixture,destination)
        content = json.loads(fixture.read_text())
        health = report(destination,content['feed']['header']['timestamp'])
        self.assertEqual(health['latest_success']['matching']['match_percent_of_nonempty_ids'],100)
        self.assertEqual(health['latest_success']['vehicle_position_count'],5)
        self.assertEqual(health['latest_success']['statuses'],{'seen':5})
        with db.transaction(destination) as con:
            con.executescript((root/'docs/handoff_queries.sql').read_text())
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_health_does_not_create_missing_database(self):
        import sqlite3
        absent = self.root/'absent.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            report(absent)
        self.assertFalse(absent.exists())


if __name__ == '__main__':
    unittest.main()
