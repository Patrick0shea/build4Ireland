import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

import db
import realtime_poller as poller


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = db.initialize(Path(self.temp.name) / 'transport.sqlite3')
        self.now = datetime(2026, 10, 4, 11, 30, tzinfo=timezone.utc).timestamp()
        with db.transaction(self.path) as con:
            con.execute("INSERT INTO agencies VALUES ('a','Dublin Bus',NULL,'Europe/Dublin',NULL,NULL)")
            con.execute("INSERT INTO routes(route_id,agency_id,route_type) VALUES ('001','a',3)")
            con.execute("INSERT INTO trips(trip_id,route_id,service_id) VALUES ('0001','001','daily')")
            con.execute("INSERT INTO stops(stop_id,stop_name) VALUES ('0002','Demo')")
            con.execute("INSERT INTO stop_times(trip_id,stop_sequence,stop_id,arrival_secs,departure_secs) VALUES ('0001',1,'0002',43200,43200),('0001',2,'0002',46800,46800)")
            con.execute("INSERT INTO calendar VALUES ('daily',1,1,1,1,1,1,1,'20260101','20261231')")

    def payload(self, entities=(), timestamp=None):
        return json.dumps({'header': {'timestamp': int(self.now if timestamp is None else timestamp)},
                           'entity': list(entities)}).encode()

    def rows(self, table):
        with db.transaction(self.path) as con:
            return con.execute('SELECT * FROM ' + table).fetchall()

    def ingest(self, entities=()):
        result = poller.ingest(self.path, self.payload(entities), self.now)
        self.now += 60
        return result

    def test_missing_threshold_and_failure_break(self):
        for _ in range(4):
            self.ingest()
        self.assertFalse(poller.suspected_missing(self.path, '0001'))
        self.ingest()
        self.assertTrue(poller.suspected_missing(self.path, '0001'))
        def fail(*args):
            raise TimeoutError('secret must never be saved')
        poller.poll_once(self.path, 'url', 'secret', fetcher=fail, now=lambda:self.now)
        self.assertFalse(poller.suspected_missing(self.path, '0001'))
        self.assertEqual(len(self.rows('trip_status_history')), 5)
        self.assertEqual(self.rows('feed_snapshots')[-1]['error_message'], 'TimeoutError')
        for _ in range(4):
            self.ingest()
        self.assertFalse(poller.suspected_missing(self.path, '0001'))
        self.ingest()
        self.assertTrue(poller.suspected_missing(self.path, '0001'))

    def test_updates_unmatched_cancellation_and_raw_stops(self):
        update = {'id':'u','tripUpdate':{'trip':{'tripId':'0001'},'stopTimeUpdate':[
            {'stopId':'0002','arrival':{'time':int(self.now)}}, {'stopId':'next'}]}}
        self.ingest([update, {'id':'unmatched','trip_update':{'trip':{'trip_id':'unknown'}}}])
        self.assertEqual(self.rows('trip_status_history')[-1]['status'], 'seen')
        rows = self.rows('trip_updates')
        self.assertEqual(rows[0]['trip_id'], '0001')
        self.assertIsNone(rows[0]['delay_seconds'])
        self.assertEqual(len(json.loads(rows[0]['raw_entity'])['trip_update']['stop_time_update']), 2)
        self.assertEqual(rows[1]['trip_id'], 'unknown')
        self.ingest([{'id':'c','trip_update':{'trip':{'trip_id':'0001','schedule_relationship':'CANCELED'}}}])
        self.assertEqual(self.rows('trip_status_history')[-1]['status'], 'cancelled')

    def test_vehicle_and_missing_optional_fields(self):
        self.ingest([{'id':'v','vehicle':{'trip':{'trip_id':'0001'},'position':{'latitude':53.3,'longitude':-6.2}}}])
        vehicle = self.rows('vehicle_positions')[0]
        self.assertEqual(vehicle['vehicle_id'], 'entity:v')
        self.assertIsNone(vehicle['speed_mps'])
        self.assertEqual(self.rows('trip_status_history')[0]['status'], 'seen')

    def test_bad_stale_duplicate_and_differential_feeds(self):
        self.ingest()
        for payload in (b'bad', self.payload(timestamp=self.now-500), self.payload(timestamp=self.now-60),
                        b'{"header":{"timestamp":1,"incrementality":"DIFFERENTIAL"}}'):
            poller.poll_once(self.path, 'url','key', fetcher=lambda *args:payload, now=lambda:self.now)
        self.assertEqual(len(self.rows('trip_status_history')), 1)
        self.assertEqual([r['fetch_status'] for r in self.rows('feed_snapshots')], ['success'] + ['error']*4)

    def test_transaction_rolls_back_invalid_entity(self):
        payload = self.payload([{'id':'v1','vehicle':{'vehicle':{'id':'same'}}},
                                {'id':'v2','vehicle':{'vehicle':{'id':'same'}}}])
        poller.poll_once(self.path, 'url','key', fetcher=lambda *args:payload, now=lambda:self.now)
        self.assertEqual(len(self.rows('feed_snapshots')), 1)
        self.assertEqual(self.rows('feed_snapshots')[0]['fetch_status'], 'error')
        self.assertEqual(self.rows('vehicle_positions'), [])
        self.assertEqual(self.rows('trip_status_history'), [])

    def test_calendar_exception_and_route_scope(self):
        with db.transaction(self.path) as con:
            self.assertIn('0001', poller.scheduled_trips(con, self.now))
            self.assertEqual(poller.scheduled_trips(con, self.now, ['other']), {})
            con.execute("INSERT INTO calendar_dates VALUES ('daily','20261004',2)")
            self.assertEqual(poller.scheduled_trips(con, self.now), {})
            con.execute("UPDATE calendar_dates SET exception_type=1")
            self.assertIn('0001', poller.scheduled_trips(con, self.now))

    def test_previous_day_after_midnight_and_wrong_instance(self):
        self.now = datetime(2026,10,5,0,30,tzinfo=timezone.utc).timestamp()
        with db.transaction(self.path) as con:
            con.execute('UPDATE stop_times SET arrival_secs=arrival_secs+46800,departure_secs=departure_secs+46800')
            self.assertEqual(poller.scheduled_trips(con, self.now)['0001'][0], '20261004')
        self.ingest([{'id':'u','trip_update':{'trip':{'trip_id':'0001','start_date':'20261005'}}}])
        self.assertEqual(self.rows('trip_status_history')[0]['status'], 'missing')

    def test_protobuf_roundtrip(self):
        try:
            from google.transit import gtfs_realtime_pb2
        except ImportError:
            self.skipTest('Optional protobuf decoder is not installed')
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = '2.0'
        feed.header.timestamp = int(self.now)
        entity = feed.entity.add()
        entity.id = 'proto'
        entity.trip_update.trip.trip_id = '0001'
        stop = entity.trip_update.stop_time_update.add()
        stop.stop_id = '0002'
        stop.arrival.time = int(self.now + 60)
        poller.ingest(self.path, feed.SerializeToString(), self.now)
        self.assertEqual(self.rows('trip_status_history')[0]['status'], 'seen')
        self.assertIsNone(self.rows('trip_updates')[0]['delay_seconds'])

    def test_long_gap_and_new_service_day_break_streak(self):
        for _ in range(4):
            self.ingest()
        self.now += 180
        self.ingest()
        self.assertFalse(poller.suspected_missing(self.path, '0001'))
        self.now += 86400
        self.ingest()
        self.assertFalse(poller.suspected_missing(self.path, '0001'))

    def test_dst_service_origin(self):
        # On the autumn transition day, GTFS noon - 12h is 00:00 UTC.
        self.now = datetime(2026,10,25,12,30,tzinfo=timezone.utc).timestamp()
        with db.transaction(self.path) as con:
            instance = poller.scheduled_trips(con, self.now)['0001']
        self.assertEqual(instance[1], datetime(2026,10,25,12,tzinfo=timezone.utc).timestamp())

    def test_invalid_configuration_reports_safe_actionable_errors(self):
        for url, key, header, expected in (
            ('PASTE_THE_FULL_REQUEST_URL_HERE','secret','x-api-key','NTA_REALTIME_URL'),
            ('https://example.com/feed','secret\n','x-api-key','NTA_API_KEY'),
            ('https://example.com/feed','secret','bad header','NTA_API_KEY_HEADER'),
        ):
            poller.poll_once(self.path, url,key,header,now=lambda:self.now)
            error = self.rows('feed_snapshots')[-1]['error_message']
            self.assertIn(expected, error)
            self.assertNotIn('secret', error)
        self.assertEqual(self.rows('trip_status_history'), [])
        poller.validate_configuration('https://api.nationaltransport.ie/gtfsr/v2/gtfsr?format=json', 'secret','x-api-key')

    def test_two_sources_store_one_snapshot_and_vehicle_evidence(self):
        payloads = {'trips': self.payload(), 'vehicles': self.payload([
            {'id': 'v', 'vehicle': {'trip': {'trip_id': '0001'},
             'vehicle': {'id': 'bus1'}, 'position': {'latitude': 53.3, 'longitude': -6.2}}}])}
        calls = []
        def fetch(url, *args):
            calls.append(url)
            return payloads[url]
        poller.poll_once(self.path, 'trips', 'secret', vehicle_url='vehicles', sleep=lambda _: None,
                         fetcher=fetch, now=lambda: self.now)
        self.assertEqual(calls, ['trips', 'vehicles'])
        self.assertEqual(len(self.rows('feed_snapshots')), 1)
        self.assertEqual(self.rows('feed_snapshots')[0]['fetch_status'], 'success')
        self.assertEqual(self.rows('vehicle_positions')[0]['latitude'], 53.3)
        self.assertEqual(self.rows('trip_status_history')[0]['status'], 'seen')
        self.assertEqual(set(poller.source_timestamps(self.rows('feed_snapshots')[0]['raw_feed'])), {'trips', 'vehicles'})

    def test_vehicle_failure_never_counts_as_missing(self):
        def fetch(url, *args):
            if url == 'vehicles':
                raise TimeoutError()
            return self.payload()
        poller.poll_once(self.path, 'trips', 'secret', vehicle_url='vehicles', sleep=lambda _: None,
                         fetcher=fetch, now=lambda: self.now)
        self.assertEqual(self.rows('feed_snapshots')[0]['fetch_status'], 'error')
        self.assertEqual(self.rows('trip_status_history'), [])

    def test_repeated_vehicle_feed_rejected_despite_new_trip_feed(self):
        vehicle_payload = self.payload()
        for _ in range(2):
            poller.poll_once(self.path, 'trips', 'secret', vehicle_url='vehicles', sleep=lambda _: None,
                fetcher=lambda url, *args: self.payload() if url == 'trips' else vehicle_payload,
                now=lambda: self.now)
            self.now += 60
        self.assertEqual([r['fetch_status'] for r in self.rows('feed_snapshots')], ['success','error'])
        self.assertEqual(len(self.rows('trip_status_history')), 1)

    def test_requests_are_spaced_inside_cycle(self):
        events = []
        def fetch(url, *args):
            events.append(url)
            return self.payload()
        poller.poll_once(self.path, 'trips', 'secret', vehicle_url='vehicles',
                         fetcher=fetch, now=lambda: self.now,
                         sleep=lambda seconds: events.append(seconds))
        self.assertEqual(events, ['trips', 61, 'vehicles'])

    def test_retry_after(self):
        def fetch(*args):
            raise HTTPError('url',429,'rate limited',{'Retry-After':'180'},None)
        _, retry = poller.poll_once(self.path, 'url','key',fetcher=fetch,now=lambda:self.now)
        self.assertEqual(retry,180)
        self.assertEqual(self.rows('trip_status_history'), [])


if __name__ == '__main__':
    unittest.main()
