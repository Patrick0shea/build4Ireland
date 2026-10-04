# Poller → MCP handoff

## MCP mode coverage

The MCP searches the full loaded national NTA timetable for GTFS route types
0 (tram/Luas), 2 (rail/Irish Rail), and 3 (bus, including Bus Éireann and
other loaded operators). Use `find_stops(query, mode="rail")` or
`find_stops(query, mode="tram")` to narrow a stop search. Then pass the returned
GTFS stop ID to `get_departures`, optionally setting `mode` and an exact
`operator` name returned by the stop search. Omit `mode` to include all three
types. This changes the MCP query scope; it does not add feeds beyond the
national NTA static GTFS and the configured NTA realtime sources. A departure
is live only when a fresh matching observation exists. Other services such as
Dublin Bikes require a separate data source.

Use `db.connect()` and the same `TRANSPORT_DB_PATH` (default
`data/transport.sqlite3`). The poller writes realtime tables; the loader writes
static tables. No schema changes were needed for this handoff.

## Verified local data

The national `GTFS_All.zip` was imported on 4 October 2026: 104 agencies,
805 routes, 14,155 stops, 267,335 trips, and 8,438,017 stop times. The full
archive is retained in the database, including operators outside realtime
coverage. Snapshot 20 matched all 2,365 non-empty trip IDs; another 23 updates had no trip ID.
An exact ID match establishes compatibility, not proof that every service is live.

Refresh with `python3 gtfs_loader.py`. Inspect with `python3 data_health.py`
or `python3 data_health.py --json`. The health command is read-only and makes
no API calls. The JSON response includes source ages, recent failures, static
counts, ID-match percentage, IDs without matches, and current status counts.
Percentages exclude absent/empty IDs, which are counted separately. Added trips
may legitimately have no static match. Scheduled-update match counts are also
included. Match counts concern this feed snapshot, not network-wide coverage.

Calendar dates in this database are **YYYY-MM-DD**. Realtime `start_date` remains
**YYYYMMDD**. Preserve all GTFS IDs as text, including spaces and leading zeros.
`stop_times` values are service-day seconds and may exceed 86400. To get a UTC
instant, construct local noon on the service date in the agency timezone,
convert to UTC, subtract 12 elapsed hours, then add service seconds. Apply
calendar_dates additions/removals, and examine prior service days for >24h trips.

## Offline demo (no API key)

A reduced real NTA snapshot and matching static records are included in
`fixtures/dublin_replay.json`: five route 27 trips, 85 stops, five trip updates,
and five vehicle positions, recorded at **2026-10-04T12:26:50+00:00**.
This is historical data, never current live service information.

```sh
python3 offline_demo.py replay --db data/demo-new.sqlite3
python3 data_health.py --db data/demo-new.sqlite3
sqlite3 -header -column data/demo-new.sqlite3 < docs/handoff_queries.sql
```

Replay requires a new destination and refuses to overwrite an existing database
or the configured live database. It records `demo_mode=historical_replay` and
`demo_as_of_utc` in schema_metadata. MCP outputs must expose that label and the
recorded timestamp. For deterministic demo queries, inject that recorded time;
do not rewrite timestamps to today. The health command uses the actual clock,
so historical data correctly appears stale.

Fixture attribution: National Transport Authority, CC BY 4.0.
Source: https://www.transportforireland.ie/transitData/PT_Data.html
The fixture is a selected-trip subset; omitted journeys must not be inferred
absent from the real transport network.

To capture another fixture without overwriting this one:
`python3 offline_demo.py capture --output /tmp/dublin-new.json`.

## Query contracts and example results

Run `docs/handoff_queries.sql` against the demo database. It contains examples for:

1. Route/stop lookup: exact route ID `1 27 c a`, route name `27`, stop `8220DB000298`
   (`Eden Quay`). IDs must never be parsed for business meaning.
2. Scheduled stop times: route, headsign, service ID, stop name and departure
   seconds. This is raw schedule data; apply service-day/calendar logic before
   presenting a departure as running on a requested date.
3. Individual stop predictions: expand `trip_updates.raw_entity` using
   `json_each(..., '$.trip_update.stop_time_update')`. The scalar `stop_id` on
   trip_updates summarizes only the first stop and is NOT the complete journey.
4. Vehicle locations from the latest successful snapshot. These are positions,
   not arrival predictions. Example fixture vehicle count: 5.
5. Status counts: the fixture produces `seen = 5`.
6. Unmatched IDs and snapshot errors: the fixture has zero unmatched non-empty
   trip IDs and no errors. Live errors remain in feed_snapshots.

Always choose a successful snapshot and return its source timestamp, actual age,
and most recent fetch error separately. With two configured feeds, raw_feed is
an envelope with per-source timestamps in `poller_sources`. A successful poll
is not automatically fresh when read later. Use each entity/source's timestamp;
if >120 seconds old, report scheduled-only/unknown and withhold live predictions.
Vehicle timestamps can be NULL. Avoid presenting a previous vehicle location as
current just because the most recent feed did not contain that vehicle.

Stop-time predictions can contain absolute epoch times, delay seconds, both, or
neither. Prefer explicit times. Delay-only predictions need the matching static
stop and service date. Preserve NULL (unknown) rather than treating it as zero.
Respect SKIPPED and NO_DATA stop relationships. A cancellation takes precedence
over an ETA. The SQL examples expose raw evidence; they do not implement delay
propagation or a journey planner.

## Status and reliability

`seen` means observed in fresh trip-update or vehicle evidence, not proof of an
actual arrival. `missing` means a scheduled active trip was not observed in this
configured feed. `cancelled` requires explicit cancellation evidence.

Use `realtime_poller.suspected_missing(path, trip_id)` for five consecutive missing
observations in one scheduled instance. API errors, intervening observations,
missing history rows, and observation gaps over 180s break the streak. Five polls
at the conservative two-feed cadence span about eight minutes, not five minutes.
Label the result **suspected missing from feed**, never confirmed non-operation.
The status table cannot represent overlapping instances of one trip ID; those
instances are excluded. Statuses are scoped to trips between first and last
scheduled stops, so they are not a complete future cancellation list. For future
cancellations inspect trip_updates directly, with its service-date descriptor.

Before showing reliability, scope the poller to demo routes covered by the feed
using repeated `--route-id` arguments. Full national static coverage exceeds
realtime coverage; raw missing counts across all operators are not a reliable
performance score. Low matching or stale feeds must withhold reliability claims.

## Validation and startup

`python3 -m unittest discover -s tests -v` checks actual ZIP-loader → poller
integration, calendar exceptions, repeated imports preserving history, five misses,
failed fetches, cancellations, freshness, and offline replay protections.

Restart any already-running poller after pulling these changes; the calendar-date
compatibility fix is loaded only at process startup. Historic snapshots taken
before static import remain unchanged; no fabricated statuses were backfilled.
With both sources, expect about two minutes per completed cycle. This cadence is
a rate-limit workaround, not a verified subscription entitlement.
