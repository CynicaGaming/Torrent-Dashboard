from __future__ import annotations

import json
import re
import unittest

from torrent_dashboard.jellyfin import (
    find_jellyfin_integration,
    jellyfin_overview,
    jellyfin_scheduled_tasks,
    refresh_jellyfin_libraries,
    start_jellyfin_scheduled_task,
    stop_jellyfin_scheduled_task,
)



def _token(request):
    """Return the API key sent in the Jellyfin 12-compatible Authorization header."""
    assert request.get_header("X-emby-token") is None, "legacy X-Emby-Token header must not be sent"
    header = request.get_header("Authorization") or ""
    assert header.startswith("MediaBrowser "), header
    match = re.search(r'Token="((?:[^"\\]|\\.)*)"', header)
    return match.group(1) if match else None

class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _limit=-1):
        if self.payload is None:
            return b""
        return json.dumps(self.payload).encode("utf-8")


class JellyfinIntegrationTests(unittest.TestCase):
    def item(self):
        return {
            "id": "jellyfin-1",
            "type": "jellyfin",
            "name": "Media server",
            "enabled": True,
            "url": "http://jellyfin:8096",
            "api_key": "secret-key",
        }

    def test_overview_fetches_server_and_virtual_folders(self):
        calls = []

        def opener(request, timeout=0):
            calls.append((request.full_url, request.get_method(), _token(request), timeout))
            if request.full_url.endswith("/System/Info"):
                return FakeResponse({
                    "ServerName": "Living Room Jellyfin",
                    "Version": "10.10.7",
                    "OperatingSystem": "Linux",
                    "HasPendingRestart": True,
                })
            if request.full_url.endswith("/Library/VirtualFolders"):
                return FakeResponse([
                    {
                        "Name": "Movies",
                        "CollectionType": "movies",
                        "Locations": ["/media/movies"],
                        "ItemId": "library-movies",
                        "RefreshStatus": "Running",
                        "RefreshProgress": 37.5,
                    },
                    {
                        "Name": "Shows",
                        "CollectionType": "tvshows",
                        "Locations": ["/media/tv", " /media/anime "],
                        "ItemId": "library-shows",
                    },
                ])
            raise AssertionError(request.full_url)

        overview = jellyfin_overview(self.item(), opener=opener)
        self.assertTrue(overview["ok"])
        self.assertEqual(overview["server"]["name"], "Living Room Jellyfin")
        self.assertEqual(overview["server"]["version"], "10.10.7")
        self.assertTrue(overview["server"]["pending_restart"])
        self.assertEqual([item["name"] for item in overview["libraries"]], ["Movies", "Shows"])
        self.assertEqual(overview["libraries"][0]["refresh_progress"], 37.5)
        self.assertEqual(overview["libraries"][1]["locations"], ["/media/tv", "/media/anime"])
        self.assertEqual([call[0].rsplit("/", 2)[-2:] for call in calls], [["System", "Info"], ["Library", "VirtualFolders"]])
        self.assertTrue(all(call[2] == "secret-key" for call in calls))

    def test_refresh_starts_real_scan_media_library_scheduled_task(self):
        calls = []

        def opener(request, timeout=0):
            calls.append((request.full_url, request.get_method(), _token(request), timeout))
            if request.full_url.endswith("/ScheduledTasks?isHidden=false"):
                return FakeResponse([
                    {
                        "Id": "scan-task",
                        "Key": "RefreshMediaLibraryTask",
                        "Name": "Scan Media Library",
                        "Category": "Library",
                        "State": "Idle",
                        "CurrentProgressPercentage": None,
                    },
                    {
                        "Id": "other-task",
                        "Key": "Plugin.Task",
                        "Name": "Other task",
                        "Category": "Plugin",
                        "State": "Idle",
                    },
                ])
            return FakeResponse()

        result = refresh_jellyfin_libraries(self.item(), opener=opener)
        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["id"], "scan-task")
        self.assertEqual(calls[0][0], "http://jellyfin:8096/ScheduledTasks?isHidden=false")
        self.assertEqual(calls[0][1], "GET")
        self.assertEqual(calls[1][0], "http://jellyfin:8096/ScheduledTasks/Running/scan-task")
        self.assertEqual(calls[1][1], "POST")
        self.assertTrue(all(call[2] == "secret-key" for call in calls))

    def test_saved_jellyfin_lookup_rejects_other_provider(self):
        cfg = {"integrations": [{"id": "sonarr-1", "type": "sonarr"}]}
        with self.assertRaisesRegex(RuntimeError, "not a Jellyfin"):
            find_jellyfin_integration(cfg, "sonarr-1")

    def test_saved_jellyfin_lookup_returns_full_server_side_configuration(self):
        item = self.item()
        found = find_jellyfin_integration({"integrations": [item]}, item["id"])
        self.assertIs(found, item)
        self.assertEqual(found["api_key"], "secret-key")


    def test_scheduled_tasks_include_plugin_categories_and_last_run_details(self):
        calls = []

        def opener(request, timeout=0):
            calls.append((request.full_url, request.get_method(), _token(request), timeout))
            return FakeResponse([
                {
                    "Id": "task-plugin",
                    "Key": "Plugin.Task",
                    "Name": "Detect And Analyze Media Segments",
                    "Category": "Intro Skipper",
                    "State": "Idle",
                    "CurrentProgressPercentage": None,
                    "LastExecutionResult": {
                        "Status": "Completed",
                        "StartTimeUtc": "2026-09-06T14:00:00Z",
                        "EndTimeUtc": "2026-09-06T14:20:00Z",
                    },
                },
                {
                    "Id": "task-app",
                    "Name": "Update Plugins",
                    "Category": "Application",
                    "State": "Running",
                    "CurrentProgressPercentage": 42.5,
                },
            ])

        tasks = jellyfin_scheduled_tasks(self.item(), opener=opener)
        self.assertEqual([task["category"] for task in tasks], ["Application", "Intro Skipper"])
        self.assertTrue(tasks[0]["running"])
        self.assertEqual(tasks[0]["progress"], 42.5)
        self.assertEqual(tasks[1]["last_status"], "Completed")
        self.assertEqual(tasks[1]["last_start"], "2026-09-06T14:00:00Z")
        self.assertEqual(tasks[1]["last_end"], "2026-09-06T14:20:00Z")
        self.assertEqual(calls[0][0], "http://jellyfin:8096/ScheduledTasks?isHidden=false")
        self.assertEqual(calls[0][1], "GET")
        self.assertEqual(calls[0][2], "secret-key")

    def test_scheduled_task_start_and_stop_use_jellyfin_running_task_endpoint(self):
        calls = []

        def opener(request, timeout=0):
            calls.append((request.full_url, request.get_method(), _token(request), timeout))
            return FakeResponse()

        started = start_jellyfin_scheduled_task(self.item(), "plugin task/1", opener=opener)
        stopped = stop_jellyfin_scheduled_task(self.item(), "plugin task/1", opener=opener)
        self.assertTrue(started["ok"])
        self.assertTrue(stopped["ok"])
        self.assertEqual(calls[0][0], "http://jellyfin:8096/ScheduledTasks/Running/plugin%20task%2F1")
        self.assertEqual(calls[0][1], "POST")
        self.assertEqual(calls[1][0], calls[0][0])
        self.assertEqual(calls[1][1], "DELETE")
        self.assertTrue(all(call[2] == "secret-key" for call in calls))

class JellyfinAuthorizationHeaderTests(unittest.TestCase):
    def test_header_uses_mediabrowser_scheme_and_escapes_values(self):
        from torrent_dashboard.jellyfin import jellyfin_authorization_header
        header = jellyfin_authorization_header('ab"c\\d')
        self.assertTrue(header.startswith('MediaBrowser Client="Torrent Dashboard"'))
        self.assertIn('Token="ab\\"c\\\\d"', header)
        self.assertIn('DeviceId="torrent-dashboard"', header)


if __name__ == "__main__":
    unittest.main()
