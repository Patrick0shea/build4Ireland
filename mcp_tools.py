"""Read-only MCP tool logic backed by the local NTA static GTFS database."""

from __future__ import annotations

from contextlib import closing
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz, process, utils

import db


DUBLIN_TZ = ZoneInfo("Europe/Dublin")
MODE_ROUTE_TYPES = {"tram": (0,), "rail": (2,), "bus": (3,)}
MODE_ALIASES = {"luas": "tram", "train": "rail", "irish rail": "rail", "trains": "rail", "buses": "bus"}
SUPPORTED_ROUTE_TYPES = tuple(sorted({value for values in MODE_ROUTE_TYPES.values() for value in values}))


def _mode_name(route_type: int) -> str:
    return {0: "tram", 2: "rail", 3: "bus"}.get(route_type, "other")


def _selected_route_types(mode: str | None) -> tuple[int, ...]:
    if mode is None or not mode.strip():
        return SUPPORTED_ROUTE_TYPES
    normalised = " ".join(mode.lower().split())
    normalised = MODE_ALIASES.get(normalised, normalised)
    if normalised not in MODE_ROUTE_TYPES:
        raise ValueError("mode must be bus, rail, or tram (Luas)")
    return MODE_ROUTE_TYPES[normalised]


def _metadata(connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in connection.execute(
        "SELECT key, value FROM schema_metadata WHERE key IN ('gtfs_imported_at_utc', 'gtfs_archive_sha256')"
    )}


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dublin_service_datetime(service_day: date, service_seconds: int) -> datetime:
    midnight = datetime.combine(service_day, time.min, tzinfo=DUBLIN_TZ)
    return midnight + timedelta(seconds=service_seconds)


def _active_services(connection, service_day: date) -> set[str]:
    day_name = service_day.strftime("%A").lower()
    day_text = service_day.isoformat()
    active = {
        row["service_id"]
        for row in connection.execute(
            f"SELECT service_id FROM calendar WHERE {day_name}=1 AND start_date<=? AND end_date>=?",
            (day_text, day_text),
        )
    }
    for row in connection.execute(
        "SELECT service_id, exception_type FROM calendar_dates WHERE date=?", (day_text,)
    ):
        if row["exception_type"] == 1:
            active.add(row["service_id"])
        else:
            active.discard(row["service_id"])
    return active


def _realtime_state(connection, now_utc: datetime) -> tuple[dict[str, Any], int | None]:
    latest_attempt = connection.execute(
        "SELECT snapshot_id, fetched_at_utc, fetch_status, error_message "
        "FROM feed_snapshots ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    if latest_attempt is None:
        return ({"state": "not_connected", "last_attempt_utc": None, "last_success_utc": None}, None)

    latest_success = connection.execute(
        "SELECT snapshot_id, fetched_at_utc, feed_timestamp_utc, raw_feed FROM feed_snapshots "
        "WHERE fetch_status='success' ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    last_success_time = None
    if latest_success:
        try:
            last_success_time = datetime.fromisoformat(latest_success["fetched_at_utc"])
            if last_success_time.tzinfo is None:
                last_success_time = last_success_time.replace(tzinfo=timezone.utc)
        except ValueError:
            last_success_time = None

    last_attempt = latest_attempt["fetched_at_utc"]
    source_ages: dict[str, float] = {}
    feed_timestamp = None
    if latest_success:
        feed_timestamp = latest_success['feed_timestamp_utc']
        raw = latest_success['raw_feed']
        try:
            envelope = json.loads(raw) if raw and raw.lstrip().startswith(b'{') else {}
            sources = envelope.get('poller_sources', {})
            if sources:
                source_ages = {name: (now_utc - datetime.fromtimestamp(item['timestamp'], timezone.utc)).total_seconds()
                               for name, item in sources.items()}
            elif feed_timestamp:
                parsed_feed_time = datetime.fromisoformat(feed_timestamp)
                if parsed_feed_time.tzinfo is None:
                    parsed_feed_time = parsed_feed_time.replace(tzinfo=timezone.utc)
                source_ages = {'feed': (now_utc - parsed_feed_time.astimezone(timezone.utc)).total_seconds()}
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            source_ages = {}

    if latest_attempt["fetch_status"] != "success":
        state = "unavailable"
    elif not source_ages:
        state = "stale"
    elif all(-30 <= age <= 120 for age in source_ages.values()):
        state = "fresh"
    else:
        state = "stale"

    result = {
        "state": state,
        "last_attempt_utc": last_attempt,
        "last_success_utc": _iso_utc(last_success_time) if last_success_time else None,
        "feed_timestamp_utc": feed_timestamp,
        "source_age_seconds": {name: round(age, 1) for name, age in source_ages.items()},
    }
    if latest_attempt["error_message"]:
        result["last_error"] = latest_attempt["error_message"]
    usable_snapshot_id = (
        latest_success["snapshot_id"]
        if state == "fresh" and latest_success and latest_success["snapshot_id"] == latest_attempt["snapshot_id"]
        else None
    )
    return result, usable_snapshot_id


def find_stops(query: str, limit: int = 8, mode: str | None = None) -> dict[str, Any]:
    """Fuzzy-search stops served by supported NTA bus, rail, and tram routes."""
    query = " ".join(query.split())
    if len(query) < 2:
        return {"query": query, "stops": [], "message": "Enter at least two characters."}
    limit = max(1, min(int(limit), 15))
    try:
        route_types = _selected_route_types(mode)
    except ValueError as error:
        return {"query": query, "stops": [], "message": str(error)}
    now_utc = datetime.now(timezone.utc)

    with closing(db.connect()) as connection:
        metadata = _metadata(connection)
        rows = connection.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops WHERE stop_name<>''"
        ).fetchall()
        rows_by_id = {row["stop_id"]: row for row in rows}
        choices = {stop_id: row["stop_name"] for stop_id, row in rows_by_id.items()}
        matches = process.extract(
            query,
            choices,
            scorer=fuzz.WRatio,
            processor=utils.default_process,
            limit=min(len(rows), limit * 8),
            score_cutoff=40,
        )
        stops = []
        for _name, score, stop_id in matches:
            row = rows_by_id[stop_id]
            type_placeholders = ",".join("?" for _ in route_types)
            service = connection.execute(
                f"""SELECT DISTINCT r.route_short_name, r.route_long_name, r.route_type, a.agency_name
                    FROM stop_times st
                    JOIN trips t ON t.trip_id=st.trip_id
                    JOIN routes r ON r.route_id=t.route_id
                    LEFT JOIN agencies a ON a.agency_id=r.agency_id
                    WHERE st.stop_id=? AND r.route_type IN ({type_placeholders})
                    ORDER BY r.route_type, r.route_short_name LIMIT 20""",
                (stop_id, *route_types),
            ).fetchall()
            if not service:
                continue
            stops.append({
                "stop_id": stop_id,
                "stop_name": row["stop_name"],
                "latitude": row["stop_lat"],
                "longitude": row["stop_lon"],
                "match_score": round(float(score), 1),
                "served_by": [
                    {"route": item["route_short_name"], "route_name": item["route_long_name"],
                     "operator": item["agency_name"], "mode": _mode_name(item["route_type"])}
                    for item in service
                ],
            })
            if len(stops) >= limit:
                break
    return {
        "query": query,
        "stops": stops,
        "as_of": _iso_utc(now_utc),
        "source": "NTA static GTFS",
        "static_feed_imported_at_utc": metadata.get("gtfs_imported_at_utc"),
        "coverage": "Stops served by bus, Irish Rail, or Luas routes in the loaded national NTA timetable.",
        "message": None if stops else "No matching supported public transport stops were found in the loaded timetable.",
    }


def get_departures(
    stop_id: str,
    route_id: str | None = None,
    limit: int = 10,
    window_minutes: int = 120,
    mode: str | None = None,
    operator: str | None = None,
) -> dict[str, Any]:
    """Return upcoming national NTA bus, rail, or tram departures and evidence status."""
    limit = max(1, min(int(limit), 25))
    window_minutes = max(15, min(int(window_minutes), 24 * 60))
    try:
        route_types = _selected_route_types(mode)
    except ValueError as error:
        return {"stop_id": stop_id, "departures": [], "message": str(error)}
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(DUBLIN_TZ)
    end_utc = now_utc + timedelta(minutes=window_minutes)
    candidates: list[dict[str, Any]] = []

    with closing(db.connect()) as connection:
        connection.create_function("unicode_casefold", 1, lambda value: value.casefold() if value else "")
        stop = connection.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops WHERE stop_id=?", (stop_id,)
        ).fetchone()
        metadata = _metadata(connection)
        if stop is None:
            return {
                "stop_id": stop_id,
                "departures": [],
                "as_of": _iso_utc(now_utc),
                "message": "This stop ID is not present in the loaded timetable.",
            }

        services_by_day: list[tuple[date, set[str], int, int]] = []
        first_day = now_local.date() - timedelta(days=1)
        last_day = end_utc.astimezone(DUBLIN_TZ).date()
        service_day = first_day
        while service_day <= last_day:
            service_start_utc = datetime.combine(service_day, time.min, tzinfo=DUBLIN_TZ).astimezone(timezone.utc)
            lower_secs = max(0, int((now_utc - service_start_utc).total_seconds()))
            upper_secs = int((end_utc - service_start_utc).total_seconds())
            if upper_secs >= 0:
                services_by_day.append((service_day, _active_services(connection, service_day), lower_secs, upper_secs))
            service_day += timedelta(days=1)

        for active_day, service_ids, lower_secs, upper_secs in services_by_day:
            if not service_ids:
                continue
            placeholders = ",".join("?" for _ in service_ids)
            type_placeholders = ",".join("?" for _ in route_types)
            filters = [f"t.service_id IN ({placeholders})", "st.stop_id=?",
                       "st.departure_secs BETWEEN ? AND ?", f"r.route_type IN ({type_placeholders})"]
            params: list[Any] = list(sorted(service_ids)) + [stop_id, lower_secs, upper_secs]
            params.extend(route_types)
            if route_id:
                filters.append("r.route_id=?")
                params.append(route_id)
            if operator:
                filters.append("unicode_casefold(a.agency_name)=unicode_casefold(?)")
                params.append(operator.strip())
            rows = connection.execute(
                f"""SELECT st.trip_id, st.departure_secs, t.service_id, t.trip_headsign,
                           r.route_id, r.route_short_name, r.route_long_name, r.route_type, a.agency_name
                    FROM stop_times st
                    JOIN trips t ON t.trip_id=st.trip_id
                    JOIN routes r ON r.route_id=t.route_id
                    LEFT JOIN agencies a ON a.agency_id=r.agency_id
                    WHERE {' AND '.join(filters)}
                    ORDER BY st.departure_secs LIMIT 300""",
                params,
            ).fetchall()
            for row in rows:
                scheduled_local = _dublin_service_datetime(active_day, row["departure_secs"])
                candidates.append({
                    "stop_id": stop_id,
                    "route_id": row["route_id"],
                    "mode": _mode_name(row["route_type"]),
                    "route": row["route_short_name"],
                    "route_name": row["route_long_name"],
                    "operator": row["agency_name"],
                    "trip_id": row["trip_id"],
                    "headsign": row["trip_headsign"],
                    "service_date": active_day.isoformat(),
                    "scheduled_departure": scheduled_local.isoformat(timespec="seconds"),
                    "_scheduled_utc": scheduled_local.astimezone(timezone.utc),
                })

        realtime, snapshot_id = _realtime_state(connection, now_utc)
        evidence_by_trip: dict[str, Any] = {}
        route_ids = sorted({item['route_id'] for item in candidates})
        archive_hash = metadata.get('gtfs_archive_sha256')
        covered_routes = set()
        if archive_hash and route_ids:
            placeholders = ','.join('?' for _ in route_ids)
            covered_routes = {r['route_id'] for r in connection.execute(
                f"SELECT route_id FROM realtime_route_coverage WHERE gtfs_archive_sha256=? AND route_id IN ({placeholders})",
                [archive_hash, *route_ids])}
            covered_routes.update(r['route_id'] for r in connection.execute(
                f"SELECT DISTINCT t.route_id FROM trip_status_history h JOIN trips t USING(trip_id) "
                f"WHERE h.status IN ('seen','cancelled') AND t.route_id IN ({placeholders})",
                route_ids))
        elif not archive_hash:
            covered_routes = set(route_ids)
        if snapshot_id is not None and candidates:
            trip_ids = sorted({item["trip_id"] for item in candidates})
            for offset in range(0, len(trip_ids), 800):
                batch = trip_ids[offset:offset + 800]
                placeholders = ",".join("?" for _ in batch)
                for row in connection.execute(
                    f"SELECT trip_id, status, delay_seconds, observed_at_utc FROM trip_status_history "
                    f"WHERE snapshot_id=? AND trip_id IN ({placeholders})",
                    [snapshot_id, *batch],
                ):
                    evidence_by_trip[row["trip_id"]] = dict(row)

    departures = []
    for item in candidates:
        scheduled_utc = item.pop("_scheduled_utc")
        evidence = evidence_by_trip.get(item["trip_id"])
        status = "scheduled-only"
        realtime_departure = None
        if evidence and evidence["status"] == "cancelled":
            status = "cancelled"
        elif evidence and evidence["status"] == "seen":
            status = "live"
            if evidence["delay_seconds"] is not None:
                realtime_departure = (scheduled_utc + timedelta(seconds=evidence["delay_seconds"])).astimezone(DUBLIN_TZ).isoformat(timespec="seconds")
        item.update({
            "status": status,
            "realtime_coverage": "observed" if item["route_id"] in covered_routes else "no_live_data",
            "realtime_departure": realtime_departure,
            "realtime_observed_at_utc": evidence["observed_at_utc"] if evidence and status in ("live", "cancelled") else None,
            "_sort_utc": scheduled_utc,
        })
        departures.append(item)
    departures.sort(key=lambda item: item["_sort_utc"])
    departures = departures[:limit]
    for item in departures:
        item.pop("_sort_utc")
    return {
        "stop_id": stop["stop_id"],
        "stop_name": stop["stop_name"],
        "departures": departures,
        "as_of": _iso_utc(now_utc),
        "timezone": "Europe/Dublin",
        "source": "NTA static GTFS",
        "static_feed_imported_at_utc": metadata.get("gtfs_imported_at_utc"),
        "realtime": realtime,
        "coverage": "Bus, rail, and tram (Luas) routes in the loaded national NTA timetable. Check realtime_coverage: no_live_data means the feed has never reported that route; it is not evidence of unreliability.",
        "message": None if departures else f"No scheduled departures were found within {window_minutes} minutes for this stop and filter.",
    }
