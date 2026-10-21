"""Regression tests for the reliability, performance, and disclosure fixes."""

from contextlib import closing
from email.message import Message
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from torrent_dashboard import dashboard
from torrent_dashboard.history import HistoryStore
from torrent_dashboard.state_gate import StateGate


def torrent(h, progress=0.5, **extra):
    return {"hash": h, "name": h, "progress": progress, "state": "downloading", **extra}


class HistoryWriteThrottleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = HistoryStore(Path(self.temp.name) / "history.sqlite3")

    def count(self, table):
        with closing(sqlite3.connect(self.store.path)) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_unchanged_torrents_are_not_rewritten_every_pass(self):
        torrents = [torrent(f"h{i}") for i in range(50)]
        self.store.sample("s", torrents, {}, None, every=3600)
        self.assertEqual(self.count("snapshots"), 1)
        self.assertEqual(self.count("torrent_history"), 50)
        with patch.object(self.store, "_db", wraps=self.store._db) as db:
            for _ in range(5):
                self.store.sample("s", torrents, {}, None, every=3600)
        db.assert_not_called()
        self.assertEqual(self.count("snapshots"), 1)

    def test_completion_is_still_recorded_between_samples(self):
        self.store.sample("s", [torrent("a")], {}, None, every=3600)
        self.store.sample("s", [torrent("a", progress=1.0, completion_on=99)], {}, None, every=3600)
        events = [e for e in self.store.events() if e["event"] == "completed"]
        self.assertEqual(len(events), 1)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT completion_on FROM torrent_history").fetchone()[0], 99)

    def test_cleanup_is_throttled(self):
        with patch.object(self.store, "cleanup", return_value={}) as cleanup:
            self.store.maybe_cleanup(30, interval=600)
            self.store.maybe_cleanup(30, interval=600)
        cleanup.assert_called_once_with(30)


class SessionAndRateLimitTests(unittest.TestCase):
    def test_expired_sessions_are_swept_on_create(self):
        store = dashboard.SessionStore()
        old, _ = store.create("u", 1, "password")
        store.sessions[old]["expires"] = time.time() - 1
        store._last_purge = 0
        store.create("u", 1, "password")
        self.assertNotIn(old, store.sessions)

    def test_attempt_table_prunes_idle_ips(self):
        from collections import defaultdict, deque
        attempts = defaultdict(deque)
        for i in range(1100):
            attempts[f"10.0.{i // 250}.{i % 250}"].append(0.0)
        self.assertTrue(dashboard._record_attempt(attempts, "192.0.2.1", 10_000.0, 600, 5))
        self.assertEqual(list(attempts), ["192.0.2.1"])

    def test_attempt_limit_still_enforced(self):
        from collections import defaultdict, deque
        attempts = defaultdict(deque)
        results = [dashboard._record_attempt(attempts, "ip", 100.0 + i, 600, 3) for i in range(5)]
        self.assertEqual(results, [True, True, True, False, False])


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.config = {"setup": {"complete": True}, "auth": {"mode": "required", "session_hours": 24},
                       "dashboard": {}, "users": []}
        self.sessions = dashboard.SessionStore()
        for name, value in (("SESSIONS", self.sessions), ("STATE_GATE", StateGate()),
                            ("load_config", Mock(side_effect=lambda: self.config))):
            patcher = patch.object(dashboard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, path, ip="192.0.2.1", token=None, body=b""):
        request = object.__new__(dashboard.Handler)
        request.path = path
        request.client_address = (ip, 1234)
        request.headers = Message()
        request.headers["Content-Length"] = str(len(body))
        if token:
            request.headers["Cookie"] = f"td_session={token}"
        request.rfile = io.BytesIO(body)
        request.send_json = Mock()
        return request

    def test_health_hides_process_details_from_remote_clients(self):
        remote = self.request("/health")
        remote.do_GET()
        self.assertEqual(set(remote.send_json.call_args.args[1]), {"ok", "version"})
        local = self.request("/health", ip="127.0.0.1")
        local.do_GET()
        self.assertIn("pid", local.send_json.call_args.args[1])

    def test_setup_endpoints_stop_disclosing_network_after_setup(self):
        status = self.request("/api/setup/status")
        status.do_GET()
        self.assertEqual(status.send_json.call_args.args[1], {"required": False, "code_required": False})
        interfaces = self.request("/api/setup/network-interfaces")
        interfaces.do_GET()
        self.assertEqual(interfaces.send_json.call_args.args[0], 403)

    def test_bad_query_returns_json_error_instead_of_dropping_connection(self):
        token, _ = self.sessions.create("admin", 24, "password", group="administrator")
        request = self.request("/api/history?minutes=abc", token=token)
        request.do_GET()
        self.assertEqual(request.send_json.call_args.args[0], 400)

    def test_single_argument_log_error_does_not_raise(self):
        request = self.request("/")
        request.requestline = "GET / HTTP/1.1"
        with patch("http.server.BaseHTTPRequestHandler.log_message") as base:
            request.log_error("Request timed out: %r", TimeoutError())
            request.log_message('"%s" %s %s', "GET /", "200", "-")
            request.log_message('"%s" %s %s', "GET /", "500", "-")
        self.assertEqual(base.call_count, 2)

    def test_unknown_username_still_runs_password_hashing(self):
        body = json.dumps({"username": "nobody", "password": "x"}).encode()
        request = self.request("/api/login", body=body)
        with patch.object(dashboard, "verify_password", return_value=False) as verify, \
             patch.object(dashboard, "HISTORY", Mock()):
            request.do_POST()
        verify.assert_called_once()
        self.assertEqual(request.send_json.call_args.args[0], 401)


class SettingsValidationTests(unittest.TestCase):
    def base(self):
        return {"dashboard": {"port": 8765, "history_sample_seconds": 10}, "auth": {"mode": "disabled"},
                "users": [], "servers": [], "notifications": {"browser": True, "volume": 72, "custom_sound_file": ""}}

    def test_rejects_non_numeric_and_out_of_range_values(self):
        for field, value in (("history_sample_seconds", "abc"), ("history_sample_seconds", 1),
                             ("history_retention_days", -5), ("port", 70000), ("low_disk_gb", "lots")):
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                dashboard.apply_settings_update(self.base(), {"dashboard": {field: value}})

    def test_accepts_numeric_strings_and_coerces_types(self):
        out = dashboard.apply_settings_update(self.base(), {
            "dashboard": {"port": "9000", "low_disk_gb": "20", "title": "  Box  "},
            "notifications": {"browser": 0, "volume": 400, "custom_sound_file": "../../evil"},
        })
        self.assertEqual(out["dashboard"]["port"], 9000)
        self.assertEqual(out["dashboard"]["low_disk_gb"], 20)
        self.assertEqual(out["dashboard"]["title"], "Box")
        self.assertIs(out["notifications"]["browser"], False)
        self.assertEqual(out["notifications"]["volume"], 100)
        self.assertEqual(out["notifications"]["custom_sound_file"], "")

    def test_https_requires_certificate_and_key(self):
        with self.assertRaises(RuntimeError):
            dashboard.apply_settings_update(self.base(), {"dashboard": {"https_enabled": True}})


class CollectorTests(unittest.TestCase):
    def test_collector_survives_unexpected_errors(self):
        stop = threading.Event()
        calls = []

        def failing_pass():
            calls.append(1)
            if len(calls) >= 3:
                stop.set()
            raise ValueError("bad config")

        with patch.object(dashboard, "_collect_once", side_effect=failing_pass), \
             patch.object(dashboard, "STATUS_REFRESH_SECONDS", 0), \
             self.assertLogs("torrent_dashboard.dashboard", level="ERROR"):
            dashboard.collector_loop(stop)
        self.assertEqual(len(calls), 3)

    def test_notifications_do_not_block_the_caller(self):
        release = threading.Event()
        delivered = []
        dispatcher = dashboard.NotificationDispatcher(lambda cfg, t, m: (release.wait(5), delivered.append(t)))
        started = time.monotonic()
        dispatcher.submit({}, "one", "m")
        dispatcher.submit({}, "two", "m")
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        dispatcher._queue.join()
        self.assertEqual(delivered, ["one", "two"])


class QBitClientCacheTests(unittest.TestCase):
    def test_versions_and_preferences_are_cached(self):
        client = dashboard.QBitClient({"base_url": "http://127.0.0.1:1", "auth_method": "api_key", "api_key": "k"})
        responses = {"/api/v2/torrents/info": [], "/api/v2/transfer/info": {}, "/api/v2/app/preferences": {"save_path": "/x"}}
        client.get_json = Mock(side_effect=lambda path, params=None: responses[path])
        client.get_text = Mock(return_value="v5")
        for _ in range(5):
            client.info()
            client.cached_preferences()
        self.assertEqual(client.get_text.call_count, 2)
        self.assertEqual(sum(1 for c in client.get_json.call_args_list if c.args[0] == "/api/v2/app/preferences"), 1)
        client.invalidate_cache()
        client.cached_preferences()
        self.assertEqual(sum(1 for c in client.get_json.call_args_list if c.args[0] == "/api/v2/app/preferences"), 2)


if __name__ == "__main__":
    unittest.main()
