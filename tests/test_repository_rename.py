"""The default update repository moved to CynicaGaming/Torrent-Dashboard."""

import json
from pathlib import Path
import tempfile
import unittest

from torrent_dashboard import recovery_tool, updater
from torrent_dashboard.config import DEFAULT_UPDATE_REPOSITORY, ConfigRepository


class RepositoryRenameTests(unittest.TestCase):
    def load(self, repository):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"updates": {"repository": repository}}), encoding="utf-8")
            return ConfigRepository(path, detect_lan_network=lambda: {}).load()["updates"]["repository"]

    def test_default_is_new_name(self):
        self.assertEqual(DEFAULT_UPDATE_REPOSITORY, "CynicaGaming/Torrent-Dashboard")
        self.assertEqual(updater.DEFAULT_REPOSITORY, DEFAULT_UPDATE_REPOSITORY)
        self.assertEqual(recovery_tool.DEFAULT_REPOSITORY, DEFAULT_UPDATE_REPOSITORY)

    def test_saved_old_default_follows_the_rename(self):
        for saved in ("CynicaGaming/TorrentDashboard", "cynicagaming/torrentdashboard",
                      "https://github.com/CynicaGaming/TorrentDashboard"):
            with self.subTest(saved=saved):
                self.assertEqual(self.load(saved), "CynicaGaming/Torrent-Dashboard")

    def test_custom_and_fork_repositories_are_kept(self):
        for saved in ("someone/TorrentDashboard", "CynicaGaming/TorrentDashboard-fork", "example/project"):
            with self.subTest(saved=saved):
                self.assertEqual(self.load(saved), saved)

    def test_updater_and_recovery_map_old_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "config.json").write_text(json.dumps({"updates": {"repository": "CynicaGaming/TorrentDashboard"}}), encoding="utf-8")
            self.assertEqual(updater.configured_repository(target), "CynicaGaming/Torrent-Dashboard")
            (target / "config.json").write_text(json.dumps({"updates": {"repository": "example/project"}}), encoding="utf-8")
            self.assertEqual(updater.configured_repository(target), "example/project")
        self.assertEqual(recovery_tool.configured_repository({"updates": {"repository": "CynicaGaming/TorrentDashboard"}}), "CynicaGaming/Torrent-Dashboard")
        self.assertEqual(recovery_tool.configured_repository({"updates": {"repository": "example/project"}}), "example/project")


if __name__ == "__main__":
    unittest.main()
