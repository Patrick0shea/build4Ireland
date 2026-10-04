"""NTA realtime ingestion using the shared db.py contract (no schema changes)."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import db

UTC = timezone.utc


class ConfigurationError(ValueError):
    """A fixed, credential-free explanation of invalid request settings."""


class FeedValidationError(ValueError):
    """A fixed, credential-free explanation of rejected feed evidence."""


def validate_configuration(url, key, header):
    try:
        parts = urlsplit(url)
        valid_url = (parts.scheme == 'https' and parts.hostname
                     and '.' in parts.hostname and parts.port != 0
                     and not parts.username and not parts.password
                     and not parts.fragment and not any(c.isspace() for c in url)
                     and not any(c in url for c in '<>')
                     and not any(word in url.upper() for word in ('PASTE_', 'COPY_THE_')))
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigurationError('NTA_REALTIME_URL must be the full HTTPS request URL from the NTA portal; replace the placeholder')
    if not key or not key.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key):
        raise ConfigurationError('NTA_API_KEY is empty or contains whitespace or invalid characters; copy the key again')
    import re
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header):
        raise ConfigurationError('NTA_API_KEY_HEADER is invalid; use x-api-key')


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def decode(payload):
    if payload.lstrip().startswith(b'{'):
        feed = json.loads(payload)
    else:
        from google.transit import gtfs_realtime_pb2
        from google.protobuf.json_format import MessageToDict
        message = gtfs_realtime_pb2.FeedMessage()
        message.ParseFromString(payload)
        if not message.IsInitialized():
            raise FeedValidationError('Incomplete protobuf feed')
        feed = MessageToDict(message, preserving_proto_field_name=True)
    # Accept both protobuf JSON naming conventions.
    def normalize(value):
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            import re
            return {re.sub(r'(?<!^)(?=[A-Z])', '_', k).lower(): normalize(v)
                    for k, v in value.items()}
        return value
    feed = normalize(feed)
    header = feed['header']
    if header.get('incrementality', 'FULL_DATASET') not in ('FULL_DATASET', 0):
        raise FeedValidationError('Differential feeds are not supported')
    timestamp = int(header['timestamp'])
    entities = feed.get('entity', [])
    ids = [e['id'] for e in entities]
    if len(ids) != len(set(ids)) or any(e.get('is_deleted') for e in entities):
        raise FeedValidationError('Duplicate or deleted entities in full feed')
    return timestamp, entities


def scheduled_trips(connection, timestamp, route_ids=()):
    """Return unambiguous active trip instances, respecting calendar and >24h times."""
    result = {}
    ambiguous = set()
    route_ids = tuple(dict.fromkeys(route_ids))
    calendars = {r['service_id']: dict(r) for r in connection.execute('SELECT * FROM calendar')}
    for cal in calendars.values():
        cal['start_date'] = cal['start_date'].replace('-', '')
        cal['end_date'] = cal['end_date'].replace('-', '')
    exceptions = {(r['service_id'], r['date'].replace('-', '')): r['exception_type']
                  for r in connection.execute('SELECT * FROM calendar_dates')}
    # Filter active service IDs before joining the large stop_times table.
    maximum = connection.execute('SELECT MAX(MAX(COALESCE(arrival_secs,0),COALESCE(departure_secs,0))) FROM stop_times').fetchone()[0] or 0
    zones = {r[0] or 'Europe/Dublin' for r in connection.execute('SELECT DISTINCT agency_timezone FROM agencies')} or {'Europe/Dublin'}
    active_services = set()
    weekdays = ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')
    for name in zones:
        today = datetime.fromtimestamp(timestamp, ZoneInfo(name)).date()
        for offset in range(maximum // 86400 + 2):
            day = today - timedelta(days=offset)
            date = day.strftime('%Y%m%d')
            for service_id, cal in calendars.items():
                if (cal['start_date'] <= date <= cal['end_date'] and cal[weekdays[day.weekday()]]
                        and exceptions.get((service_id,date)) != 2):
                    active_services.add(service_id)
            active_services.update(service_id for (service_id, exception_date), kind in exceptions.items()
                                   if exception_date == date and kind == 1)
    if not active_services:
        return {}
    conditions = "t.service_id IN (SELECT value FROM json_each(?))"
    parameters = [json.dumps(sorted(active_services))]
    if route_ids:
        conditions += " AND t.route_id IN (SELECT value FROM json_each(?))"
        parameters.append(json.dumps(route_ids))
    rows = connection.execute(f'''SELECT t.trip_id,t.service_id,t.route_id,a.agency_timezone,
               MIN(COALESCE(s.departure_secs,s.arrival_secs)) AS first,
               MAX(COALESCE(s.arrival_secs,s.departure_secs)) AS last
        FROM trips t JOIN routes r USING(route_id)
        LEFT JOIN agencies a USING(agency_id) JOIN stop_times s USING(trip_id)
        WHERE {conditions} GROUP BY t.trip_id''', parameters).fetchall()
    weekdays = ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')
    for row in rows:
        if row['first'] is None or row['last'] is None:
            continue
        zone = ZoneInfo(row['agency_timezone'] or 'Europe/Dublin')
        today = datetime.fromtimestamp(timestamp, zone).date()
        for offset in range(row['last'] // 86400 + 2):
            day = today - timedelta(days=offset)
            date = day.strftime('%Y%m%d')
            cal = calendars.get(row['service_id'])
            active = bool(cal and cal['start_date'] <= date <= cal['end_date']
                          and cal[weekdays[day.weekday()]])
            exception = exceptions.get((row['service_id'], date))
            if exception is not None:
                active = exception == 1
            # GTFS service-day origin is local noon minus twelve elapsed hours.
            origin = datetime(day.year, day.month, day.day, 12, tzinfo=zone).timestamp() - 43200
            start, end = origin + row['first'], origin + row['last']
            if active and start <= timestamp <= end:
                if row['trip_id'] in result:
                    ambiguous.add(row['trip_id'])
                result[row['trip_id']] = (date, start, end)
    # The agreed history key cannot represent overlapping instances of the same ID.
    return {k: v for k, v in result.items() if k not in ambiguous}


def combine_feeds(trip_payload, vehicle_payload, fetched_at, max_age=120):
    """Build one full observation from two independently timestamped sources."""
    entities = []
    sources = {}
    for name, payload in (('trips', trip_payload), ('vehicles', vehicle_payload)):
        stamp, items = decode(payload)
        if stamp > fetched_at + 30 or fetched_at - stamp > max_age:
            raise FeedValidationError('A configured source is stale or in the future')
        sources[name] = {'timestamp': stamp, 'raw_base64': base64.b64encode(payload).decode('ascii')}
        for entity in items:
            # Keep each kind from its designated source to avoid duplicate vehicles.
            kind = 'trip_update' if name == 'trips' else 'vehicle'
            if kind not in entity:
                continue
            entity['id'] = name + ':' + entity['id']
            entity[kind].setdefault('timestamp', stamp)
            entities.append(entity)
    return json.dumps({'header': {'timestamp': max(v['timestamp'] for v in sources.values())},
                       'entity': entities, 'poller_sources': sources}).encode()


def source_timestamps(payload):
    if payload and payload.lstrip().startswith(b'{'):
        sources = json.loads(payload).get('poller_sources', {})
        return {name: value['timestamp'] for name, value in sources.items()}
    return {}


def ingest(path, payload, fetched_at, route_ids=(), max_age=120):
    timestamp, entities = decode(payload)
    if timestamp > fetched_at + 30 or fetched_at - timestamp > max_age:
        raise FeedValidationError('Feed timestamp is stale or in the future')
    with db.transaction(path) as con:
        previous = con.execute("SELECT MAX(feed_timestamp_utc) FROM feed_snapshots WHERE fetch_status='success'").fetchone()[0]
        if previous and iso(timestamp) <= previous:
            raise FeedValidationError('Repeated or out-of-order feed timestamp')
        source_times = source_timestamps(payload)
        if source_times:
            previous_payload = con.execute("SELECT raw_feed FROM feed_snapshots WHERE fetch_status='success' ORDER BY snapshot_id DESC LIMIT 1").fetchone()
            previous_times = source_timestamps(previous_payload[0]) if previous_payload else {}
            if any(stamp <= previous_times.get(name, 0) for name, stamp in source_times.items()):
                raise FeedValidationError('A configured source has a repeated or out-of-order timestamp')
        updates = [e for e in entities if 'trip_update' in e]
        vehicles = [e for e in entities if 'vehicle' in e]
        snapshot = con.execute('''INSERT INTO feed_snapshots
            (fetched_at_utc,feed_timestamp_utc,fetch_status,entity_count,
             trip_update_count,vehicle_position_count,alert_count,raw_feed)
            VALUES (?,?,'success',?,?,?,?,?)''',
            (iso(fetched_at), iso(timestamp), len(entities), len(updates), len(vehicles),
             sum('alert' in e for e in entities), payload)).lastrowid
        archive = con.execute("SELECT value FROM schema_metadata WHERE key='gtfs_archive_sha256'").fetchone()
        reported_trip_ids = set()
        for entity in updates:
            update = entity['trip_update']
            trip = update.get('trip', {})
            observed = int(update.get('timestamp', timestamp))
            relationship = trip.get('schedule_relationship', 'SCHEDULED')
            if (trip.get('trip_id') and 0 <= timestamp - observed <= max_age
                    and relationship in ('SCHEDULED', 'CANCELED', 0, 3)):
                reported_trip_ids.add(trip['trip_id'])
        for entity in vehicles:
            vehicle = entity['vehicle']
            trip_id = vehicle.get('trip', {}).get('trip_id')
            observed = int(vehicle.get('timestamp', timestamp))
            if trip_id and 0 <= timestamp - observed <= max_age:
                reported_trip_ids.add(trip_id)
        if archive and archive[0] and reported_trip_ids:
            for row in con.execute(
                "SELECT DISTINCT route_id FROM trips WHERE trip_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(reported_trip_ids)),),
            ):
                con.execute('''INSERT INTO realtime_route_coverage VALUES (?,?,?,?)
                    ON CONFLICT(gtfs_archive_sha256,route_id) DO UPDATE SET
                    last_seen_at_utc=excluded.last_seen_at_utc''',
                    (archive[0], row['route_id'], iso(timestamp), iso(timestamp)))
        evidence = {}
        active = scheduled_trips(con, timestamp, route_ids)
        for entity in updates:
            update = entity['trip_update']
            trip = update.get('trip', {})
            trip_id = trip.get('trip_id')
            relationship = trip.get('schedule_relationship', 'SCHEDULED')
            relationship = {0:'SCHEDULED',1:'ADDED',2:'UNSCHEDULED',3:'CANCELED',6:'DUPLICATED',7:'DELETED'}.get(relationship, relationship)
            # Scalar columns summarize the first stop; raw_entity preserves ALL stops.
            stop = next(iter(update.get('stop_time_update', [])), {})
            delay = update.get('delay')
            con.execute('''INSERT INTO trip_updates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (snapshot, entity['id'], trip_id, trip.get('route_id'), trip.get('direction_id'),
                 trip.get('start_date'), trip.get('start_time'), relationship, delay,
                 stop.get('stop_id'), stop.get('stop_sequence'), json.dumps(entity).encode()))
            if (trip_id in active and trip.get('start_date', active[trip_id][0]) == active[trip_id][0]
                    and 0 <= timestamp - int(update.get('timestamp', timestamp)) <= max_age):
                if relationship in ('SCHEDULED','CANCELED'):
                    status = 'cancelled' if relationship == 'CANCELED' else 'seen'
                    if evidence.get(trip_id, ('',))[0] != 'cancelled':
                        evidence[trip_id] = (status, delay, update.get('vehicle', {}).get('id'),
                                             stop.get('stop_id'), stop.get('stop_sequence'))
        for entity in vehicles:
            vehicle = entity['vehicle']
            trip = vehicle.get('trip', {})
            position = vehicle.get('position', {})
            vehicle_id = vehicle.get('vehicle', {}).get('id') or 'entity:' + entity['id']
            con.execute('''INSERT INTO vehicle_positions VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (snapshot, vehicle_id, trip.get('trip_id'), position.get('latitude'),
                 position.get('longitude'), position.get('bearing'), position.get('speed'),
                 vehicle.get('current_stop_sequence'), vehicle.get('stop_id'),
                 vehicle.get('current_status'),
                 iso(int(vehicle['timestamp'])) if 'timestamp' in vehicle else None))
            trip_id = trip.get('trip_id')
            observed = int(vehicle.get('timestamp', timestamp))
            if (trip_id in active and trip.get('start_date', active[trip_id][0]) == active[trip_id][0]
                    and 0 <= timestamp - observed <= max_age):
                evidence.setdefault(trip_id, ('seen', None, vehicle_id, vehicle.get('stop_id'),
                                              vehicle.get('current_stop_sequence')))
        for trip_id in active:
            status, delay, vehicle, stop, sequence = evidence.get(trip_id, ('missing',None,None,None,None))
            con.execute('INSERT INTO trip_status_history VALUES (?,?,?,?,?,?,?,?)',
                        (snapshot, trip_id, status, iso(timestamp), delay, vehicle, stop, sequence))
    return snapshot


def suspected_missing(path, trip_id):
    """Five immediately consecutive fresh successful polls in the same service instance."""
    with closing(db.connect(path)) as con:
        archive = con.execute("SELECT value FROM schema_metadata WHERE key='gtfs_archive_sha256'").fetchone()
        if archive and archive[0]:
            coverage = con.execute(
                "SELECT first_seen_at_utc FROM realtime_route_coverage WHERE gtfs_archive_sha256=? "
                "AND route_id=(SELECT route_id FROM trips WHERE trip_id=?)", (archive[0], trip_id)
            ).fetchone()
            if coverage is None:
                return False
            first_seen = datetime.fromisoformat(coverage[0]).timestamp()
        else:
            first_seen = None
        rows = con.execute('''SELECT f.*,h.status FROM feed_snapshots f
            LEFT JOIN trip_status_history h ON h.snapshot_id=f.snapshot_id AND h.trip_id=?
            ORDER BY f.snapshot_id DESC LIMIT 5''', (trip_id,)).fetchall()
        if len(rows) != 5 or any(r['fetch_status'] != 'success' or r['status'] != 'missing' for r in rows):
            return False
        times = [datetime.fromisoformat(r['feed_timestamp_utc']).timestamp() for r in rows]
        if first_seen is not None and any(value < first_seen for value in times):
            return False
        if any(a - b > 180 for a, b in zip(times, times[1:])):
            return False
        latest = times[0]
        instance = scheduled_trips(con, latest).get(trip_id)
        return bool(instance and all(datetime.fromisoformat(r['feed_timestamp_utc']).timestamp() >= instance[1]
                                     for r in rows))


def fetch(url, key, header, timeout):
    validate_configuration(url, key, header)
    request = Request(url, headers={header: key, 'Accept':'application/json, application/x-protobuf'})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def poll_once(path, url, key, header='x-api-key', route_ids=(), fetcher=fetch, now=time.time, vehicle_url=None, vehicle_key=None):
    payload = None
    attempted = now()
    try:
        if vehicle_url and not vehicle_key:
            raise ConfigurationError('Set NTA_VEHICLE_API_KEY to the secondary subscription token; NTA allows one request per token every 60 seconds')
        payload = fetcher(url, key, header, 30)
        if vehicle_url:
            vehicle_payload = fetcher(vehicle_url, vehicle_key, header, 30)
            payload = combine_feeds(payload, vehicle_payload, now())
        return ingest(path, payload, now(), route_ids), 60
    except Exception as error:
        # Never persist request URLs, headers, exception text, or credentials.
        message = f'HTTP {error.code}' if isinstance(error, HTTPError) else type(error).__name__
        if isinstance(error, (ConfigurationError, FeedValidationError)):
            message = str(error)
        elif isinstance(error, ValueError) and payload is None:
            message = 'Request ValueError: check NTA_REALTIME_URL and key/header formatting'
        retry = 60
        if isinstance(error, HTTPError):
            value = error.headers.get('Retry-After', '')
            try:
                retry = max(60, int(value))
            except ValueError:
                from email.utils import parsedate_to_datetime
                try:
                    retry = max(60, parsedate_to_datetime(value).timestamp() - now())
                except (TypeError, ValueError):
                    pass
        with db.transaction(path) as con:
            snapshot = con.execute('''INSERT INTO feed_snapshots
                (fetched_at_utc,fetch_status,error_message,raw_feed) VALUES (?,'error',?,?)''',
                (iso(attempted), message, payload)).lastrowid
        return snapshot, retry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=None)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--route-id', action='append', default=[])
    args = parser.parse_args()
    url, key = os.environ.get('NTA_REALTIME_URL'), os.environ.get('NTA_API_KEY')
    vehicle_url = os.environ.get('NTA_VEHICLE_POSITIONS_URL')
    vehicle_key = os.environ.get('NTA_VEHICLE_API_KEY')
    if not url or not key:
        parser.error('Set NTA_REALTIME_URL and NTA_API_KEY in the environment')
    if vehicle_url and not vehicle_key:
        parser.error('Set NTA_VEHICLE_API_KEY to your secondary NTA subscription token when configuring the vehicle feed')
    path = db.initialize(args.db)
    print('Polling started; with two subscription tokens, both feeds are fetched once per cycle.', flush=True)
    try:
        while True:
            snapshot, retry = poll_once(path, url, key, os.environ.get('NTA_API_KEY_HEADER','x-api-key'), args.route_id, vehicle_url=vehicle_url, vehicle_key=vehicle_key)
            with closing(db.connect(path)) as con:
                result = con.execute('SELECT fetch_status,error_message FROM feed_snapshots WHERE snapshot_id=?', (snapshot,)).fetchone()
            print(f"Poll {snapshot}: {result['fetch_status']}" + (f" — {result['error_message']}" if result['error_message'] else ''), flush=True)
            if args.once:
                break
            # Retry-After is a delay from the response, not from cycle start.
            # Waiting after ingestion is conservative and avoids an early retry.
            time.sleep(max(61, retry))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
