import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

import db
from mcp_tools import _realtime_state


class MCPFreshnessTests(unittest.TestCase):
    def test_requires_each_configured_feed_timestamp_to_be_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = db.initialize(Path(directory) / 'db.sqlite3')
            now = datetime(2026,10,4,13,0,tzinfo=timezone.utc)
            fetched = now.timestamp()
            envelope = {'poller_sources': {'trips': {'timestamp': fetched-130},
                                           'vehicles': {'timestamp': fetched-65}}}
            with db.transaction(path) as con:
                con.execute('''INSERT INTO feed_snapshots
                    (fetched_at_utc,feed_timestamp_utc,fetch_status,raw_feed)
                    VALUES (?,?,'success',?)''',
                    (now.isoformat(),now.isoformat(),json.dumps(envelope).encode()))
                state,snapshot = _realtime_state(con,now)
                self.assertEqual(state['state'],'stale')
                self.assertEqual(state['source_age_seconds'],{'trips':130.0,'vehicles':65.0})
                self.assertIsNone(snapshot)

    def test_single_feed_uses_feed_timestamp_and_failed_attempt_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = db.initialize(Path(directory) / 'db.sqlite3')
            now = datetime(2026,10,4,13,0,tzinfo=timezone.utc)
            with db.transaction(path) as con:
                con.execute('''INSERT INTO feed_snapshots
                    (fetched_at_utc,feed_timestamp_utc,fetch_status,raw_feed)
                    VALUES (?,?,'success',?)''',
                    (now.isoformat(),now.isoformat(),b'{"header":{}}'))
                state,snapshot = _realtime_state(con,now)
                self.assertEqual(state['state'],'fresh')
                self.assertEqual(snapshot,1)
                con.execute("INSERT INTO feed_snapshots (fetched_at_utc,fetch_status,error_message) VALUES (?,'error','HTTP 429')",(now.isoformat(),))
                state,snapshot = _realtime_state(con,now)
                self.assertEqual(state['state'],'unavailable')
                self.assertIsNone(snapshot)


if __name__ == '__main__':
    unittest.main()
