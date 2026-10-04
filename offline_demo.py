"""Capture a small Dublin fixture and replay it into a NEW, separate database."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import db
from data_health import readonly
from realtime_poller import decode, ingest, scheduled_trips

TABLES = ('agencies','routes','stops','trips','stop_times','calendar','calendar_dates')
DEFAULT_FIXTURE = Path(__file__).parent / 'fixtures' / 'dublin_replay.json'


def capture(source, output, limit=5):
    with closing(readonly(source)) as con:
        con.execute('BEGIN')
        snapshot = con.execute("SELECT * FROM feed_snapshots WHERE fetch_status='success' ORDER BY snapshot_id DESC LIMIT 1").fetchone()
        if not snapshot:
            raise ValueError('No successful source snapshot')
        stamp, entities = decode(snapshot['raw_feed'])
        active = scheduled_trips(con, stamp)
        candidates = {r[0] for r in con.execute('''SELECT t.trip_id FROM trips t JOIN routes r USING(route_id)
            JOIN agencies a USING(agency_id) WHERE a.agency_name LIKE '%Dublin Bus%' AND r.route_type=3''')}
        observed = {e.get('trip_update',e.get('vehicle',{})).get('trip',{}).get('trip_id') for e in entities}
        selected = sorted(t for t in candidates & observed if t in active and active[t][2] >= stamp + 600)[:limit]
        if not selected:
            raise ValueError('No matching active Dublin trips; load compatible static data first')
        def rows(table, column, values):
            if not values:
                return []
            return [dict(r) for r in con.execute(f"SELECT * FROM {table} WHERE {column} IN ({','.join('?' for _ in values)})", tuple(values))]
        tables = {}
        tables['trips'] = rows('trips','trip_id',selected)
        tables['stop_times'] = rows('stop_times','trip_id',selected)
        tables['routes'] = rows('routes','route_id',{r['route_id'] for r in tables['trips']})
        tables['agencies'] = rows('agencies','agency_id',{r['agency_id'] for r in tables['routes'] if r['agency_id'] is not None})
        tables['stops'] = rows('stops','stop_id',{r['stop_id'] for r in tables['stop_times']})
        included = {r['stop_id'] for r in tables['stops']}
        while parents := {r['parent_station'] for r in tables['stops'] if r['parent_station'] and r['parent_station'] not in included}:
            extra = rows('stops','stop_id',parents)
            if not extra:
                raise ValueError('Missing parent stop in source')
            tables['stops'].extend(extra)
            included.update(r['stop_id'] for r in extra)
        services = {r['service_id'] for r in tables['trips']}
        tables['calendar'] = rows('calendar','service_id',services)
        tables['calendar_dates'] = rows('calendar_dates','service_id',services)
        entities = [e for e in entities if e.get('trip_update',e.get('vehicle',{})).get('trip',{}).get('trip_id') in selected]
        fixture = {'mode':'historical_replay', 'description':'Recorded NTA Dublin Bus sample; NOT live data. Static tables and feed reduced to selected trips.',
            'attribution':'National Transport Authority, CC BY 4.0',
            'source_url':'https://www.transportforireland.ie/transitData/PT_Data.html',
            'captured_snapshot_id':snapshot['snapshot_id'], 'as_of_utc':datetime.fromtimestamp(stamp,timezone.utc).isoformat(),
            'tables':tables, 'feed':{'header':{'gtfs_realtime_version':'2.0','timestamp':stamp},'entity':entities}}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as file:
        json.dump(fixture,file,indent=2,ensure_ascii=False)
        file.write('\n')
    return fixture


def replay(fixture_path, destination):
    fixture = json.loads(Path(fixture_path).read_text())
    destination = Path(destination).expanduser().resolve()
    if destination == db.database_path().resolve():
        raise ValueError('Replay cannot target the live TRANSPORT_DB_PATH')
    # Exclusive creation prevents overwriting any existing user database.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb'):
        pass
    db.initialize(destination)
    with db.transaction(destination) as con:
        con.execute('PRAGMA defer_foreign_keys=ON')
        for table in TABLES:
            allowed = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
            for record in fixture['tables'][table]:
                if not set(record) <= allowed:
                    raise ValueError('Unknown fixture column')
                con.execute(f"INSERT INTO {table} ({','.join(record)}) VALUES ({','.join('?' for _ in record)})",tuple(record.values()))
        con.executemany('INSERT OR REPLACE INTO schema_metadata VALUES (?,?)', (
            ('demo_mode','historical_replay'), ('demo_as_of_utc',fixture['as_of_utc']),
            ('gtfs_source_url',fixture['source_url'])))
    stamp = fixture['feed']['header']['timestamp']
    ingest(destination,json.dumps(fixture['feed']).encode(),stamp)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    cap = commands.add_parser('capture')
    cap.add_argument('--db',default=str(db.database_path()))
    cap.add_argument('--output',default=str(DEFAULT_FIXTURE))
    run = commands.add_parser('replay')
    run.add_argument('--fixture',default=str(DEFAULT_FIXTURE))
    run.add_argument('--db',required=True)
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            fixture = capture(args.db,args.output)
            print(f"Captured {len(fixture['tables']['trips'])} trips to {args.output}")
        else:
            path = replay(args.fixture,args.db)
            print(f'OFFLINE HISTORICAL REPLAY — never live. Database: {path}')
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1,f'{error}\n')


if __name__ == '__main__':
    main()
