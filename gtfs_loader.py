"""Download and transactionally import an NTA static GTFS ZIP into SQLite."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import re
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable

import db


DEFAULT_GTFS_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip"
REQUIRED_FILES = ("routes.txt", "stops.txt", "trips.txt", "stop_times.txt")
def parse_gtfs_time(value: str | None) -> int | None:
    """Convert a GTFS HH:MM:SS value to seconds, preserving hours >= 24."""
    if value is None or not value.strip():
        return None
    match = re.fullmatch(r"(\d{1,3}):(\d{2}):(\d{2})", value.strip())
    if not match:
        raise ValueError(f"Invalid GTFS time {value!r}; expected HH:MM:SS")
    hours, minutes, seconds = map(int, match.groups())
    if minutes > 59 or seconds > 59:
        raise ValueError(f"Invalid GTFS time {value!r}; minute/second out of range")
    return hours * 3600 + minutes * 60 + seconds


def _blank_to_none(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def _integer(value: str | None) -> int | None:
    value = _blank_to_none(value)
    return int(value) if value is not None else None


def _real(value: str | None) -> float | None:
    value = _blank_to_none(value)
    return float(value) if value is not None else None


def _date(value: str | None) -> str | None:
    value = _blank_to_none(value)
    if value is None:
        return None
    if not re.fullmatch(r"\d{8}", value):
        raise ValueError(f"Invalid GTFS date {value!r}; expected YYYYMMDD")
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _csv_rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """Yield CSV records by basename, regardless of the ZIP's directory layout."""
    member = next((item for item in archive.namelist() if Path(item).name == name), None)
    if member is None:
        return
    with archive.open(member) as binary_file:
        text_file = io.TextIOWrapper(binary_file, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text_file)


def _insert_rows(
    connection,
    table: str,
    columns: tuple[str, ...],
    records: Iterable[tuple],
    batch_size: int = 5000,
) -> int:
    placeholders = ",".join("?" for _ in columns)
    column_sql = ",".join(columns)
    statement = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
    count = 0
    batch: list[tuple] = []
    for row in records:
        batch.append(row)
        if len(batch) >= batch_size:
            connection.executemany(statement, batch)
            count += len(batch)
            batch.clear()
    if batch:
        connection.executemany(statement, batch)
        count += len(batch)
    return count


def _rows(archive: zipfile.ZipFile, name: str, transform):
    for row in _csv_rows(archive, name):
        yield transform(row)


def _import_archive(archive: zipfile.ZipFile, connection) -> dict[str, int]:
    members = {Path(member).name for member in archive.namelist()}
    missing = sorted(set(REQUIRED_FILES) - members)
    if missing:
        raise ValueError(f"GTFS archive is missing required files: {', '.join(missing)}")
    if "calendar.txt" not in members and "calendar_dates.txt" not in members:
        raise ValueError("GTFS archive must contain calendar.txt or calendar_dates.txt")

    # Clear static records only after validating the ZIP. Historical realtime
    # tables intentionally have no FK to static IDs, so refreshes preserve them.
    connection.execute("PRAGMA defer_foreign_keys = ON")
    for table in ("calendar_dates", "calendar", "stop_times", "trips", "routes", "stops", "agencies"):
        connection.execute(f"DELETE FROM {table}")

    counts: dict[str, int] = {}

    if "agency.txt" in members:
        counts["agencies"] = _insert_rows(
            connection,
            "agencies",
            ("agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang", "agency_phone"),
            _rows(archive, "agency.txt", lambda r: (
                r.get("agency_id", "") or "", r.get("agency_name", ""), _blank_to_none(r.get("agency_url")),
                _blank_to_none(r.get("agency_timezone")), _blank_to_none(r.get("agency_lang")),
                _blank_to_none(r.get("agency_phone")),
            )),
        )
    else:
        counts["agencies"] = 0

    counts["routes"] = _insert_rows(
        connection,
        "routes",
        ("route_id", "agency_id", "route_short_name", "route_long_name", "route_desc", "route_type", "route_url", "route_color", "route_text_color"),
        _rows(archive, "routes.txt", lambda r: (
            r["route_id"], _blank_to_none(r.get("agency_id")), _blank_to_none(r.get("route_short_name")),
            _blank_to_none(r.get("route_long_name")), _blank_to_none(r.get("route_desc")), int(r["route_type"]),
            _blank_to_none(r.get("route_url")), _blank_to_none(r.get("route_color")),
            _blank_to_none(r.get("route_text_color")),
        )),
    )
    counts["stops"] = _insert_rows(
        connection,
        "stops",
        ("stop_id", "stop_code", "stop_name", "stop_desc", "stop_lat", "stop_lon", "zone_id", "stop_url", "location_type", "parent_station", "wheelchair_boarding"),
        _rows(archive, "stops.txt", lambda r: (
            r["stop_id"], _blank_to_none(r.get("stop_code")), r["stop_name"], _blank_to_none(r.get("stop_desc")),
            _real(r.get("stop_lat")), _real(r.get("stop_lon")), _blank_to_none(r.get("zone_id")),
            _blank_to_none(r.get("stop_url")), _integer(r.get("location_type")),
            _blank_to_none(r.get("parent_station")), _integer(r.get("wheelchair_boarding")),
        )),
    )
    counts["trips"] = _insert_rows(
        connection,
        "trips",
        ("trip_id", "route_id", "service_id", "trip_headsign", "trip_short_name", "direction_id", "block_id", "shape_id", "wheelchair_accessible", "bikes_allowed"),
        _rows(archive, "trips.txt", lambda r: (
            r["trip_id"], r["route_id"], r["service_id"], _blank_to_none(r.get("trip_headsign")),
            _blank_to_none(r.get("trip_short_name")), _integer(r.get("direction_id")),
            _blank_to_none(r.get("block_id")), _blank_to_none(r.get("shape_id")),
            _integer(r.get("wheelchair_accessible")), _integer(r.get("bikes_allowed")),
        )),
    )
    counts["stop_times"] = _insert_rows(
        connection,
        "stop_times",
        ("trip_id", "stop_sequence", "stop_id", "arrival_secs", "departure_secs", "stop_headsign", "pickup_type", "drop_off_type", "shape_dist_traveled", "timepoint"),
        _rows(archive, "stop_times.txt", lambda r: (
            r["trip_id"], int(r["stop_sequence"]), r["stop_id"], parse_gtfs_time(r.get("arrival_time")),
            parse_gtfs_time(r.get("departure_time")), _blank_to_none(r.get("stop_headsign")),
            _integer(r.get("pickup_type")), _integer(r.get("drop_off_type")),
            _real(r.get("shape_dist_traveled")), _integer(r.get("timepoint")),
        )),
    )

    if "calendar.txt" in members:
        counts["calendar"] = _insert_rows(
            connection,
            "calendar",
            ("service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"),
            _rows(archive, "calendar.txt", lambda r: (
                r["service_id"], *(int(r[day]) for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")),
                _date(r["start_date"]), _date(r["end_date"]),
            )),
        )
    else:
        counts["calendar"] = 0

    if "calendar_dates.txt" in members:
        counts["calendar_dates"] = _insert_rows(
            connection,
            "calendar_dates",
            ("service_id", "date", "exception_type"),
            _rows(archive, "calendar_dates.txt", lambda r: (r["service_id"], _date(r["date"]), int(r["exception_type"]))),
        )
    else:
        counts["calendar_dates"] = 0

    if not counts["routes"] or not counts["stops"] or not counts["trips"] or not counts["stop_times"]:
        raise ValueError("GTFS archive has one or more required files with no data rows")
    return counts


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "build4Ireland-gtfs-loader/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def load_gtfs(
    archive_path: str | Path | None = None,
    *,
    url: str | None = None,
    database_path: str | Path | None = None,
) -> dict[str, int]:
    """Import a local GTFS ZIP or download one; transaction rolls back on errors."""
    selected_url = url or os.environ.get("NTA_GTFS_URL", DEFAULT_GTFS_URL)
    if archive_path is not None:
        source = Path(archive_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(source)
        with source.open("rb") as archive_file:
            checksum = _sha256(archive_file)
        archive_context = source
        source_description = f"local:{source.resolve()}"
    else:
        temp_dir = Path(tempfile.mkdtemp(prefix="build4ireland-gtfs-"))
        archive_context = temp_dir / "nta_gtfs.zip"
        try:
            _download(selected_url, archive_context)
            with archive_context.open("rb") as archive_file:
                checksum = _sha256(archive_file)
            source_description = selected_url
        except Exception:
            archive_context.unlink(missing_ok=True)
            temp_dir.rmdir()
            raise

    try:
        db.initialize(database_path)
        with db.transaction(database_path) as connection:
            with zipfile.ZipFile(archive_context) as archive:
                counts = _import_archive(archive, connection)
            imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            connection.executemany(
                "INSERT INTO schema_metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (("gtfs_imported_at_utc", imported_at), ("gtfs_archive_sha256", checksum), ("gtfs_source_url", source_description)),
            )
        return counts
    finally:
        if archive_path is None:
            archive_context.unlink(missing_ok=True)
            archive_context.parent.rmdir()


def _sha256(file: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := file.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the NTA static GTFS feed into SQLite.")
    parser.add_argument("--zip", dest="archive_path", help="Use an already-downloaded GTFS ZIP")
    parser.add_argument("--url", help=f"Override the download URL (default: {DEFAULT_GTFS_URL})")
    parser.add_argument("--db", dest="database_path", help="Override TRANSPORT_DB_PATH")
    args = parser.parse_args()

    counts = load_gtfs(args.archive_path, url=args.url, database_path=args.database_path)
    for table, count in counts.items():
        print(f"{table}: {count:,}")
    print(f"Database: {args.database_path or db.database_path()}")


if __name__ == "__main__":
    main()
