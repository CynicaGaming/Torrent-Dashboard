from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from torrent_dashboard.config import (
    ConfigRepository,
    DEFAULT_CONFIG,
    normalize_config,
    public_config,
)


class ConfigModuleTests(unittest.TestCase):
    def test_repository_creates_and_loads_default_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            repo = ConfigRepository(path)
            loaded = repo.load()
            self.assertTrue(path.is_file())
            self.assertFalse(loaded["setup"]["complete"])
            self.assertEqual(loaded["updates"]["repository"], "CynicaGaming/Torrent-Dashboard")

    def test_existing_config_migrates_setup_and_legacy_network_fields(self):
        raw = {
            "auth": {
                "trusted_cidrs": ["127.0.0.0/8", "192.168.50.0/24"],
                "auto_trust_lan": True,
            }
        }
        cfg = normalize_config(
            raw,
            lambda: {"interface_id": "eth-test", "interface": "Ethernet"},
        )
        self.assertTrue(cfg["setup"]["complete"])
        self.assertEqual(cfg["auth"]["trusted_ips"], ["192.168.50.0/24"])
        self.assertEqual(cfg["auth"]["trusted_interfaces"], ["eth-test"])

    def test_legacy_auth_integrations_notifications_and_update_source_migrate(self):
        raw = {
            "auth": {"username": "legacy-admin", "password_hash": "legacy-hash"},
            "integrations": [
                {
                    "type": "github",
                    "repository": "https://github.com/example/fork.git",
                },
                {
                    "type": "sonarr",
                    "name": "Sonarr",
                    "url": "http://sonarr:8989/",
                    "api_key": "abc123",
                },
            ],
            "notifications": {
                "discord_webhook": "https://discord.com/api/webhooks/1/test",
            },
        }
        cfg = normalize_config(raw)
        self.assertEqual(len(cfg["users"]), 1)
        self.assertEqual(cfg["users"][0]["username"], "legacy-admin")
        self.assertEqual(cfg["users"][0]["group"], "administrator")
        self.assertEqual(cfg["updates"]["repository"], "example/fork")
        self.assertEqual({item["type"] for item in cfg["integrations"]}, {"sonarr", "discord"})
        self.assertNotIn("discord_webhook", cfg["notifications"])

    def test_repository_save_removes_retired_github_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            repo = ConfigRepository(path)
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["updates"] = {
                "repository": "https://github.com/example/fork.git",
                "github_token": "should-not-persist",
            }
            cfg["integrations"] = [
                {"type": "github", "repository": "example/old"},
            ]
            repo.save(cfg)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["updates"], {"repository": "example/fork"})
            self.assertEqual(saved["integrations"], [])

    def test_notification_volume_is_defaulted_and_clamped(self):
        self.assertEqual(normalize_config({})["notifications"]["volume"], 72)
        self.assertEqual(normalize_config({"notifications": {"volume": 150}})["notifications"]["volume"], 100)
        self.assertEqual(normalize_config({"notifications": {"volume": -5}})["notifications"]["volume"], 0)
        self.assertEqual(normalize_config({"notifications": {"volume": "invalid"}})["notifications"]["volume"], 72)

    def test_public_config_redacts_browser_secrets(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["auth"].update({"username": "admin", "password_hash": "hash"})
        cfg["servers"] = [{"password": "secret", "api_key": "qbt_secret"}]
        cfg["integrations"] = [{
            "id": "one",
            "type": "sonarr",
            "name": "Sonarr",
            "enabled": True,
            "url": "http://sonarr:8989",
            "api_key": "secret",
        }]
        cfg["notifications"]["gotify_token"] = "secret"
        public = public_config(cfg)
        self.assertNotIn("username", public["auth"])
        self.assertNotIn("password_hash", public["auth"])
        self.assertEqual(public["servers"][0]["password"], "<configured>")
        self.assertEqual(public["servers"][0]["api_key"], "<configured>")
        self.assertEqual(public["integrations"][0]["api_key"], "<configured>")
        self.assertEqual(public["integrations"][0]["configured_secrets"], ["api_key"])
        self.assertEqual(public["notifications"]["gotify_token"], "<configured>")


if __name__ == "__main__":
    unittest.main()
