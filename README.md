# Build4Ireland MCP (local Dublin bus demo)

This first MCP server searches Dublin bus stops and returns timetable departures
from the local NTA GTFS database. Until the realtime poller is running, results
are marked `scheduled-only` and the realtime state is `not_connected`. A timetable
entry is not evidence that a bus is currently running.

## Start locally

Python 3.11 or newer is required.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

If the local database has not been loaded yet, import the free static timetable:

```sh
.venv/bin/python gtfs_loader.py
```

Start the MCP server:

```sh
.venv/bin/python mcp_server.py
```

The streamable HTTP MCP endpoint is `http://127.0.0.1:8000/mcp`. Override the
host, port, or path with `MCP_HOST`, `MCP_PORT`, or `MCP_PATH`. The database path
can be changed with `TRANSPORT_DB_PATH`.

## Tools

- `find_stops(query, limit=8)`: fuzzy-searches stops served by Dublin Bus,
  Go-Ahead Ireland, or Nitelink and returns the GTFS stop IDs.
- `get_departures(stop_id, route_id=null, limit=10, window_minutes=120)`: returns
  upcoming Dublin bus timetable entries, status, source freshness, and feed
  import time. It supports overnight GTFS service times.

Example sequence: call `find_stops("Donnybrook")`, choose a returned `stop_id`,
then pass it to `get_departures`.

## Connect ChatGPT

ChatGPT connects to remote MCP servers. For a local development server, follow
OpenAI's [Secure MCP Tunnel setup](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
to connect the local endpoint without exposing it publicly. Configure the MCP
endpoint shown above.

The static NTA timetable requires no API key. The realtime poller is a separate
process and will require NTA API credentials when it is added.
