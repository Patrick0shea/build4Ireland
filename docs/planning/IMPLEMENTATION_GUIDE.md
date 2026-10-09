# Implementation guide: Honest Live Transport for Ireland

Based on [`PLAN.md`](PLAN.md). This guide defines the build order, interfaces, data model, validation, and demo for a working hackathon MVP. The repository currently contains only the plan; commands below describe the implementation to build, not existing functionality.

## 1. Ship this first

Build an MCP server that answers three questions:

1. “Which stop do you mean?” → `find_stops`.
2. “What is due here?” → `get_departures`, with evidence and freshness.
3. “How much live evidence do we have for this route?” → `get_route_reliability`.

Start with one supported NTA bus feed and a small set of stops/routes with verified static-to-live ID matches. Keep the architecture ready for national coverage, but name the actual supported operators in the demo. Discover current routes from the feed rather than assuming the plan’s 46A example is still valid.

**Success:** a client calls the tools, receives useful departures, sees the difference between live predictions and timetable entries, and gets an honest response during an upstream outage.

**Defer:** rail, Luas, bikes, route planning, fares, national reliability rankings, and cloud deployment until the core flow works. A local demonstration is enough for the first submission.

The [event page](https://luma.com/build-for-ireland) lists building from 12:00 and submissions at 16:00. Treat this as a four-hour MVP budget, adjusted to time remaining on the day.

## 2. Correct the reliability claim

Missing real-time data does **not** prove a bus failed to run. The [GTFS trip-update documentation](https://gtfs.org/documentation/realtime/feed-entities/trip-updates/) explicitly treats an absent update as unavailable real-time information.

Use these concepts separately:

| Concept | Evidence | User wording |
| --- | --- | --- |
| Live prediction | Fresh stop-time prediction | “Live estimate, updated 45 seconds ago” |
| Scheduled only | Active timetable entry without a usable prediction | “Scheduled; no live prediction available” |
| Cancelled | Explicit trip cancellation | “Cancelled according to the feed” |
| Stop skipped | Explicit `SKIPPED` for this stop | “This service will not stop here” |
| Stale evidence | Previously received prediction now too old | “Last prediction is stale; showing timetable” |
| Not observed | No trip evidence during a sufficiently monitored window | “Not observed in the feed; operation unconfirmed” |
| Insufficient history | Short collection period, outage, or poor ID matching | “Insufficient data to assess this route” |

For the MVP, keep the planned tool name `get_route_reliability`, but describe its output as **live-feed visibility**, not arrival reliability. Actual punctuality requires observations of arrivals or another defensible ground truth. Do not market a visibility percentage as the probability a bus will turn up.

## 3. First 20 minutes: remove data-access risks

- Register or sign in at the [NTA developer portal](https://developer.nationaltransport.ie/). It advertises GTFS-Realtime v2 and requires keys.
- Inspect the API documentation available to your account. Confirm the production URL, authentication header, response encoding, available feeds, and request quota. Do not guess endpoint paths or headers.
- Locate the static GTFS archive corresponding to that live feed through the portal’s schedules link. Download it once and record its source, retrieval time, and checksum.
- Fetch one authorised real-time response. Check its feed timestamp, entity types, and whether its trip IDs match the archive. Record matched and unmatched counts.
- Select a real stop and route with usable live evidence for the demo. Check direction and destination.
- Start retaining snapshots immediately so you can build observation history while implementing the tools.

**Access gate:** if credentials or compatible data are unavailable after 20 minutes, switch the build to clearly labelled fixtures/replay. Continue implementing the same ingestion and query paths. Synthetic fixtures demonstrate behaviour; they are never presented as live Irish services.

The NTA’s [UAT usage policy](https://developeruat.ntaapptest.transportforireland.ie/usagepolicy) describes a 60-second token limit. This is a planning reference, not confirmation of your production quota. Verify the policy for your actual subscription before polling multiple endpoints; two requests per minute may exceed a shared token limit.

## 4. Project structure and setup

Use Python 3.11+, FastMCP, SQLite, httpx, gtfs-realtime-bindings, rapidfuzz, pydantic-settings, and pytest. Pin compatible dependency versions after the first working installation and commit the lockfile.

```text
pyproject.toml
.env.example
.gitignore
src/irish_transport/
  config.py           # environment settings and thresholds
  db.py               # schema, migrations, connection settings
  service_time.py     # GTFS service dates and UTC conversion
  import_gtfs.py      # archive validation and transactional import
  poller.py           # quota-aware fetch, decode, persist
  normalise_rt.py     # trip instances and stop-level predictions
  departures.py       # timetable/live merge
  reliability.py      # observation-window calculations
  server.py           # MCP tool definitions
  demo.py             # optional minimal read-only dashboard
scripts/
  seed_fixtures.py
  replay_feed.py
tests/
  fixtures/
  test_service_time.py
  test_departures.py
  test_reliability.py
data/                 # ignored: SQLite DB and downloaded feeds
```

Example installation once `pyproject.toml` exists:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Suggested `.env.example`:

```dotenv
DATABASE_PATH=data/transport.sqlite3
NTA_STATIC_URL=
NTA_TRIP_UPDATES_URL=
NTA_VEHICLE_POSITIONS_URL=
NTA_API_KEY=
NTA_API_KEY_HEADER=
POLL_INTERVAL_SECONDS=60
LIVE_MAX_AGE_SECONDS=120
OBSERVATION_GRACE_SECONDS=600
MIN_WINDOW_COVERAGE=0.90
MIN_ELIGIBLE_TRIPS=20
DEMO_MODE=live
MCP_HOST=127.0.0.1
MCP_PORT=8000
```

These thresholds are proposed MVP defaults, not provider guarantees. Keep secrets out of logs, snapshots, version control, and tool output. Ignore `.env`, `.venv`, `data/`, and Python caches.

## 5. Storage: preserve the evidence

Use two processes on the same machine: one poller writing SQLite and one MCP server reading it. Enable WAL, foreign keys, and a busy timeout on each connection. Keep write transactions short and batch ingestion. Never keep a database transaction open during an HTTP request.

Suggested minimum schema:

| Table | Key and essential columns |
| --- | --- |
| `feed_versions` | `feed_version`, source URL, downloaded UTC, checksum, validity dates |
| `routes` | `(feed_version, route_id)`, agency ID, short/long name, route type |
| `stops` | `(feed_version, stop_id)`, name, lat/lon, parent station |
| `trips` | `(feed_version, trip_id)`, route ID, service ID, headsign, direction ID |
| `stop_times` | `(feed_version, trip_id, stop_sequence)`, stop ID, arrival/departure seconds, pickup/drop-off rules |
| `calendar` | `(feed_version, service_id)`, weekdays, start/end dates |
| `calendar_dates` | `(feed_version, service_id, date)`, exception type |
| `poll_attempts` | attempt ID, source, fetch UTC, HTTP status, feed UTC, success/error, match counts |
| `rt_snapshots` | snapshot ID, source, feed UTC, received UTC, payload hash, payload path, feed version |
| `trip_observations` | snapshot ID, trip instance key, trip/route ID, relationship, entity timestamp, vehicle ID |
| `stop_predictions` | snapshot ID, trip instance key, stop sequence/ID, arrival/departure UTC, relationship, uncertainty |
| `vehicle_observations` | snapshot ID, trip instance key if resolvable, vehicle ID, position UTC, lat/lon |

A trip instance key includes **feed namespace/version, trip ID, service date, and start time where required**. A trip ID alone repeats across dates. Keep unresolved entities separately with their raw identifiers; never silently join them by route name.

Add indexes on stop-times `(feed_version, stop_id, departure_seconds)`, trips `(feed_version, route_id, service_id)`, and observations `(trip_instance_key, snapshot_id)`. Track the active static version separately; old observations retain their original version.

Keep immutable snapshot history plus a current projection for fast queries. A successful empty feed must replace the current projection; otherwise yesterday’s entities can remain falsely live. Do not erase historical observations when an entity disappears.

## 6. Implement M1: static GTFS import

1. Download with a timeout and validate that the result is a readable ZIP.
2. Read CSV with proper quoting and UTF-8 BOM handling. Preserve IDs as strings, including leading zeros.
3. Load routes, stops, trips, stop_times, calendar, and calendar_dates. GTFS can supply calendar_dates without calendar; support that case.
4. Parse `HH:MM:SS` as `hours * 3600 + minutes * 60 + seconds`. `25:10:00` is 90,600 seconds, not a malformed clock time.
5. Validate foreign-key joins and report broken references, service validity, and row counts.
6. Import into a new feed version inside a transaction. Activate only after validation; leave the previous version available if import fails.
7. Implement active services: weekday/date range, then calendar_dates additions and removals.

**Time rule:** compute events relative to the GTFS service date in the agency timezone, then store resulting UTC instants. Follow GTFS’s noon-minus-12-hours service-day definition for DST transitions rather than assuming every service day begins at an ordinary local midnight. Query prior service days when their offsets overlap the requested departure window; include more than one prior day if the feed contains larger offsets.

Reference: [GTFS Schedule specification](https://gtfs.org/documentation/schedule/reference/).

**Done when:** a known stop returns valid scheduled services for an injected time, including a previous-day `25:10` departure and a calendar exception.

## 7. Implement M1: poller

Poll centrally; MCP calls only read the database.

```text
wait until the subscription permits the next request
fetch a configured feed with a bounded timeout
decode and validate without changing current state
record attempt and retain safe raw payload
normalise identifiers, service dates, and timestamps
transactionally save history and replace current source projection
on failure, retain last good data and expose its increasing age
```

- Decode protobuf only if the endpoint actually returns protobuf; add a JSON adapter if required by the verified API.
- Use protobuf field presence checks. An absent time or delay must not become a zero-valued prediction.
- Handle explicit cancellations, stop skips, and `NO_DATA`. A vehicle position alone does not produce an ETA.
- Use absolute predicted event time when supplied; otherwise apply a valid delay to the scheduled event. Follow GTFS propagation rules if supported; if not, expose only directly supported stop predictions for the MVP.
- Use entity timestamps where available and feed timestamp as a fallback. Fetch time is not proof that the upstream data is fresh. Missing/invalid source timestamps mean freshness is unknown.
- Reject out-of-order snapshots from the current projection; retain them as diagnostic evidence. Support full snapshots first; explicitly reject or correctly process differential feeds.
- Measure ID matching for in-scope scheduled entities, excluding legitimately added trips. Poor matching disables visibility scoring and produces a diagnostic.
- On 429, honour `Retry-After`; on other failures use quota-aware exponential backoff with jitter. Successful HTTP responses containing the same old feed do not count as fresh coverage.
- Record independent health/freshness for each source. A working vehicle feed must not hide a broken prediction feed.

**Done when:** at least two snapshots ingest, repeat ingestion is safe, an empty snapshot clears current entries, and simulated upstream failure changes freshness without deleting evidence.

## 8. Implement M2: MCP tools

Use typed inputs/outputs and deterministic domain functions underneath the tools. FastMCP’s [running-server documentation](https://github.com/PrefectHQ/fastmcp/blob/main/docs/deployment/running-server.mdx) documents Streamable HTTP through `transport="http"`.

```python
from fastmcp import FastMCP

mcp = FastMCP("Honest Irish Transport")

# Register thin @mcp.tool wrappers around tested domain functions.

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)
```

Check the installed version’s default MCP path and verify the client handshake before preparing the demo. Bind publicly only when deploying intentionally.

### `find_stops(query: str, limit: int = 5)`

Normalize case, whitespace, and accents. Rank exact ID/name matches first, then rapidfuzz matches. Return stop ID, full name, coordinates, score, and parent/direction context where available. Bound limits and query length. Return alternatives for ambiguous names instead of silently selecting a stop.

### `get_departures(stop_id: str, modes: list[str] | None = None, limit: int = 10)`

Query the next 60 minutes by default, with an injected clock for testing. Map modes from GTFS route types, validate unsupported modes, and respect no-pickup stop entries.

Resolve active trip instances, join current evidence, apply stop relationships, and sort by usable predicted time or scheduled time. Include nearby explicitly cancelled services as cancelled entries. If stale predictions could have moved a service into/out of the window, return a warning about incomplete results.

Example contract (illustrative identifiers/times):

```json
{
  "stop_id": "example-stop",
  "generated_at": "2026-10-04T13:00:00Z",
  "timezone": "Europe/Dublin",
  "data_mode": "live",
  "source_health": {"trip_updates": "fresh"},
  "departures": [{
    "trip_id": "example-trip",
    "route_id": "example-route",
    "route_name": "Example service",
    "destination": "City centre",
    "scheduled_at": "2026-10-04T13:08:00Z",
    "predicted_at": "2026-10-04T13:11:00Z",
    "status": "live",
    "evidence_at": "2026-10-04T12:59:15Z",
    "evidence_age_seconds": 45,
    "explanation": "Fresh stop-level prediction"
  }],
  "warnings": []
}
```

Use `scheduled-only` with `predicted_at: null` when evidence is absent or stale; retain stale evidence separately if useful. Return explicit unknown-stop and unavailable-static-data errors. An empty list is only “no services found in this window,” not evidence of a shutdown.

### `get_route_reliability(route_id: str, lookback_hours: int = 2)`

Calculate scheduled trip instances whose full monitoring window lies inside your collection history. Proposed window: 10 minutes before scheduled first departure through 10 minutes after scheduled final arrival. Count only instances for which fresh, successfully matched polls cover at least 90% of expected sample slots, with no unexplained gap longer than twice the configured cadence. These are adjustable heuristics, not proof of operation.

Return separate counts for eligible scheduled instances, observed instances, explicitly cancelled instances, unobserved instances, and excluded instances. Use disjoint accounting categories, with explicit cancellations classified first. Include collection start/end, coverage, match rate, source/version, and thresholds.

```text
live_visibility_rate = observed_non_cancelled / eligible_non_cancelled
```

Return null when the denominator is zero or below the proposed 20-trip minimum. Mark cancellations as explicit feed evidence, and label unobserved trips “operation unconfirmed.” Trips seen in vehicle positions and trip updates should count once. Do not score future trips or trips whose monitoring window has not closed. On a fresh hackathon database, “insufficient history” is the expected honest result.

**Done when:** all tools work through an actual MCP client and disclose source freshness/replay mode without requiring the assistant to infer it.

## 9. Meaningful verification

Use tiny GTFS archives and deterministic real-time fixtures; inject time rather than sleeping. Run the tests below before expanding scope.

| Fixture/scenario | Required result |
| --- | --- |
| Calendar removal/addition | Correct active services |
| Previous-day 25:10 and DST transition | Correct service date and UTC instant |
| Absent protobuf prediction fields | No fabricated zero/epoch ETA |
| Fresh prediction vs timetable | Correct time, status, and provenance |
| Cancellation, skipped stop, NO_DATA | Distinct correct behaviour |
| Vehicle position without stop prediction | No invented ETA |
| Old source timestamp fetched now | Stale/unknown, never fresh |
| Successful empty snapshot | Current entries removed; history retained |
| Duplicate/out-of-order snapshot | No duplicate history or freshness rollback |
| API outage or ID mismatch | Visibility scoring withheld |
| Same trip ID on two dates | Independent instances |
| Collection begins midway through a trip | Excluded from scoring |
| Tiny denominator / replay fixture | Insufficient history / explicit replay label |

Run `python -m pytest` after creating the test suite. Then manually connect an MCP client, search the demo stop, request departures, request visibility, and stop the poller to verify stale-data behaviour. Verify concurrent reads while the poller commits.

## 10. Four-hour delivery order

| Time from build start | Work | Exit criterion |
| --- | --- | --- |
| 0:00–0:20 | Credentials, matching static/live feed, first capture | Live path works or labelled replay chosen |
| 0:20–1:10 | Scaffold, schema, static import, service-time functions | Deterministic scheduled departures |
| 1:10–1:55 | Poller, normalization, snapshot retention | Fresh predictions and health metadata |
| 1:55–2:40 | Stop search + departures MCP tools | End-to-end client interaction |
| 2:40–3:10 | Visibility tool + edge-case tests | Honest scoring or insufficient-history response |
| 3:10–3:35 | Minimal dashboard or readable client presentation | Status labels and freshness visible |
| 3:35–4:00 | Freeze features, rehearsal, submission | Repeatable demo with fallback |

If behind schedule, cut the dashboard and scored visibility before cutting freshness labels or the working MCP interaction. Preserve the reliability tool with an honest insufficient-data response.

## 11. Demo and outage fallback

A 90-second demo:

1. Ask for the next departure from a verified stop. Show stop disambiguation if relevant.
2. Show a live estimate alongside a timetable-only entry; explain the evidence timestamp.
3. Ask about route visibility. Show the observation window and denominator, or explain that there is not enough history yet.
4. Switch to a prepared outage fixture and show stale evidence explicitly.
5. Finish with the differentiator: the assistant reports what the source supports and tells you when it cannot verify service.

Use a recorded payload replay with a deterministic simulated clock, or synthetic fixtures if permitted recordings are unavailable. Include `data_mode: replay` or `synthetic` in every response and a visible banner. Store original capture time separately from simulated time. Do not silently re-date recordings to make them look live.

The plan’s “plain ChatGPT guess” comparison should be a clearly marked illustrative answer unless you actually capture that baseline. Avoid claiming another product always guesses.

## 12. Dashboard and deployment, only after the MVP

Build a single page backed by the same query functions: stop search, departure list, source health, and route visibility with sample counts. Rank routes only when each meets comparable history/coverage requirements; otherwise show an empty state explaining collection is underway.

For Fly.io or EC2, use one host with a persistent local SQLite volume and exactly one poller. SQLite WAL requires processes sharing the same local filesystem; do not spread readers/writers across independent instances or network-mounted storage. Ensure restart policies, persistent snapshots, graceful shutdown, and a health check showing last successful source timestamps. Use TLS and protect access before wider exposure. Back up with SQLite’s supported backup mechanism.

Deployment acceptance: restart the application and verify history survives, tools remain reachable, and only one quota-aware poller is running.

## 13. Extend M3–M4 after the hackathon

- **Rail/Luas:** verify official endpoints, licences, identifiers, and freshness first. Put each behind an adapter returning the same evidence fields; never merge source IDs without a namespace.
- **Bikes:** verify a currently available GBFS feed; join station_information and station_status, expose availability age, and calculate distance for `get_bikes(lat, lon)`.
- **Planner:** begin with direct journeys, then one transfer using service-day-aware times, walking estimates, and minimum interchange times. Label timetable vs live portions independently. Visibility is not a valid substitute for a calibrated journey reliability model.
- **Fares:** add verified, dated fare rules and eligibility. Return “fare unknown” when unsupported rather than inferring a student/adult price. This is a separate data-modelling task.
- **Actual reliability:** collect more history, distinguish feed coverage from operation, establish validated arrival observations, and publish the method and limitations before claiming ghost buses or route punctuality.

## 14. Submission checklist

- [ ] One verified stop/route works through an MCP client.
- [ ] Current timetable version and supported operator scope are visible.
- [ ] Timetable-only, cancelled, skipped, fresh, and stale states are distinguishable.
- [ ] Visibility output includes denominator, history, exclusions, and limitations.
- [ ] Missing data is never claimed as a confirmed no-show.
- [ ] API failure/replay demo is repeatable and clearly labelled.
- [ ] No upstream request occurs per tool call; secrets stay private.
- [ ] README includes actual setup commands, client connection details, and data attribution.
- [ ] Required tests pass and the demo has been rehearsed.

## Sources and verification boundaries

- [Original event brief and schedule](https://luma.com/build-for-ireland).
- [NTA production developer portal](https://developer.nationaltransport.ie/): discover current APIs, registration, and static-feed links.
- [NTA UAT fair usage policy](https://developeruat.ntaapptest.transportforireland.ie/usagepolicy): reference only; production account limits remain to be verified.
- [GTFS Schedule reference](https://gtfs.org/documentation/schedule/reference/): service calendars, IDs, time semantics, and files.
- [GTFS Trip Updates](https://gtfs.org/documentation/realtime/feed-entities/trip-updates/): absent evidence, predictions, skipped stops, and delay propagation.
- [FastMCP running-server documentation](https://github.com/PrefectHQ/fastmcp/blob/main/docs/deployment/running-server.mdx): HTTP transport invocation.

No authenticated NTA request, compatible archive, production quota, or installed Python dependency version was verified while writing this guide. The access gate explicitly resolves these before implementation depends on them.
