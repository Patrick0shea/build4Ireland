-- Static GTFS tables and append-only realtime history.
-- GTFS clock times are stored as integer seconds from the service day's midnight;
-- values may exceed 86400 for trips after midnight (for example, 25:10).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agencies (
    agency_id TEXT PRIMARY KEY,
    agency_name TEXT NOT NULL,
    agency_url TEXT,
    agency_timezone TEXT,
    agency_lang TEXT,
    agency_phone TEXT
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    agency_id TEXT REFERENCES agencies(agency_id),
    route_short_name TEXT,
    route_long_name TEXT,
    route_desc TEXT,
    route_type INTEGER NOT NULL,
    route_url TEXT,
    route_color TEXT,
    route_text_color TEXT
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id TEXT PRIMARY KEY,
    stop_code TEXT,
    stop_name TEXT NOT NULL,
    stop_desc TEXT,
    stop_lat REAL,
    stop_lon REAL,
    zone_id TEXT,
    stop_url TEXT,
    location_type INTEGER,
    parent_station TEXT REFERENCES stops(stop_id),
    wheelchair_boarding INTEGER
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_id TEXT NOT NULL,
    trip_headsign TEXT,
    trip_short_name TEXT,
    direction_id INTEGER,
    block_id TEXT,
    shape_id TEXT,
    wheelchair_accessible INTEGER,
    bikes_allowed INTEGER
);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_id TEXT NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL REFERENCES stops(stop_id),
    arrival_secs INTEGER,
    departure_secs INTEGER,
    stop_headsign TEXT,
    pickup_type INTEGER,
    drop_off_type INTEGER,
    shape_dist_traveled REAL,
    timepoint INTEGER,
    PRIMARY KEY (trip_id, stop_sequence)
);

CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    monday INTEGER NOT NULL,
    tuesday INTEGER NOT NULL,
    wednesday INTEGER NOT NULL,
    thursday INTEGER NOT NULL,
    friday INTEGER NOT NULL,
    saturday INTEGER NOT NULL,
    sunday INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id TEXT NOT NULL,
    date TEXT NOT NULL,
    exception_type INTEGER NOT NULL CHECK (exception_type IN (1, 2)),
    PRIMARY KEY (service_id, date)
);

CREATE TABLE IF NOT EXISTS realtime_route_coverage (
    gtfs_archive_sha256 TEXT NOT NULL,
    route_id TEXT NOT NULL,
    first_seen_at_utc TEXT NOT NULL,
    last_seen_at_utc TEXT NOT NULL,
    PRIMARY KEY (gtfs_archive_sha256, route_id)
);

CREATE TABLE IF NOT EXISTS feed_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at_utc TEXT NOT NULL,
    feed_timestamp_utc TEXT,
    fetch_status TEXT NOT NULL CHECK (fetch_status IN ('success', 'error')),
    error_message TEXT,
    entity_count INTEGER NOT NULL DEFAULT 0,
    trip_update_count INTEGER NOT NULL DEFAULT 0,
    vehicle_position_count INTEGER NOT NULL DEFAULT 0,
    alert_count INTEGER NOT NULL DEFAULT 0,
    raw_feed BLOB
);

-- One row per scheduled trip considered on each successful poll. "missing"
-- rows are only written for successful feeds; API failures never count as misses.
-- A consumer may flag a trip suspected missing after five consecutive misses.
CREATE TABLE IF NOT EXISTS trip_status_history (
    snapshot_id INTEGER NOT NULL REFERENCES feed_snapshots(snapshot_id) ON DELETE CASCADE,
    -- No static GTFS foreign key: refreshes may remove old trip IDs, but their
    -- historical observations must remain queryable.
    trip_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('seen', 'missing', 'cancelled')),
    observed_at_utc TEXT NOT NULL,
    delay_seconds INTEGER,
    vehicle_id TEXT,
    stop_id TEXT,
    stop_sequence INTEGER,
    PRIMARY KEY (snapshot_id, trip_id)
);

-- Preserve realtime trip updates even when their IDs do not match the loaded
-- timetable. This makes feed/static version mismatches diagnosable.
CREATE TABLE IF NOT EXISTS trip_updates (
    snapshot_id INTEGER NOT NULL REFERENCES feed_snapshots(snapshot_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    trip_id TEXT,
    route_id TEXT,
    direction_id INTEGER,
    start_date TEXT,
    start_time TEXT,
    schedule_relationship TEXT,
    delay_seconds INTEGER,
    stop_id TEXT,
    stop_sequence INTEGER,
    raw_entity BLOB,
    PRIMARY KEY (snapshot_id, entity_id)
);

CREATE TABLE IF NOT EXISTS vehicle_positions (
    snapshot_id INTEGER NOT NULL REFERENCES feed_snapshots(snapshot_id) ON DELETE CASCADE,
    vehicle_id TEXT NOT NULL,
    trip_id TEXT,
    latitude REAL,
    longitude REAL,
    bearing REAL,
    speed_mps REAL,
    current_stop_sequence INTEGER,
    stop_id TEXT,
    current_status TEXT,
    vehicle_timestamp_utc TEXT,
    PRIMARY KEY (snapshot_id, vehicle_id)
);

CREATE INDEX IF NOT EXISTS idx_routes_type ON routes(route_type);
CREATE INDEX IF NOT EXISTS idx_stops_name ON stops(stop_name);
CREATE INDEX IF NOT EXISTS idx_trips_route_service ON trips(route_id, service_id);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop_departure ON stop_times(stop_id, departure_secs);
CREATE INDEX IF NOT EXISTS idx_stop_times_trip_sequence ON stop_times(trip_id, stop_sequence);
CREATE INDEX IF NOT EXISTS idx_trip_status_trip_snapshot ON trip_status_history(trip_id, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_feed_snapshots_fetched ON feed_snapshots(fetched_at_utc);
CREATE INDEX IF NOT EXISTS idx_trip_updates_trip_snapshot ON trip_updates(trip_id, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_positions_trip ON vehicle_positions(trip_id, snapshot_id);

INSERT OR IGNORE INTO schema_metadata (key, value) VALUES ('schema_version', '1');
