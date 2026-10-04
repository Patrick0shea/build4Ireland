# Database contract (M1)

The local database defaults to `data/transport.sqlite3`. Set `TRANSPORT_DB_PATH`
to override it. Both the poller and MCP server should call `db.initialize()` at
startup and use `db.connect()` or `db.transaction()` for access. SQLite WAL mode
is enabled so the poller can write while the MCP server reads.

## Static timetable

`agencies`, `routes`, `stops`, `trips`, `stop_times`, `calendar`, and
`calendar_dates` contain the imported GTFS dataset. GTFS IDs are text and must
be preserved exactly because realtime trip descriptors refer to these IDs.
The stop-time columns `arrival_secs` and `departure_secs` are integer seconds
from the service day's midnight; they intentionally allow values above 86,400
for GTFS times such as `25:10:00`.

For the first demo, Dublin bus routes are selected from this national timetable.
Do not discard the other operators during import; the full feed is useful for
future Republic of Ireland coverage.

## Realtime history

- `feed_snapshots`: one row per poll attempt. A failed request has
  `fetch_status='error'` and must not create missing-trip records.
- `trip_status_history`: one row per scheduled trip per successful poll, with
  `status` set to `seen`, `missing`, or `cancelled`. Five consecutive `missing`
  rows for the same trip, with no failed poll between them, qualify as
  **suspected missing**. Keep the status provisional; it is not proof that a bus
  was cancelled or failed to operate.
- `trip_updates`: preserves raw realtime trip updates even when trip IDs cannot
  be matched to the loaded timetable. This is needed to diagnose GTFS version
  mismatches; realtime IDs are intentionally not constrained by static-table
  foreign keys.
- `vehicle_positions`: vehicle locations associated with their source snapshot.

All timestamps ending in `_utc` use ISO 8601 UTC strings. Scheduled service
dates and service seconds remain in the GTFS agency timezone (`Europe/Dublin` for
the initial demo); convert scheduled instants to UTC when comparing to feed
timestamps.

## Initialization

```python
import db

db.initialize()
with db.connect() as connection:
    routes = connection.execute(
        "SELECT route_id, route_short_name FROM routes WHERE route_type = 3"
    ).fetchall()
```
