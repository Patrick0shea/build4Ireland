# Honest Live Transport MCP Server for Ireland — Plan

## Problem
Ask an LLM "when's the next 46A from Donnybrook?" and it guesses. Existing apps show what the timetable says, not whether the bus will actually turn up.

## Idea
An MCP server that gives LLMs accurate, live access to Irish public transport data (buses, trains, Luas, Dublin Bikes) across all of Ireland. What makes it different: it is **honest about data quality**. It logs the live feed, detects ghost buses (scheduled trips that never appear in the live feed), and warns users about unreliable routes.

---

## Milestones

### M1: Foundation — Static GTFS + Poller *(start here)*
- Project scaffold: Python, FastMCP, SQLite WAL, `pyproject.toml`
- Static GTFS downloader: fetches NTA zip, loads routes/stops/trips/stop_times/calendar into SQLite
- Background GTFS-Realtime poller: hits NTA RT endpoint every 60s, stores `trip_updates` and `vehicle_positions` with timestamps
- Schema designed for ghost-bus detection from day one (trips scheduled vs. trips seen live)

### M2: Core MCP Tools
- `find_stops(query)` — fuzzy name search via rapidfuzz, returns stop_id + coordinates
- `get_departures(stop_id, modes?)` — next departures tagged `live`, `scheduled-only`, or `cancelled`
- `get_route_reliability(route_id)` — compares scheduled trips to observed trips, flags ghost buses

### M3: More Data Sources
- Irish Rail realtime XML (no key needed)
- Luas forecasts API
- Dublin Bikes via Smart Dublin GBFS
- `get_bikes(lat, lon)` tool

### M4: Trip Planner + Fares
- `plan_trip(origin, destination, prefs, traveller_profile)` — direct + one transfer, optimised by fastest/cheapest/most-reliable
- Fare comparison (student vs adult)

### M5: Dashboard + Deployment
- One-page reliability dashboard (today's worst routes)
- Fly.io deployment config, caching layer so demo survives API downtime
- Demo script (plain ChatGPT guess vs. server answer)

---

## Data Sources
- NTA GTFS static timetable + GTFS-Realtime (free API key required)
- Irish Rail realtime API (XML, no key)
- Luas forecasts API
- Dublin Bikes via JCDecaux or Smart Dublin GBFS

## Technical Shape
- Python, FastMCP, SQLite (WAL mode), httpx, gtfs-realtime-bindings, rapidfuzz
- Two processes sharing one DB: a **poller** (fetches GTFS-R every 60s) and the **MCP server** (streamable HTTP)
- Poll centrally, serve from cache — never call upstream APIs per request
- Deployable to Fly.io or EC2

## Key Gotchas
- GTFS times past midnight (24:30, 25:10) — parse as offsets, not clock times
- Store UTC, display in Europe/Dublin
- Static GTFS trip IDs must match live feed or everything looks like a ghost bus
- Cache snapshots so demo survives a live API failure
