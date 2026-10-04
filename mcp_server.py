"""FastMCP entry point for the local Dublin bus timetable demo."""

from __future__ import annotations

import os

from fastmcp import FastMCP

import db
from mcp_tools import find_stops as find_stops_tool
from mcp_tools import get_departures as get_departures_tool


mcp = FastMCP(
    "Honest Live Transport Ireland",
    instructions=(
        "Answer transport questions only from tool results. These tools currently "
        "cover Dublin bus timetable data. Always state whether realtime data is "
        "fresh, stale, unavailable, or not connected. 'scheduled-only' means the "
        "timetable lists a trip but the current realtime feed did not provide a "
        "matching observation; it does not mean the bus is cancelled or will not run. "
        "Do not claim reliability until historical observations are available."
    ),
)


@mcp.tool(description=(
    "Find Dublin bus stops by fuzzy name. Use this first when the user gives a "
    "place or stop name instead of a GTFS stop ID. Returns stable stop IDs, "
    "coordinates, match scores, and example serving routes."
))
def find_stops(query: str, limit: int = 8) -> dict:
    return find_stops_tool(query, limit)


@mcp.tool(description=(
    "Get upcoming scheduled Dublin bus departures for a GTFS stop ID, optionally "
    "filtered by route. Report each status honestly: live only when there is a "
    "fresh matching realtime observation; cancelled only on explicit cancellation; "
    "otherwise scheduled-only. Check the returned realtime state and timetable "
    "import timestamp before answering. A missing feed observation does not prove "
    "a bus will not run."
))
def get_departures(stop_id: str, route_id: str | None = None, limit: int = 10, window_minutes: int = 120) -> dict:
    return get_departures_tool(stop_id, route_id, limit, window_minutes)


def main() -> None:
    db.initialize()
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))
    path = os.environ.get("MCP_PATH", "/mcp")
    mcp.run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
