import hashlib
import json
import tempfile
import unittest
from pathlib import Path


from torrent_dashboard.release_provenance import (
    ReleaseProvenance,
    asset_sha256,
    find_dashboard_asset,
    github_release_integrity,
    normalize_release_integrity,
    release_info_payload,
)


class ReleaseParsingTests(unittest.TestCase):
    def test_windows_zip_is_selected_for_windows_distribution(self):
        release = {
            "tag_name": "v0.5.145",
            "assets": [
                {"name": "Torrent-Dashboard-0.5.145.zip"},
                {"name": "TorrentDashboard-Windows-0.5.145-x64.zip"},
            ],
        }
        self.assertEqual(
            find_dashboard_asset(release, "windows-x64")["name"],
            "TorrentDashboard-Windows-0.5.145-x64.zip",
        )

    def test_source_zip_is_preferred_over_windows_preview(self):
        release = {
            "tag_name": "v0.5.144",
            "assets": [
                {"name": "Torrent-Dashboard-0.5.144-windows-x64.zip"},
                {"name": "Torrent-Dashboard-0.5.144.zip"},
            ],
        }
        self.assertEqual(
            find_dashboard_asset(release)["name"],
            "Torrent-Dashboard-0.5.144.zip",
        )

    def test_asset_digest_and_github_release_normalization(self):
        digest = "AB" * 32
        release = {
            "tag_name": "v0.5.95",
            "prerelease": True,
            "published_at": "2026-09-03T12:00:00Z",
            "html_url": "https://github.com/example/repo/releases/tag/v0.5.95",
            "assets": [
                {
                    "name": "Torrent-Dashboard-0.5.95.zip",
                    "digest": f"sha256:{digest}",
                }
            ],
        }
        self.assertEqual(asset_sha256(release["assets"][0]), digest.lower())
        self.assertEqual(
            github_release_integrity([release]),
            [
                {
                    "version": "0.5.95",
                    "sha256": digest.lower(),
                    "package": "Torrent-Dashboard-0.5.95.zip",
                    "publishedAt": "2026-09-03T12:00:00Z",
                    "channel": "prerelease",
                    "releaseUrl": "https://github.com/example/repo/releases/tag/v0.5.95",
                }
            ],
        )

    def test_integrity_normalization_filters_invalid_and_duplicate_rows(self):
        good = "1" * 64
        rows = normalize_release_integrity(
            [
                {"version": "v0.5.95", "sha256": good},
                {"version": "0.5.95", "sha256": "2" * 64},
                {"version": "invalid", "sha256": good},
                {"version": "0.5.94", "sha256": "bad"},
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], "0.5.95")
        self.assertEqual(rows[0]["package"], "Torrent-Dashboard-0.5.95.zip")

    def test_release_info_rejects_malformed_digest(self):
        with self.assertRaises(RuntimeError):
            release_info_payload("0.5.95", "package.zip", "not-a-digest")


class ReleaseProvenanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.release_info = root / "release-info.json"
        self.integrity = root / "data" / "release-integrity.json"
        self.updates = root / "data" / "updates"
        self.notes = root / "release_notes" / "releases.json"
        self.notes.parent.mkdir(parents=True)
        self.notes.write_text(
            json.dumps(
                {
                    "releases": [
                        {
                            "version": "0.5.95",
                            "date": "2026-09-03",
                            "status": "prerelease",
                            "title": "Release provenance module extraction",
                            "summary": "Extract provenance behavior.",
                            "highlights": ["One"],
                            "fixes": [],
                            "technical": [],
                            "validation": [],
                            "known_issues": [],
                        },
                        {
                            "version": "0.5.94",
                            "date": "2026-09-03",
                            "status": "prerelease",
                            "title": "Previous",
                            "summary": "Previous release.",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.store = ReleaseProvenance(
            release_info_path=self.release_info,
            integrity_cache_path=self.integrity,
            updates_dir=self.updates,
            release_notes_path=self.notes,
            version="0.5.95",
            default_repository="CynicaGaming/Torrent-Dashboard",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_installed_release_info_takes_precedence_over_retained_package(self):
        installed_digest = "a" * 64
        package = self.updates / "0.5.95" / "Torrent-Dashboard-0.5.95.zip"
        package.parent.mkdir(parents=True)
        package.write_bytes(b"different retained package")
        self.store.write_release_info(
            self.release_info,
            {
                "version": "0.5.95",
                "package": "installed.zip",
                "sha256": installed_digest,
            },
        )
        info = self.store.installed_release_info()
        self.assertEqual(info["sha256"], installed_digest)
        self.assertEqual(info["package"], "installed.zip")

    def test_retained_verified_package_recovers_missing_release_info(self):
        package = self.updates / "0.5.95" / "Torrent-Dashboard-0.5.95.zip"
        package.parent.mkdir(parents=True)
        package.write_bytes(b"verified update bytes")
        expected = hashlib.sha256(package.read_bytes()).hexdigest()
        info = self.store.installed_release_info()
        self.assertEqual(info["sha256"], expected)
        self.assertTrue(self.release_info.is_file())
        persisted = json.loads(self.release_info.read_text(encoding="utf-8"))
        self.assertEqual(persisted["sha256"], expected)

    def test_malformed_metadata_is_ignored(self):
        self.release_info.write_text("{broken", encoding="utf-8")
        self.integrity.parent.mkdir(parents=True)
        self.integrity.write_text(json.dumps({"schema": 99, "releases": []}), encoding="utf-8")
        self.assertEqual(self.store.installed_release_info(), {})
        self.assertEqual(self.store.cached_release_integrity(), [])

    def test_cache_normalization_and_installed_precedence_in_history(self):
        cached_digest = "b" * 64
        installed_digest = "c" * 64
        self.store.write_release_integrity_cache(
            [
                {
                    "version": "v0.5.95",
                    "sha256": cached_digest,
                    "package": "cached.zip",
                    "channel": "prerelease",
                },
                {"version": "bad", "sha256": "d" * 64},
            ]
        )
        self.store.write_release_info(
            self.release_info,
            {
                "version": "0.5.95",
                "package": "installed.zip",
                "sha256": installed_digest,
            },
        )
        history = self.store.local_release_history(limit=2)
        self.assertEqual([item["version"] for item in history], ["0.5.95", "0.5.94"])
        self.assertEqual(history[0]["sha256"], installed_digest)
        self.assertEqual(history[0]["package"], "installed.zip")
        payload = json.loads(self.integrity.read_text(encoding="utf-8"))
        self.assertEqual(payload["repository"], "CynicaGaming/Torrent-Dashboard")
        self.assertEqual(len(payload["releases"]), 1)

    def test_latest_manifest_merge_preserves_current_override_behavior(self):
        digest = "e" * 64
        history = self.store.local_release_history(
            {
                "version": "0.5.95",
                "title": "Generic GitHub title",
                "publishedAt": "2026-09-03T12:00:00Z",
                "channel": "prerelease",
                "notes": "Remote notes",
                "asset": {"sha256": digest, "name": "Torrent-Dashboard-0.5.95.zip"},
            }
        )
        # Preserve current dashboard behavior: a non-empty remote title overrides
        # the bundled title when the manifest is merged.
        self.assertEqual(history[0]["title"], "Generic GitHub title")
        self.assertEqual(history[0]["sha256"], digest)


if __name__ == "__main__":
    unittest.main()
