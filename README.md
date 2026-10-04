# Build4Ireland MCP (local NTA public transport demo)

This MCP server searches stops and returns timetable departures for bus, Irish Rail,
and Luas services in the locally loaded national NTA GTFS database. Until the realtime poller is running, results
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

- `find_stops(query, limit=8, mode=null)`: fuzzy-searches supported NTA stops;
  optional mode is `bus`, `rail`, or `tram` (`Luas`). Results include operators.
- `get_departures(stop_id, route_id=null, limit=10, window_minutes=120,
  mode=null, operator=null)`: returns upcoming national bus, rail, or tram
  timetable entries, optionally filtered by mode/operator, with realtime status
  and source freshness. It supports overnight GTFS service times.

Example sequence: call `find_stops("Heuston", mode="rail")`, choose a returned
`stop_id`, then pass it to `get_departures(mode="rail")`. Omit `mode` to search
all supported modes at a stop. Other bus operators in the national timetable
are included and may be scheduled-only when no matching realtime observation is
available. Modes without NTA GTFS routes, such as Dublin Bikes, require a separate
source integration.

## Connect ChatGPT

ChatGPT connects to remote MCP servers. For a local development server, follow
OpenAI's [Secure MCP Tunnel setup](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
to connect the local endpoint without exposing it publicly. Configure the MCP
endpoint shown above.

The static NTA timetable requires no API key. Run the realtime poller as a
separate process with the two subscription tokens described in `POLLER.md`.
Each feed is fetched once per minute using a separate NTA token.
