"""Read-only transport database diagnostics; no upstream API requests."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

import db


def readonly(path=None):
    selected = Path(path or db.database_path()).expanduser().resolve()
    connection = sqlite3.connect(selected.as_uri() + '?mode=ro', uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def age(value, now):
    return round(now - datetime.fromisoformat(value).timestamp(), 1) if value else None


def report(path=None, now=None):
    now = time.time() if now is None else now
    with closing(readonly(path)) as con:
        con.execute('BEGIN')
        metadata = dict(con.execute('SELECT key,value FROM schema_metadata'))
        tables = ('agencies', 'routes', 'stops', 'trips', 'stop_times', 'calendar', 'calendar_dates')
        counts = {table: con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}
        recent = [dict(r) for r in con.execute('''SELECT snapshot_id,fetched_at_utc,feed_timestamp_utc,
            fetch_status,trip_update_count,vehicle_position_count,error_message
            FROM feed_snapshots ORDER BY snapshot_id DESC LIMIT 10''')]
        latest = con.execute("SELECT * FROM feed_snapshots WHERE fetch_status='success' ORDER BY snapshot_id DESC LIMIT 1").fetchone()
        result = {'database': str(Path(path or db.database_path()).expanduser().resolve()),
                  'mode': metadata.get('demo_mode', 'live'),
                  'checked_at_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                  'static_counts': counts, 'static_imported_at_utc': metadata.get('gtfs_imported_at_utc'),
                  'static_source': metadata.get('gtfs_source_url'),
                  'recent_polls': recent, 'warnings': [], 'latest_success': None}
        if not counts['trips'] or not counts['stop_times']:
            result['warnings'].append('Static timetable is empty; matching and status tracking are unavailable.')
        if latest:
            sid = latest['snapshot_id']
            metrics = dict(con.execute('''SELECT COUNT(*) AS total_updates,
                SUM(CASE WHEN u.trip_id IS NULL OR u.trip_id='' THEN 1 ELSE 0 END) AS missing_trip_ids,
                SUM(CASE WHEN t.trip_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_updates,
                SUM(CASE WHEN u.trip_id IS NOT NULL AND u.trip_id!='' THEN 1 ELSE 0 END) AS updates_with_ids
                FROM trip_updates u LEFT JOIN trips t ON u.trip_id=t.trip_id WHERE snapshot_id=?''', (sid,)).fetchone())
            metrics = {k: v or 0 for k, v in metrics.items()}
            metrics['match_percent_of_nonempty_ids'] = (round(100 * metrics['matched_updates'] / metrics['updates_with_ids'], 2)
                                                       if metrics['updates_with_ids'] else None)
            metrics['unmatched_id_examples'] = [r[0] for r in con.execute('''SELECT DISTINCT u.trip_id FROM trip_updates u
                LEFT JOIN trips t ON t.trip_id=u.trip_id WHERE snapshot_id=? AND t.trip_id IS NULL
                AND u.trip_id IS NOT NULL AND u.trip_id!='' LIMIT 10''', (sid,))]
            metrics['scheduled_updates'] = con.execute("SELECT COUNT(*) FROM trip_updates WHERE snapshot_id=? AND schedule_relationship='SCHEDULED'", (sid,)).fetchone()[0]
            metrics['matched_scheduled_updates'] = con.execute("SELECT COUNT(*) FROM trip_updates u JOIN trips t ON t.trip_id=u.trip_id WHERE snapshot_id=? AND schedule_relationship='SCHEDULED'", (sid,)).fetchone()[0]
            statuses = dict(con.execute('SELECT status,COUNT(*) FROM trip_status_history WHERE snapshot_id=? GROUP BY status', (sid,)))
            raw = latest['raw_feed']
            sources = json.loads(raw).get('poller_sources', {}) if raw and raw.lstrip().startswith(b'{') else {}
            source_ages = {name: round(now - item['timestamp'], 1) for name, item in sources.items()}
            result['latest_success'] = {'snapshot_id': sid, 'fetched_at_utc': latest['fetched_at_utc'],
                'feed_timestamp_utc': latest['feed_timestamp_utc'],
                'feed_age_seconds': age(latest['feed_timestamp_utc'], now), 'source_age_seconds': source_ages,
                'trip_update_count': latest['trip_update_count'], 'vehicle_position_count': latest['vehicle_position_count'],
                'matching': metrics, 'statuses': statuses}
            if any(v > 120 or v < -30 for v in source_ages.values()) or (age(latest['feed_timestamp_utc'], now) or 0) > 120:
                result['warnings'].append('One or more feeds are stale; expose scheduled-only results, not fresh live predictions.')
            if metrics['matched_updates'] < metrics['updates_with_ids']:
                result['warnings'].append('Some realtime IDs do not match this timetable; inspect feed compatibility.')
            if not statuses:
                result['warnings'].append('Latest successful snapshot has no status history; check calendar, route scope and poller restart after import.')
        else:
            result['warnings'].append('No successful realtime snapshots.')
        if recent and recent[0]['fetch_status'] == 'error':
            result['warnings'].append('Latest poll failed; last successful data is retained but is not a fresh observation.')
        if result['mode'] != 'live':
            result['warnings'].insert(0, 'OFFLINE REPLAY: historical fixture data, never current live transport information.')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = report(args.db)
    except sqlite3.Error as error:
        parser.exit(1, f'Database unavailable: {error}\n')
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Database: {result['database']}\nMode: {result['mode']}")
    print('Static: ' + ', '.join(f'{k}={v:,}' for k, v in result['static_counts'].items()))
    success = result['latest_success']
    if success:
        print(f"Last success: #{success['snapshot_id']} at {success['fetched_at_utc']}")
        print(f"Feed age: {success['feed_age_seconds']} seconds; source ages: {success['source_age_seconds']}")
        print(f"Updates: {success['trip_update_count']}; vehicles: {success['vehicle_position_count']}")
        match = success['matching']
        print(f"ID matches: {match['matched_updates']}/{match['updates_with_ids']} ({match['match_percent_of_nonempty_ids']}%); updates without IDs: {match['missing_trip_ids']}")
        print(f"Statuses: {success['statuses']}")
    for poll in result['recent_polls']:
        print(f"#{poll['snapshot_id']} {poll['fetch_status']} {poll['error_message'] or ''}")
    for warning in result['warnings']:
        print('WARNING: ' + warning)


if __name__ == '__main__':
    main()
