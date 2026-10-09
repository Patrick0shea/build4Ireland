# Honest Live Transport Ireland

[![tests](https://github.com/Patrick0shea/build4Ireland/actions/workflows/tests.yml/badge.svg)](https://github.com/Patrick0shea/build4Ireland/actions/workflows/tests.yml)

An MCP server that gives an AI assistant real Irish public transport data, and
tells it how much to trust each answer.

Built in a day at [Build for Ireland](https://luma.com/build-for-ireland)
(4 October 2026) by [Patrick O'Shea](https://github.com/Patrick0shea) and
Harry Kennedy.

![Example transport assistant response for a Limerick-to-Dublin query, distinguishing a scheduled-only coach departure from the overall realtime feed status.](docs/images/transit-assistant-demo.png)

*Example response from prototype testing. The feed can be fresh overall while a
specific departure stays scheduled-only when no matching realtime observation
is available.*

## The problem

Ask a chatbot "when's the next bus from this stop?" and it will guess. Transport
apps show the timetable, but don't always say whether a time is a live
prediction or a schedule. This project connects an assistant to the National
Transport Authority's timetable and realtime feeds, and labels every departure
by the evidence behind it:

| Status | Meaning |
|---|---|
| `live` | A fresh realtime update matches this scheduled trip. |
| `scheduled-only` | The timetable has it, but there's no matching realtime evidence. This does **not** mean it was cancelled. |
| `cancelled` | The realtime feed explicitly cancels this trip. |

Every response also reports when the feed was last updated, so the assistant
can say "this is the timetable, the live feed is 20 minutes stale" instead of
inventing a prediction.

## What it does

- Searches stops across NTA bus, Irish Rail, and Luas services (fuzzy name
  matching, optional mode filter).
- Returns upcoming departures with optional mode, route, and operator filters,
  including realtime evidence when a fresh observation matches.
- Polls the NTA GTFS-Realtime feeds in the background and keeps a timestamped
  history, so missing trips are recorded rather than silently dropped.
- Includes a recorded snapshot of real Dublin data that replays without an API
  key.

It does not plan multi-leg journeys, compare fares, or include Dublin Bikes.
Those were in the [original plan](docs/planning/) but not built on the day.

## How it works

```text
NTA static GTFS ──► gtfs_loader.py ─────┐
                                        ▼
NTA GTFS-Realtime ─► realtime_poller.py ─► SQLite (WAL) ◄── mcp_server.py ◄── AI assistant
                                                              (find_stops, get_departures)
```

The loader and poller write to one SQLite database. The MCP server only reads
from it and never calls upstream APIs per request, so it answers quickly and
keeps working if a feed goes down. WAL mode lets the poller write while the
server reads.

| File | Purpose |
|---|---|
| `gtfs_loader.py` | Downloads and imports the national NTA GTFS timetable. |
| `realtime_poller.py` | Polls trip updates and vehicle positions; records seen, missing, and cancelled trips. |
| `mcp_tools.py` | Stop search and departure logic, including realtime matching and freshness. |
| `mcp_server.py` | FastMCP server exposing the tools over Streamable HTTP. |
| `data_health.py` | Read-only report on feed freshness and ID matching. |
| `offline_demo.py` | Capture and replay a recorded data snapshot. |
| `db.py`, `db/schema.sql` | Shared database connection and schema. |

## Run it

Python 3.11 or newer is required.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gtfs_loader.py      # import the timetable (no API key needed)
.venv/bin/python mcp_server.py       # serves MCP at http://127.0.0.1:8000/mcp
```

Connect any MCP client to that endpoint and ask, for example:

- "Find Dublin Heuston and show me the next three rail departures."
- "Find bus stops near Rathmines and show the serving operators."
- "Are these departures based on fresh realtime data or the timetable?"

For live updates, get NTA API keys, copy `.env.example`, and start the poller
as described in [docs/POLLER.md](docs/POLLER.md). ChatGPT's web app needs a
remote connection path for a local server; see the
[Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

### Without an API key

Replay a recorded snapshot of real Dublin route 27 data into a new database:

```sh
.venv/bin/python offline_demo.py replay --db data/demo.sqlite3
TRANSPORT_DB_PATH=data/demo.sqlite3 .venv/bin/python mcp_server.py
```

Replayed data is labelled as a historical replay with its recorded timestamp.
It is never presented as live.

### Tests

```sh
.venv/bin/python -m pip install -r requirements-poller.txt
.venv/bin/python -m unittest discover -s tests
```

## Documentation

- [docs/DATABASE.md](docs/DATABASE.md): schema and data contract.
- [docs/POLLER.md](docs/POLLER.md): realtime setup and data checks.
- [docs/MCP_HANDOFF.md](docs/MCP_HANDOFF.md): query contracts, coverage, and the offline replay.
- [docs/planning/](docs/planning/): the hackathon plan, spec, and implementation guide.

## Data and limitations

The static timetable is the national NTA GTFS feed. Realtime data comes from
the NTA GTFS-Realtime endpoints, which need subscription keys. Realtime data
may be delayed, unavailable, or stale; when it is, the assistant should give
timetable times rather than call them live. A trip missing from the feed is not
proof that it didn't run.

NTA data is used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
