-- Read-only examples. Use fixture replay for deterministic demo outputs.
-- Inspect mode/freshness before presenting any data as current.
SELECT key,value FROM schema_metadata WHERE key IN ('demo_mode','demo_as_of_utc');
SELECT snapshot_id, fetched_at_utc,feed_timestamp_utc,fetch_status,error_message
FROM feed_snapshots ORDER BY snapshot_id DESC LIMIT 5;

-- Route and stop lookup.
SELECT route_id,route_short_name,route_long_name FROM routes WHERE route_short_name='27';
SELECT stop_id,stop_name,stop_lat,stop_lon FROM stops WHERE stop_id='8220DB000298';

-- Raw scheduled times; apply service calendar and timezone before displaying.
SELECT t.trip_id,r.route_short_name,t.trip_headsign,t.service_id,
       s.stop_id,s.stop_name,st.stop_sequence,st.departure_secs
FROM stop_times st JOIN trips t USING(trip_id) JOIN routes r USING(route_id)
JOIN stops s USING(stop_id)
WHERE st.stop_id='8220DB000298' ORDER BY st.departure_secs LIMIT 10;

-- Every stop prediction (not just the first summary stop).
SELECT u.trip_id, u.start_date,u.schedule_relationship,
       json_extract(j.value,'$.stop_id') AS stop_id,
       json_extract(j.value,'$.stop_sequence') AS stop_sequence,
       json_extract(j.value,'$.schedule_relationship') AS stop_relationship,
       json_extract(j.value,'$.arrival.time') AS arrival_epoch,
       json_extract(j.value,'$.departure.time') AS departure_epoch,
       json_extract(j.value,'$.arrival.delay') AS arrival_delay_seconds
FROM trip_updates u,json_each(CAST(u.raw_entity AS TEXT),'$.trip_update.stop_time_update') j
WHERE u.snapshot_id=(SELECT MAX(snapshot_id) FROM feed_snapshots WHERE fetch_status='success')
LIMIT 10;

SELECT v.vehicle_id,v.trip_id,r.route_short_name,v.latitude,v.longitude,v.vehicle_timestamp_utc
FROM vehicle_positions v LEFT JOIN trips t ON t.trip_id=v.trip_id
LEFT JOIN routes r ON r.route_id=t.route_id
WHERE v.snapshot_id=(SELECT MAX(snapshot_id) FROM feed_snapshots WHERE fetch_status='success')
LIMIT 10;

SELECT status,COUNT(*) AS trips FROM trip_status_history
WHERE snapshot_id=(SELECT MAX(snapshot_id) FROM feed_snapshots WHERE fetch_status='success')
GROUP BY status;

SELECT DISTINCT u.trip_id FROM trip_updates u LEFT JOIN trips t ON t.trip_id=u.trip_id
WHERE u.snapshot_id=(SELECT MAX(snapshot_id) FROM feed_snapshots WHERE fetch_status='success')
AND u.trip_id IS NOT NULL AND u.trip_id!='' AND t.trip_id IS NULL LIMIT 10;
