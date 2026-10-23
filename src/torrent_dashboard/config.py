"""Configuration schema, migration, browser sanitization, and atomic file persistence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

from .integrations import INTEGRATION_TYPES, normalize_integration, redacted_integrations
from .users import normalize_user, public_user, sync_legacy_auth
from .persistence import atomic_write_json


DEFAULT_UPDATE_REPOSITORY = "CynicaGaming/Torrent-Dashboard"
# Former default repository names. A saved setting that still points at one of these
# was never customized, so it follows the default to its new name. Forks and other
# custom repositories are left unchanged.
RETIRED_UPDATE_REPOSITORIES = ("CynicaGaming/TorrentDashboard",)

DEFAULT_CONFIG = {
    "setup": {"complete": False},
    "dashboard": {
        "title": "Torrent Dashboard",
        "bind_host": "0.0.0.0",
        "port": 8765,
        "open_browser": True,
        "history_retention_days": 30,
        "history_sample_seconds": 10,
        "low_disk_gb": 20,
        "https_enabled": False,
        "https_cert": "",
        "https_key": "",
    },
    "updates": {"repository": DEFAULT_UPDATE_REPOSITORY},
    "auth": {
        "mode": "lan_bypass",
        "trusted_interfaces": [],
        "trusted_ips": [],
        "session_hours": 24,
        "max_login_attempts_per_10m": 20,
    },
    "recovery": {"key_hash": "", "created_at": 0, "last4": ""},
    "users": [],
    "servers": [],
    "notifications": {
        "browser": True,
        "sound": False,
        "sound_mode": "default",
        "volume": 72,
        "custom_sound_file": "",
        "custom_sound_name": "",
        "custom_sound_mime": "",
    },
    "integrations": [],
}


def deep_merge(base, override):
    """Recursively merge JSON-style dictionaries using override values."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def stable_record_id(kind, *parts):
    """Create the deterministic short IDs used by legacy migration paths."""
    raw = kind + ":" + ":".join(str(value or "") for value in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def normalize_github_repository(value: str) -> str:
    """Normalize the public update repository to GitHub's owner/repository form."""
    value = str(value or "").strip().removesuffix(".git").strip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):].strip("/")
            break
    parts = value.split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part or "") for part in parts):
        raise RuntimeError("GitHub repository must be owner/repo or a github.com repository URL")
    return "/".join(parts)


def _migrate_network_auth(raw, detect_lan_network: Callable[[], dict] | None) -> None:
    auth_raw = raw.setdefault("auth", {})
    legacy_cidrs = list(auth_raw.get("trusted_cidrs", []) or [])
    if "trusted_ips" not in auth_raw:
        auth_raw["trusted_ips"] = [
            cidr for cidr in legacy_cidrs if cidr not in ("127.0.0.0/8", "::1/128")
        ]
    if "trusted_interfaces" not in auth_raw:
        auth_raw["trusted_interfaces"] = []
        if auth_raw.get("auto_trust_lan", False) and detect_lan_network is not None:
            try:
                default = detect_lan_network()
                interface = default.get("interface_id") or default.get("interface")
                if interface:
                    auth_raw["trusted_interfaces"] = [interface]
            except Exception:
                pass


def _migrate_users(raw, merged) -> None:
    raw_users = raw.get("users")
    if isinstance(raw_users, list) and raw_users:
        merged["users"] = [
            normalize_user(item, item)
            for item in raw_users
            if isinstance(item, dict)
        ]
        return

    legacy_auth = raw.get("auth", {}) if isinstance(raw.get("auth"), dict) else {}
    legacy_hash = str(legacy_auth.get("password_hash") or "")
    if not legacy_hash:
        merged["users"] = []
        return
    username = str(legacy_auth.get("username") or "admin")[:128]
    merged["users"] = [
        normalize_user(
            {
                "id": stable_record_id("user", username),
                "username": username,
                "password_hash": legacy_hash,
                "group": "administrator",
            },
            require_password=True,
        )
    ]


def _migrate_integrations(raw, merged) -> str:
    raw_integrations = raw.get("integrations")
    legacy_github_repo = ""
    if isinstance(raw_integrations, list):
        migrated = []
        for item in raw_integrations:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() == "github":
                if not legacy_github_repo:
                    legacy_github_repo = str(item.get("repository") or "").strip()
                continue
            try:
                migrated.append(normalize_integration(item, item))
            except Exception:
                continue
        merged["integrations"] = migrated
    elif isinstance(raw_integrations, dict):
        migrated = []
        for provider in ("sonarr", "radarr", "lidarr", "prowlarr", "jellyfin", "plex"):
            value = raw_integrations.get(provider) or {}
            if not isinstance(value, dict) or not value.get("url"):
                continue
            payload = {
                "id": stable_record_id("integration", provider, value.get("url")),
                "type": provider,
                "name": INTEGRATION_TYPES[provider]["label"],
                **value,
            }
            try:
                migrated.append(normalize_integration(payload, payload))
            except Exception:
                continue
        webhook = str(raw_integrations.get("home_assistant_webhook") or "").strip()
        if webhook:
            payload = {
                "id": stable_record_id("integration", "home_assistant", webhook),
                "type": "home_assistant",
                "name": "Home Assistant",
                "webhook_url": webhook,
            }
            try:
                migrated.append(normalize_integration(payload, payload))
            except Exception:
                pass
        merged["integrations"] = migrated
    else:
        merged["integrations"] = []
    return legacy_github_repo


def _migrate_notification_destinations(raw, merged) -> None:
    legacy_notifications = raw.get("notifications", {}) if isinstance(raw.get("notifications"), dict) else {}
    legacy_destinations = [
        ("generic_webhook", "webhook_url", str(legacy_notifications.get("webhook_url") or "").strip()),
        ("discord", "webhook_url", str(legacy_notifications.get("discord_webhook") or "").strip()),
        ("ntfy", "topic_url", str(legacy_notifications.get("ntfy_url") or "").strip()),
    ]
    for provider, field, value in legacy_destinations:
        if not value:
            continue
        if any(
            item.get("type") == provider and item.get(field) == value
            for item in merged.get("integrations", [])
        ):
            continue
        payload = {
            "id": stable_record_id("integration", provider, value),
            "type": provider,
            "name": INTEGRATION_TYPES[provider]["label"],
            field: value,
            "enabled": True,
        }
        try:
            merged.setdefault("integrations", []).append(normalize_integration(payload, payload))
        except Exception:
            pass
    for legacy_key in ("webhook_url", "discord_webhook", "ntfy_url"):
        merged.setdefault("notifications", {}).pop(legacy_key, None)


def normalize_config(raw, detect_lan_network: Callable[[], dict] | None = None):
    """Normalize one parsed configuration and apply all supported legacy migrations."""
    if not isinstance(raw, dict):
        raise RuntimeError("Configuration root must be a JSON object")
    raw = json.loads(json.dumps(raw))

    if "setup" not in raw:
        raw["setup"] = {"complete": True}

    _migrate_network_auth(raw, detect_lan_network)
    merged = deep_merge(DEFAULT_CONFIG, raw)
    merged.setdefault("dashboard", {}).pop("refresh_seconds", None)
    merged.setdefault("dashboard", {}).pop("read_only", None)

    _migrate_users(raw, merged)
    legacy_github_repo = _migrate_integrations(raw, merged)
    _migrate_notification_destinations(raw, merged)

    legacy_updates = raw.get("updates", {}) if isinstance(raw.get("updates"), dict) else {}
    update_repo = str(
        legacy_updates.get("repository")
        or legacy_github_repo
        or DEFAULT_UPDATE_REPOSITORY
    ).strip()
    try:
        update_repo = normalize_github_repository(update_repo)
    except Exception:
        update_repo = DEFAULT_UPDATE_REPOSITORY
    if update_repo.lower() in {repo.lower() for repo in RETIRED_UPDATE_REPOSITORIES}:
        update_repo = DEFAULT_UPDATE_REPOSITORY
    merged["updates"] = {"repository": update_repo}
    merged["integrations"] = [
        item for item in merged.get("integrations", []) if item.get("type") != "github"
    ]

    notifications = merged.setdefault("notifications", {})
    try:
        notification_volume = int(round(float(notifications.get("volume", 72))))
    except (TypeError, ValueError):
        notification_volume = 72
    notifications["volume"] = max(0, min(100, notification_volume))

    recovery = merged.setdefault("recovery", {})
    recovery["key_hash"] = str(recovery.get("key_hash") or "")
    try:
        recovery["created_at"] = int(recovery.get("created_at") or 0)
    except (TypeError, ValueError):
        recovery["created_at"] = 0
    recovery["last4"] = str(recovery.get("last4") or "")[-4:]

    sync_legacy_auth(merged)
    return merged


def public_config(cfg):
    """Return the configuration subset safe to expose to authenticated browsers."""
    out = json.loads(json.dumps(cfg))
    out.setdefault("auth", {}).pop("password_hash", None)
    out.setdefault("auth", {}).pop("username", None)
    recovery = out.setdefault("recovery", {})
    recovery["configured"] = bool(recovery.pop("key_hash", ""))
    for server in out.get("servers", []):
        if server.get("password"):
            server["password"] = "<configured>"
        if server.get("api_key"):
            server["api_key"] = "<configured>"
    out["users"] = [public_user(user) for user in cfg.get("users", [])]
    out["integrations"] = redacted_integrations(cfg)
    notifications = out.get("notifications", {})
    for secret in ("gotify_token", "telegram_bot_token"):
        if notifications.get(secret):
            notifications[secret] = "<configured>"
    return out


class ConfigRepository:
    """Own config.json loading, migration, sanitization-on-save, and atomic persistence."""

    def __init__(self, path: Path | str, detect_lan_network: Callable[[], dict] | None = None):
        self.path = Path(path)
        self._detect_lan_network = detect_lan_network

    def load(self):
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, DEFAULT_CONFIG)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return normalize_config(raw, self._detect_lan_network)

    def save(self, cfg):
        clean = json.loads(json.dumps(cfg))
        clean["integrations"] = [
            item for item in clean.get("integrations", []) if item.get("type") != "github"
        ]
        updates = clean.setdefault("updates", {})
        updates["repository"] = normalize_github_repository(
            updates.get("repository") or DEFAULT_UPDATE_REPOSITORY
        )
        updates.pop("github_token", None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, clean)


__all__ = [
    "ConfigRepository",
    "DEFAULT_CONFIG",
    "DEFAULT_UPDATE_REPOSITORY",
    "RETIRED_UPDATE_REPOSITORIES",
    "deep_merge",
    "normalize_config",
    "normalize_github_repository",
    "public_config",
    "stable_record_id",
]
