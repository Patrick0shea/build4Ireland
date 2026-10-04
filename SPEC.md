# Build4Ireland — Hackathon MVP Spec

## Purpose

Build an MCP server that lets an LLM answer public transport questions across Ireland using real timetable and realtime data. The central product is reliable, evidence-backed answers with clear source, freshness, and coverage information. A dashboard is optional and secondary.

## User and main flow

**User:** a traveller asking an AI assistant about a bus departure or a route's recent reliability.

**Main demo:** ask an assistant for the best way to make a journey or for the next departure from a known stop. The assistant calls MCP tools, which return available options based on the relevant modes and sources, with timestamps and explicit evidence/coverage status. Answers must make it clear when they rely on a timetable, realtime observation, or incomplete source coverage.

## Product scope

The intended product covers public and shared transport across Ireland, including bus, Irish Rail, Luas, and shared bikes, with trip planning and fare comparison where source data supports them. The architecture should support adding operators and modes without changing the LLM-facing interface. Nationwide and multimodal coverage is the target; actual coverage must be reported from the sources integrated and available at query time.

The hackathon is a first delivery step, not the product boundary. Prioritize integrations and MCP tools that make the end-to-end answer accurate. Implement as many useful source adapters as time and access permit, and make unsupported modes/areas explicit. A polished public dashboard, user accounts, and production deployment are lower priority than correct MCP responses and reliable data handling.

## MVP scope

### In scope

- Python service exposing MCP tools using FastMCP.
- SQLite database in WAL mode, shared by the poller and MCP service.
- Transport-source adapter pattern that can serve multiple modes/operators through common journey and departure concepts.
- Import of NTA static GTFS and polling of its GTFS-Realtime feed as the initial integration, with Irish Rail, Luas, and shared-bike adapters as prioritized extensions when access and time allow.
- Background polling at a configurable interval (default 60 seconds for GTFS-Realtime); persist timestamped observations and source metadata.
- `find_stops(query)`: fuzzy stop-name search returning stable stop IDs, names, and coordinates.
- `get_departures(stop_id, route_id?, mode?, limit?)`: upcoming departures across supported sources, with scheduled time, realtime evidence when matched, status, source, and feed freshness.
- `plan_trip(origin, destination, prefs?, traveller_profile?)`: feasible journeys across supported modes, initially direct and one transfer; rank by fastest, cheapest, or best-supported/reliable where data permits.
- `get_route_reliability(route_id, window_hours?)`: when a source has suitable historical data, return scheduled and observed counts, sample window, data coverage, and qualified not-observed metrics.
- Fare comparison when fare data is available for the relevant modes and traveller profile.
- A short setup guide, sample questions, and a demo path that can run from cached data if an upstream feed is unavailable.

### Lower priority for the hackathon MVP

- A polished public dashboard, user accounts, and production-grade public deployment.
- Full coverage of every operator, route, and fare on day one. The product must be designed toward this; responses must say what is and is not covered.

Do not hide or overstate data limitations. A missing realtime record alone cannot establish that a scheduled trip did not operate. Report it as **not observed in this source/feed**, alongside source freshness and coverage; use stronger language only when supported by explicit source data (such as a cancellation).

## Tool response contract

Use JSON-serializable objects with stable field names. Include source/operator, mode, `as_of` (UTC timestamp), and source/feed freshness and coverage in responses. Separate timetable data from realtime observations so an LLM can explain what backs each answer. Return an empty result with an explanation when the stop, route, or requested coverage is unavailable; do not invent a result.

### Departure status

- `live`: a scheduled departure has a confidently matched realtime update or vehicle observation.
- `scheduled-only`: the timetable has a departure, but no matching realtime evidence was found. This does not mean cancelled or missing in reality.
- `cancelled`: only when an explicit realtime cancellation is matched to the scheduled trip.

Every departure includes `stop_id`, `route_id`, `trip_id`, `scheduled_departure`, `realtime_departure` (nullable), `status`, and relevant update timestamp. Times are ISO 8601 with timezone offset in tool output; store timestamps in UTC. Preserve GTFS service times after midnight (such as 24:30) as service-day offsets when importing and resolving trips.

Reliability results must state the observation window, source coverage, and denominator. Distinguish “not observed in feed” from “did not run”; do not present the latter as a fact. For trip planning, do not imply a mode/operator was checked if its adapter is unavailable or stale.

## Data and matching rules

- Poll upstream centrally and serve from the local database; MCP requests must not make upstream calls.
- Keep raw/normalized feed snapshots or enough timestamped records to reproduce what the poller saw.
- Match realtime records to static trips using the GTFS trip identifiers and service date. Record unmatched realtime items for diagnosis rather than silently discarding them.
- Detect stale or unavailable feeds and surface that state to tools. A lack of fresh data must not be reported as a healthy feed or as route unreliability.
- Keep the data source, fetch time, and any normalization needed to interpret each observation.

## Architecture and module boundaries

- `sources/<mode_or_operator>`: source-specific clients and normalization into shared transport records.
- `pollers`: fetch and persist timestamped realtime observations.
- `storage`: schema, connections, and read/write operations.
- `tools`: stop/place search, departure lookup, trip planning, and reliability calculation.
- `server`: FastMCP registration and process entry point.
- `demo` or documentation: deterministic sample data/path, source coverage notes, and demo script.

Keep storage and normalization functions callable without starting the MCP server so both tools and poller can be developed and checked independently.

## Two-person work split

Agree the schema and Python interfaces before parallel implementation. Keep ownership separated by module and integrate through a small shared contract.

| Workstream | Primary owner | Deliverables |
|---|---|---|
| Data foundation and source adapters | Person A | Shared transport data model and SQLite schema/storage; NTA GTFS/GTFS-Realtime integration; additional operator adapters as prioritized together; freshness and coverage metadata |
| MCP tools and LLM answer quality | Person B | FastMCP server; stop/departure/trip-planning tools; source-aware response formatting; setup/demo documentation |
| Joint integration | Both | Agree shared model and source contract early; connect one end-to-end journey/departure flow; add further modes in parallel; rehearse live and cached paths |

Suggested integration checkpoints: first agree names/types for modes, operators, stops/places, routes, scheduled journeys, realtime observations, fares, and source coverage; then land one working source adapter and an MCP departure result; then connect trip planning and additional adapters; finally validate the full demo across the available modes. Avoid both people editing the same module at once; coordinate shared changes to schema and tool contracts before editing them.

## Demo acceptance criteria

The MVP is ready to present when:

1. A fresh setup can import static GTFS and start the server using documented commands and configuration.
2. `find_stops` can find a known stop and return its stable ID and mode/operator context where available.
3. `get_departures` returns upcoming departures from the integrated sources and labels realtime evidence, timetable-only results, explicit cancellations, source, and freshness accurately.
4. `plan_trip` returns a feasible journey for the demo across the modes supported by the integrated data, or explains which requested coverage is unavailable.
5. Reliability results, when available, explain their sample window, source coverage, observed count, denominator, and limits.
6. The assistant's answer is grounded in returned data, indicates stale/missing coverage, and does not claim an unobserved trip was cancelled or never ran.
7. If a live source is unavailable, the same flow can use a clearly labelled cached or fixture snapshot and disclose its timestamp.

## Configuration and operational expectations

- Keep API keys and local secrets in environment variables; provide an example environment file containing placeholders only.
- Make source endpoints, polling intervals, database path, and static-feed sources configurable.
- Log fetch failures and parsing/matching counts. Retry transient failures without blocking MCP tool calls.
- Document the NTA key prerequisite, initial import, startup, and how to refresh or use demo data.

## Decisions to confirm during implementation

- Which operator/mode source can be integrated next after NTA, based on access, data quality, and time.
- For each source, what evidence counts as a realtime observation and how it matches a scheduled service.
- Which trip-planning preferences and fare profiles have sufficiently complete data to support accurate results.
- Whether the chosen MCP client connects over stdio or streamable HTTP for the demo environment.
