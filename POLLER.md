# Realtime poller handoff

`realtime_poller.py` uses the existing `db.py`, `TRANSPORT_DB_PATH`, and shared
`DATABASE.md` schema. No schema changes were made. The loader owns static tables;
the poller writes only realtime tables. Run one poller process per database.

## Run

Python 3.10+ is required. First populate the timetable with the teammate's loader:

```sh
python3 gtfs_loader.py
```

Set environment variables in your shell (the program does not auto-load `.env`):

```sh
export TRANSPORT_DB_PATH="$PWD/data/transport.sqlite3"
export NTA_REALTIME_URL='COPY_THE_FULL_FEED_URL_FROM_YOUR_NTA_PORTAL'
export NTA_API_KEY_HEADER='x-api-key'
read -s NTA_API_KEY
export NTA_API_KEY
python3 realtime_poller.py --once
python3 realtime_poller.py
```

For JSON, select `format=json` in the portal and include that query parameter in
the URL. For protobuf responses, install the optional decoder first:

```sh
python3 -m pip install -r requirements-poller.txt
```

Use the full snapshot endpoint covering the operators being monitored. A
vehicle-only or operator-limited response must not be used to judge trips outside
that coverage. Confirm your subscription's URL and quota in the portal. The
poller waits at least 61 seconds between requests; HTTP Retry-After can extend that delay.
No authenticated NTA request has been verified in this checkout.

Select known Dublin route IDs from the static timetable and scope the demo:

```sh
python3 realtime_poller.py --route-id EXACT_ROUTE_ID --route-id ANOTHER_ROUTE_ID
```

This limits status classification, not raw ingestion. Without this flag all
currently scheduled trips are considered. It does not filter or edit static data.
`--db /path/to/database.sqlite3` overrides `TRANSPORT_DB_PATH` for this process.

## Stored evidence

- Every fetch/parse failure gets a `feed_snapshots` error row with no status rows.
  Raw payloads are retained when received; errors contain only exception class or
  HTTP code, not credentials or request URLs.
- Successful full snapshots are stored atomically with trip updates, vehicle
  positions, and scheduled-trip statuses. Duplicate, out-of-order, stale (>120s),
  future (>30s), malformed, and differential feeds are rejected as error attempts.
- Exact text trip IDs are preserved. Unmatched updates and vehicles remain
  available through a LEFT JOIN with `trips`; no guessed route/name joins occur.
- `trip_updates.raw_entity` preserves every stop prediction. The scalar stop
  columns summarize the first stop only; `delay_seconds` is the trip-level delay
  when present. Missing optional values stay NULL. `raw_feed` preserves the full
  original response, including alerts (only alert counts are otherwise indexed).
- A trip is considered scheduled while between its first and last stop time,
  with its service calendar, exceptions, agency timezone and >24h times applied.
  GTFS service-day origin is local noon minus twelve elapsed hours, including DST.
- Matching trip updates or fresh vehicle observations mean `seen`; explicit
  trip cancellation takes precedence. A descriptor for a different service date
  does not mark today's trip seen. Added/unscheduled updates are retained but do
  not count as evidence for scheduled instances.
- The unchanged schema has no service-date column in history and permits only one
  status per trip ID per snapshot. Overlapping instances are excluded from status
  classification. The suspicion helper checks the current service instance.

```python
from realtime_poller import suspected_missing

suspected = suspected_missing('data/transport.sqlite3', 'EXACT_TRIP_ID')
```

The helper requires five immediately consecutive successful `missing` snapshots,
within the same scheduled instance. Errors, seen/cancelled states, absent status
rows, or gaps over 180 seconds break the streak. This is **suspected missing from
the feed**, never proof of cancellation or failure to operate. Feed coverage and
static/realtime compatibility should be checked before presenting this signal.

Inspect unmatched IDs:

```sql
SELECT u.snapshot_id, u.trip_id
FROM trip_updates u LEFT JOIN trips t ON t.trip_id = u.trip_id
WHERE t.trip_id IS NULL;
```

Inspect successive attempts:

```sql
SELECT snapshot_id, fetched_at_utc, fetch_status, entity_count, error_message
FROM feed_snapshots ORDER BY snapshot_id DESC LIMIT 10;
```

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Tests use fresh temporary databases and injected feeds; they require no API key.

## Separate vehicle endpoint

Set `NTA_VEHICLE_POSITIONS_URL` to the full Vehicle Positions request URL from
your NTA portal, then restart the existing process. Keep `NTA_REALTIME_URL`
pointing at the trip-update feed. Both requests use the same key/header.

With this option each cycle makes two requests separated by 61 seconds, then
waits at least another 61 seconds before starting the next cycle. Each feed is
therefore fetched about once every two minutes, trading the original 60-second
per-feed target for conservative shared-key request spacing. Confirm the actual
subscription quota in the portal. The cycle stores one combined `feed_snapshots` observation using the existing
schema. This is one logical poll attempt spanning both configured feeds. A failure
of either request or either feed's validation records an error and writes no
status rows or partial normalized data; it conservatively breaks the missing
streak. Each source must advance its timestamp independently. Successful
`raw_feed` values are JSON envelopes containing the normalized combined feed and
`poller_sources` with each original response encoded as base64 and its timestamp.
The combined timestamp is the later source timestamp; entity timestamps retain
their own source timestamp when no explicit entity timestamp was supplied.
Trip entities come from the trip source, vehicle entities from the vehicle source;
entity IDs are prefixed with the source to prevent collisions. Both original
responses, including any alerts, are preserved in the envelope; alerts are not
included in its normalized entity counts.

Do not start a second independent poller against this database for the vehicle
feed: status classification should happen once per combined observation. Stop
with Ctrl+C, set the new variable, then run `python3 realtime_poller.py --once`.
Check `vehicle_position_count`, then restart continuous polling. Live access to
the separate vehicle endpoint still requires verification with your account.

After changing polling code, stop the old process and restart it. On startup with
two feeds, the first result takes at least 61 seconds. Retry-After is honoured as
a full delay after the failed response; request time is not subtracted from it.
The spacing is conservative, not a verified guarantee of the account quota.
