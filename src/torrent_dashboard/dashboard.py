#!/usr/bin/env python3
"""Torrent Dashboard HTTP application and composition root."""
from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import hmac
import http.cookiejar
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import shutil
import socket
import subprocess
import queue
import re
import ssl
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import webbrowser
from collections import defaultdict, deque
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in (None, ""):
    _src = Path(__file__).resolve().parents[1]
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from torrent_dashboard import __version__
from torrent_dashboard.config import (
    ConfigRepository,
    DEFAULT_CONFIG,
    DEFAULT_UPDATE_REPOSITORY,
    normalize_github_repository,
    public_config,
)
from torrent_dashboard.config_store import ConfigStore
from torrent_dashboard.history import HistoryStore
from torrent_dashboard.http_input import parse_json_body, parse_multipart
from torrent_dashboard.state_gate import StateGate
from torrent_dashboard.backups import (
    BACKUP_EXTENSION,
    MAX_BACKUP_BYTES,
    backup_path,
    create_backup,
    delete_backup,
    import_backup,
    list_backups,
    restore_backup,
)
from torrent_dashboard.release_provenance import (
    ReleaseProvenance,
    asset_sha256 as _asset_sha256,
    find_dashboard_asset as _find_dashboard_asset,
    github_release_integrity as _github_release_integrity,
    release_info_payload as _release_info_payload,
    version_key as _version_key,
)
from torrent_dashboard.integrations import (
    INTEGRATION_TYPES,
    delete_integration,
    integration_catalog,
    integration_health_statuses,
    normalize_integration,
    redacted_integrations,
    save_integration,
    save_jellyfin_task_favorites,
    test_integration_connection,
)
from torrent_dashboard.jellyfin import (
    find_jellyfin_integration,
    jellyfin_overview,
    jellyfin_scheduled_tasks,
    refresh_jellyfin_libraries,
    start_jellyfin_scheduled_task,
    stop_jellyfin_scheduled_task,
)
from torrent_dashboard.recovery import (
    RECOVERY_ACCOUNT_ID,
    RECOVERY_ACCOUNT_USERNAME,
    generate_dashboard_recovery_key,
    recovery_key_record,
    verify_dashboard_recovery_key,
)
from torrent_dashboard.runtime_paths import (
    app_dir,
    is_frozen,
    runtime_distribution,
    source_python_env,
    updater_command,
)
from torrent_dashboard.users import (
    AVATAR_DIR,
    MAX_AVATAR_BYTES,
    PROFILE_AVATAR_TYPES,
    USER_GROUPS,
    approve_user,
    change_current_user_password,
    configured_user_avatar,
    delete_user,
    delete_user_avatar_files,
    hash_password,
    normalize_user,
    public_user,
    register_user,
    reject_user,
    remove_user_avatar,
    save_current_user_profile,
    save_user,
    session_is_admin,
    store_user_avatar,
    sync_legacy_auth,
    user_by_id,
    user_by_username,
    user_display_name,
    verify_password,
)

APP_DIR = app_dir()
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = DATA_DIR / "torrent_desk.sqlite3"
UPDATE_DIR = DATA_DIR / "updates"
UPDATE_STATE_PATH = DATA_DIR / "update-status.json"
RELEASE_INFO_PATH = APP_DIR / "release-info.json"
RELEASE_INTEGRITY_CACHE_PATH = DATA_DIR / "release-integrity.json"
CUSTOM_SOUND_BASENAME = "notification-custom"
LEGACY_CUSTOM_SOUND_BASENAME = "custom-notification-sound"
MAX_CUSTOM_SOUND_BYTES = 2 * 1024 * 1024
VERSION = __version__
STATUS_REFRESH_SECONDS = 1.0

RELEASE_PROVENANCE = ReleaseProvenance(
    release_info_path=RELEASE_INFO_PATH,
    integrity_cache_path=RELEASE_INTEGRITY_CACHE_PATH,
    updates_dir=UPDATE_DIR,
    release_notes_path=APP_DIR / "release_notes" / "releases.json",
    version=VERSION,
    default_repository=DEFAULT_UPDATE_REPOSITORY,
)
cached_release_integrity = RELEASE_PROVENANCE.cached_release_integrity
write_release_integrity_cache = RELEASE_PROVENANCE.write_release_integrity_cache
write_release_info = RELEASE_PROVENANCE.write_release_info
installed_release_info = RELEASE_PROVENANCE.installed_release_info
local_release_history = RELEASE_PROVENANCE.local_release_history


class SingleInstanceLock:
    """Machine-level guard that prevents two dashboard processes from running."""

    def __init__(self):
        self._handle = None
        self._file = None

    def acquire(self):
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, "Local\\TorrentDashboard.SingleInstance")
            if not handle:
                raise OSError("Could not create the Torrent Dashboard instance mutex")
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True

        import fcntl
        lock_path = Path(tempfile.gettempdir()) / "torrent-dashboard.lock"
        lock_file = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return False
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        self._file = lock_file
        return True

    def release(self):
        if self._handle is not None:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        if self._file is not None:
            try:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None


CONFIG_REPOSITORY = ConfigRepository(
    CONFIG_PATH,
    detect_lan_network=lambda: detect_lan_network(),
)
CONFIG_STORE = ConfigStore(CONFIG_REPOSITORY.load, CONFIG_REPOSITORY.save)
STATE_GATE = StateGate()


def load_config():
    return CONFIG_STORE.load()


def mutate_config(transform):
    return CONFIG_STORE.mutate(transform)


class SessionStore:
    PURGE_INTERVAL_SECONDS = 300

    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}
        self._last_purge = 0.0

    def _purge_expired_locked(self, now):
        # Expired sessions were previously only dropped when their own token was
        # presented again, so abandoned sessions accumulated for the life of the
        # process. Sweep periodically whenever a new session is created.
        if now - self._last_purge < self.PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        for token in [t for t, item in self.sessions.items() if item["expires"] < now]:
            self.sessions.pop(token, None)

    def create(self, username, hours, auth_kind, group="administrator", user_id="", display_name=""):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        expires = now + max(1, float(hours)) * 3600
        with self.lock:
            self._purge_expired_locked(now)
            self.sessions[token] = {
                "username": username,
                "csrf": csrf,
                "expires": expires,
                "auth_kind": auth_kind,
                "group": group,
                "user_id": user_id,
                "display_name": display_name or username,
            }
            return token, dict(self.sessions[token])

    def get(self, token):
        if not token:
            return None
        with self.lock:
            item = self.sessions.get(token)
            if not item:
                return None
            if item["expires"] < time.time():
                self.sessions.pop(token, None)
                return None
            return dict(item)

    def remove(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def update_user(self, user):
        uid = str(user.get("id") or "")
        if not uid:
            return
        with self.lock:
            for item in self.sessions.values():
                if item.get("user_id") == uid:
                    item["username"] = user.get("username", item.get("username", ""))
                    item["group"] = user.get("group", item.get("group", "standard"))
                    item["display_name"] = user_display_name(user)

    def remove_user(self, user_id):
        uid = str(user_id or "")
        with self.lock:
            doomed = [token for token, item in self.sessions.items() if item.get("user_id") == uid]
            for token in doomed:
                self.sessions.pop(token, None)

    def remove_user_except(self, user_id, keep_token):
        uid = str(user_id or "")
        with self.lock:
            doomed = [token for token, item in self.sessions.items() if item.get("user_id") == uid and token != keep_token]
            for token in doomed:
                self.sessions.pop(token, None)

    def clear(self):
        with self.lock:
            self.sessions.clear()


SESSIONS = SessionStore()
LOGIN_ATTEMPTS = defaultdict(deque)
LOGIN_LOCK = threading.Lock()
REGISTRATION_ATTEMPTS = defaultdict(deque)
REGISTRATION_LOCK = threading.Lock()
RECOVERY_LOCK = threading.Lock()
RECOVERY_ATTEMPTS = defaultdict(deque)
# Verified when a login names an unknown user so the response time does not
# reveal which usernames exist.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def _record_attempt(attempts, ip, now, window, limit):
    """Sliding-window rate limit. Returns False when ``ip`` is over ``limit``.

    Caller must hold the lock that guards ``attempts``. Idle IP entries are
    removed so the table cannot grow without bound.
    """
    q = attempts[ip]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    if len(attempts) > 1024:
        for key in [k for k, v in attempts.items() if not v or v[-1] < now - window]:
            attempts.pop(key, None)
    return True


def normalize_trusted_entry(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        if "/" in value:
            return str(ipaddress.ip_network(value, strict=False))
        addr = ipaddress.ip_address(value)
        return f"{addr}/{32 if addr.version == 4 else 128}"
    except Exception as exc:
        raise RuntimeError(f"Invalid IP whitelist entry: {value}") from exc


def is_trusted_ip(ip, networks):
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(c, strict=False) for c in networks)
    except Exception:
        return False


def _usable_range(network):
    if not network:
        return None, None
    try:
        if network.version == 4 and network.prefixlen <= 30:
            return str(network.network_address + 1), str(network.broadcast_address - 1)
        if network.version == 6 and network.prefixlen < 127:
            return str(network.network_address + 1), str(network.broadcast_address - 1)
        return str(network.network_address), str(network.broadcast_address)
    except Exception:
        return None, None


def _network_result(address, prefix, gateway="", interface="", source="", interface_id="", is_default=False):
    try:
        addr = ipaddress.ip_address(str(address).strip())
        if addr.version != 4 or addr.is_loopback or addr.is_link_local:
            return None
        network = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
        first, last = _usable_range(network)
        return {
            "detected": True,
            "interface": interface or interface_id or "",
            "interface_id": interface_id or interface or "",
            "address": str(addr),
            "gateway": str(gateway or ""),
            "cidr": str(network),
            "prefix": int(network.prefixlen),
            "netmask": str(network.netmask),
            "range_start": first,
            "range_end": last,
            "source": source,
            "default": bool(is_default or gateway),
        }
    except Exception:
        return None


def _clean_windows_adapter_name(heading):
    name = re.sub(r"^.*?adapter\s+", "", str(heading or ""), flags=re.I).strip()
    return name or str(heading or "").strip()


def _parse_windows_interfaces(text):
    pattern = re.compile(
        r"(?ms)^([^\r\n]*adapter\s+[^:\r\n]+):\s*\r?\n(.*?)(?=^[^\r\n]*adapter\s+[^:\r\n]+:\s*$|\Z)",
        re.I,
    )
    results = []
    for heading, block in pattern.findall(text or ""):
        ip_m = re.search(r"IPv4 Address[^:]*:\s*([0-9.]+)", block, re.I)
        mask_m = re.search(r"Subnet Mask[^:]*:\s*([0-9.]+)", block, re.I)
        if not ip_m or not mask_m:
            continue
        gateway = ""
        gw_pos = re.search(r"Default Gateway[^:]*:", block, re.I)
        if gw_pos:
            tail = "\n".join(block[gw_pos.end():].splitlines()[:3])
            ips = re.findall(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])", tail)
            gateway = ips[0] if ips else ""
        try:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask_m.group(1)}").prefixlen
        except Exception:
            continue
        name = _clean_windows_adapter_name(heading)
        result = _network_result(ip_m.group(1), prefix, gateway, name, "ipconfig", name, bool(gateway))
        if result:
            results.append(result)
    return results


def _windows_background_process_kwargs():
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def _detect_windows_interfaces():
    try:
        out = subprocess.check_output(["ipconfig"], text=True, errors="replace", timeout=4, **_windows_background_process_kwargs())
        return _parse_windows_interfaces(out)
    except Exception:
        return []


def _detect_linux_interfaces():
    results = []
    default_gateway = ""
    default_interface = ""
    try:
        route = subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True, errors="replace", timeout=4)
        m = re.search(r"default(?:\s+via\s+(\S+))?.*?\sdev\s+(\S+)", route)
        if m:
            default_gateway, default_interface = m.group(1) or "", m.group(2)
    except Exception:
        pass
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show", "scope", "global"], text=True, errors="replace", timeout=4)
        for line in out.splitlines():
            m = re.search(r"^\d+:\s+([^\s]+)\s+inet\s+([0-9.]+)/(\d+)", line)
            if not m:
                continue
            interface, address, prefix = m.group(1).split("@")[0], m.group(2), int(m.group(3))
            gateway = default_gateway if interface == default_interface else ""
            result = _network_result(address, prefix, gateway, interface, "ip addr", interface, interface == default_interface)
            if result:
                results.append(result)
    except Exception:
        pass
    return results


def _detect_macos_interfaces():
    results = []
    default_gateway = ""
    default_interface = ""
    try:
        route = subprocess.check_output(["route", "-n", "get", "default"], text=True, errors="replace", timeout=4)
        gm = re.search(r"gateway:\s*(\S+)", route)
        im = re.search(r"interface:\s*(\S+)", route)
        default_gateway = gm.group(1) if gm else ""
        default_interface = im.group(1) if im else ""
    except Exception:
        pass
    try:
        out = subprocess.check_output(["ifconfig"], text=True, errors="replace", timeout=4)
        blocks = re.split(r"(?m)^(?=[A-Za-z0-9_.-]+:\s)", out)
        for block in blocks:
            head = re.match(r"^([A-Za-z0-9_.-]+):", block)
            if not head:
                continue
            interface = head.group(1)
            for m in re.finditer(r"\binet\s+([0-9.]+)\s+netmask\s+(0x[0-9a-fA-F]+|[0-9.]+)", block):
                address, mask = m.group(1), m.group(2)
                if address.startswith("127."):
                    continue
                if mask.lower().startswith("0x"):
                    n = int(mask, 16)
                    mask = ".".join(str((n >> shift) & 255) for shift in (24, 16, 8, 0))
                try:
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                except Exception:
                    continue
                gateway = default_gateway if interface == default_interface else ""
                result = _network_result(address, prefix, gateway, interface, "ifconfig", interface, interface == default_interface)
                if result:
                    results.append(result)
    except Exception:
        pass
    return results


LAN_DETECT_LOCK = threading.Lock()
LAN_DETECT_CACHE = {"ts": 0.0, "interfaces": []}


def detect_network_interfaces(force=False):
    now = time.time()
    with LAN_DETECT_LOCK:
        cached = LAN_DETECT_CACHE.get("interfaces") or []
        if not force and cached and now - LAN_DETECT_CACHE.get("ts", 0) < 30:
            return json.loads(json.dumps(cached))
    if os.name == "nt":
        results = _detect_windows_interfaces()
    elif sys.platform == "darwin":
        results = _detect_macos_interfaces()
    else:
        results = _detect_linux_interfaces()
    # Collapse duplicate interface/address pairs and prefer the default route first.
    unique = {}
    for item in results:
        unique[(item.get("interface_id"), item.get("address"))] = item
    results = sorted(unique.values(), key=lambda x: (not bool(x.get("default")), str(x.get("interface", "")).lower(), x.get("address", "")))
    with LAN_DETECT_LOCK:
        LAN_DETECT_CACHE["ts"] = now
        LAN_DETECT_CACHE["interfaces"] = json.loads(json.dumps(results))
    return json.loads(json.dumps(results))


def detect_lan_network(force=False):
    interfaces = detect_network_interfaces(force)
    if interfaces:
        return dict(next((x for x in interfaces if x.get("default")), interfaces[0]))
    # Fallback: identify the preferred local address. Do not invent a prefix.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("1.1.1.1", 53))
        address = sock.getsockname()[0]
        sock.close()
    except Exception:
        try:
            address = socket.gethostbyname(socket.gethostname())
        except Exception:
            address = "127.0.0.1"
    return {"detected": False, "address": address, "gateway": "", "cidr": "", "prefix": None,
            "netmask": "", "range_start": "", "range_end": "", "interface": "", "interface_id": "",
            "source": "fallback", "default": False}


def local_lan_ip():
    return detect_lan_network().get("address") or "127.0.0.1"


def interface_networks(interface_ids):
    wanted = {str(x) for x in (interface_ids or []) if str(x)}
    if not wanted:
        return []
    return [x for x in detect_network_interfaces() if x.get("interface_id") in wanted and x.get("cidr")]


def effective_trusted_cidrs(auth_cfg):
    networks = ["127.0.0.0/8", "::1/128"]
    for item in interface_networks(auth_cfg.get("trusted_interfaces", [])):
        networks.append(item["cidr"])
    manual = auth_cfg.get("trusted_ips")
    if manual is None:
        manual = auth_cfg.get("trusted_cidrs", []) or []
    for value in manual or []:
        normalized = normalize_trusted_entry(value)
        if normalized:
            networks.append(normalized)
    return list(dict.fromkeys(c for c in networks if c))


def is_loopback_ip(ip):
    try:
        return ipaddress.ip_address(ip).is_loopback
    except Exception:
        return False


SETUP_CODE = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()



def normalize_qbittorrent_server(data, existing=None):
    existing = existing or {}
    sid = str(data.get("id") or existing.get("id") or uuid.uuid4().hex[:8])[:64]
    auth_method = str(data.get("auth_method") or existing.get("auth_method") or ("api_key" if existing.get("api_key") else "password"))
    if auth_method not in ("api_key", "password"):
        raise RuntimeError("qBitTorrent authentication must be API key or username/password")

    password = data.get("password")
    if password in (None, "", "<configured>"):
        password = existing.get("password", "")
    api_key = data.get("api_key")
    if api_key in (None, "", "<configured>"):
        api_key = existing.get("api_key", "")

    item = {
        "id": sid,
        "name": str(data.get("name") or existing.get("name") or "qBittorrent")[:128],
        "type": "qbittorrent",
        "base_url": str(data.get("base_url") or existing.get("base_url") or "").strip().rstrip("/")[:2048],
        "auth_method": auth_method,
        "api_key": str(api_key or ""),
        "username": str(data.get("username") if data.get("username") is not None else existing.get("username", ""))[:256],
        "password": str(password or ""),
        "enabled": bool(data.get("enabled", True)),
    }
    if not item["base_url"].startswith(("http://", "https://")):
        raise RuntimeError("qBitTorrent URL must start with http:// or https://")
    if auth_method == "api_key":
        if not item["api_key"]:
            raise RuntimeError("Enter the qBitTorrent API key")
        if not (item["api_key"].startswith("qbt_") and len(item["api_key"]) == 32):
            raise RuntimeError("qBitTorrent API keys must be 32 characters and start with qbt_ (qBitTorrent 5.2+)")
    else:
        if not item["username"]:
            raise RuntimeError("Enter the qBitTorrent username")
        if not item["password"]:
            raise RuntimeError("Enter the qBitTorrent password")
    return item


def test_server_connection(server):
    client = QBitClient(server)
    client.login()
    version = client.get_text("/api/v2/app/version")
    api_version = client.get_text("/api/v2/app/webapiVersion")
    return {
        "ok": True,
        "name": server.get("name", "qBittorrent"),
        "version": version,
        "api_version": api_version,
        "base_url": server.get("base_url"),
    }


QBIT_PROXY_TYPES = {"none", "http", "socks5", "socks4"}


def normalize_qbittorrent_proxy_type(value):
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", "")
        return {"none": "none", "http": "http", "socks5": "socks5", "socks4": "socks4"}.get(normalized, "none")
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return "none"
    return {-1: "none", 0: "none", 1: "http", 2: "socks5", 3: "http", 4: "socks5", 5: "socks4"}.get(numeric, "none")


def encode_qbittorrent_proxy_type(proxy_type, auth_enabled, current_value):
    proxy_type = str(proxy_type or "none").strip().lower()
    if proxy_type not in QBIT_PROXY_TYPES:
        raise RuntimeError("Unsupported proxy type")
    if isinstance(current_value, str):
        return {"none": "None", "http": "HTTP", "socks5": "SOCKS5", "socks4": "SOCKS4"}[proxy_type]
    if proxy_type == "none":
        return 0 if current_value == 0 else -1
    if proxy_type == "http":
        return 3 if auth_enabled else 1
    if proxy_type == "socks5":
        return 4 if auth_enabled else 2
    return 5


def _qbit_rate_to_kb(value):
    try:
        return max(0, round(int(value or 0) / 1024))
    except (TypeError, ValueError):
        return 0


def _qbit_int(value, label, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be a whole number") from exc
    if number < minimum or number > maximum:
        raise RuntimeError(f"{label} must be between {minimum} and {maximum}")
    return number


def _qbit_rate_from_kb(value, label):
    kb = _qbit_int(value, label, 0, 2_000_000_000)
    return kb * 1024


class QBitClient:
    def __init__(self, server):
        self.server = server
        self.base = server["base_url"].rstrip("/")
        self.lock = threading.RLock()
        self.opener = None
        # Prevent bad password credentials from being retried every dashboard
        # refresh and tripping qBittorrent's Web UI IP-ban protection. API-key
        # authentication is stateless and never calls the login endpoint.
        self.auth_blocked_error = None
        self._value_cache = {}

    def _make_opener(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [
            ("User-Agent", f"TorrentDashboard/{VERSION}"),
            ("Referer", self.base + "/"),
            ("Origin", self.base)
        ]
        if self.server.get("auth_method") == "api_key":
            opener.addheaders.append(("Authorization", "Bearer " + self.server.get("api_key", "")))
        return opener

    def login(self):
        if self.auth_blocked_error:
            raise RuntimeError(self.auth_blocked_error)

        if self.server.get("auth_method") == "api_key":
            if not self.server.get("api_key"):
                raise RuntimeError("qBittorrent API key is missing")
            self.opener = self._make_opener()
            self.auth_blocked_error = None
            return

        opener = self._make_opener()
        data = urllib.parse.urlencode({
            "username": self.server.get("username", ""),
            "password": self.server.get("password", "")
        }).encode()
        req = urllib.request.Request(self.base + "/api/v2/auth/login", data=data, method="POST")
        try:
            with opener.open(req, timeout=7) as r:
                body = r.read().decode(errors="replace").strip()
            if body.lower() != "ok.":
                # qBittorrent normally returns HTTP 200 + "Fails." for bad
                # credentials. Do not keep retrying, because enough failures
                # cause qBittorrent to ban this client's IP.
                self.opener = None
                self.auth_blocked_error = (
                    "qBittorrent rejected the username/password. Automatic login retries "
                    "are paused to avoid an IP ban. Verify the server credentials in "
                    "Setup/Settings, then save them before retrying."
                )
                raise RuntimeError(self.auth_blocked_error)
            self.opener = opener
            self.auth_blocked_error = None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace").strip()
            except Exception:
                pass
            self.opener = None
            if e.code == 403:
                self.auth_blocked_error = (
                    "qBittorrent has banned this dashboard IP after too many failed Web UI "
                    "login attempts (HTTP 403). Torrent Dashboard has stopped retrying. Stop Torrent Dashboard, "
                    "wait for/clear the qBittorrent Web UI ban (or restart qBittorrent), verify the "
                    "username/password in Setup/Settings, then retry after the qBittorrent ban clears."
                )
                raise RuntimeError(self.auth_blocked_error) from e
            raise RuntimeError(f"qBittorrent login HTTP {e.code}: {detail or 'login failed'}") from e
        except urllib.error.URLError as e:
            self.opener = None
            raise RuntimeError(f"Cannot reach qBittorrent at {self.base}: {e.reason}") from e

    def _request(self, method, path, form=None, raw=None, headers=None, expect_json=False):
        with self.lock:
            if self.opener is None:
                self.login()
            url = self.base + path
            data = raw
            hdrs = dict(headers or {})
            if form is not None:
                data = urllib.parse.urlencode(form, doseq=True).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with self.opener.open(req, timeout=12) as r:
                    body = r.read()
                    status = r.status
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code in (401, 403):
                    if self.server.get("auth_method") == "api_key":
                        raise RuntimeError(
                            f"qBittorrent rejected the API key (HTTP {e.code}). API-key authentication "
                            "requires qBittorrent 5.2.0+ / WebAPI 2.14.1+; verify or rotate the key in "
                            "qBittorrent Preferences → Web UI → API Key."
                        ) from e
                    self.opener = None
                    self.login()
                    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
                    try:
                        with self.opener.open(req, timeout=12) as r:
                            body = r.read(); status = r.status
                    except urllib.error.HTTPError as retry_error:
                        # Previously this escaped as a raw HTTPError.
                        self.opener = None
                        retry_detail = retry_error.read().decode(errors="replace")[:500]
                        raise RuntimeError(f"qBittorrent HTTP {retry_error.code}: {retry_detail or path}") from retry_error
                    except urllib.error.URLError as retry_error:
                        raise RuntimeError(f"qBittorrent connection error: {retry_error.reason}") from retry_error
                else:
                    raise RuntimeError(f"qBittorrent HTTP {e.code}: {detail or path}") from e
            except urllib.error.URLError as e:
                raise RuntimeError(f"qBittorrent connection error: {e.reason}") from e
            if expect_json:
                if not body:
                    return None
                return json.loads(body.decode())
            return status, body

    def get_json(self, path, params=None):
        if params:
            path += "?" + urllib.parse.urlencode(params, doseq=True)
        return self._request("GET", path, expect_json=True)

    def get_text(self, path, params=None):
        if params:
            path += "?" + urllib.parse.urlencode(params, doseq=True)
        _, body = self._request("GET", path, expect_json=False)
        return body.decode(errors="replace").strip()

    def post(self, path, form=None):
        return self._request("POST", path, form=form or {})

    @staticmethod
    def _torrent_metadata_json(body):
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("qBittorrent returned invalid torrent metadata") from exc

    @staticmethod
    def _metadata_api_error(exc):
        message = str(exc)
        if "qBittorrent HTTP 404" in message:
            return RuntimeError("Torrent metadata preview requires qBittorrent Web API 2.11.9 or newer")
        return RuntimeError(message)

    def fetch_torrent_metadata(self, source, downloader=""):
        source = str(source or "").strip()[:16000]
        if not source:
            raise RuntimeError("A magnet link or torrent URL is required")
        form = {"source": source}
        downloader = str(downloader or "").strip()[:256]
        if downloader:
            form["downloader"] = downloader
        try:
            status, body = self.post("/api/v2/torrents/fetchMetadata", form)
        except RuntimeError as exc:
            raise self._metadata_api_error(exc) from exc
        return {
            "qbit_status": int(status),
            "complete": int(status) == 200,
            "metadata": self._torrent_metadata_json(body),
        }

    def parse_torrent_metadata(self, filename, content):
        if not content:
            raise RuntimeError("No .torrent file supplied")
        boundary = "----TorrentDashboardMetadata" + secrets.token_hex(12)
        safe_name = Path(str(filename or "torrent.torrent")).name.replace('"', "")[:255] or "torrent.torrent"
        raw = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"{safe_name}\"\r\n"
            "Content-Type: application/x-bittorrent\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        try:
            status, body = self._request(
                "POST", "/api/v2/torrents/parseMetadata", raw=raw,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
        except RuntimeError as exc:
            raise self._metadata_api_error(exc) from exc
        return {
            "qbit_status": int(status),
            "metadata": self._torrent_metadata_json(body),
        }

    def save_torrent_metadata(self, source, torrent_id=""):
        source = str(source or "").strip()[:16000]
        torrent_id = str(torrent_id or "").strip()[:256]
        candidates = []
        for value in (source, torrent_id):
            if value and value not in candidates:
                candidates.append(value)
        if not candidates:
            raise RuntimeError("Torrent metadata source is required")

        errors = []
        for candidate in candidates:
            route = "/api/v2/torrents/saveMetadata?" + urllib.parse.urlencode({"source": candidate})
            try:
                status, body = self._request("GET", route, expect_json=False)
                if body:
                    return int(status), body
                errors.append(RuntimeError("qBittorrent returned an empty torrent metadata file"))
            except RuntimeError as exc:
                errors.append(exc)

        export_hash = torrent_id
        if not export_hash and re.fullmatch(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}", source):
            export_hash = source
        if export_hash:
            route = "/api/v2/torrents/export?" + urllib.parse.urlencode({"hash": export_hash})
            try:
                status, body = self._request("GET", route, expect_json=False)
                if body:
                    return int(status), body
                errors.append(RuntimeError("qBittorrent returned an empty torrent file"))
            except RuntimeError as exc:
                errors.append(exc)

        if errors and all("qBittorrent HTTP 404" in str(error) for error in errors):
            raise self._metadata_api_error(errors[0]) from errors[0]
        detail = str(errors[-1]) if errors else "metadata is unavailable"
        raise RuntimeError(f"Torrent metadata is not available to save yet: {detail}")

    VERSION_CACHE_SECONDS = 300
    PREFERENCES_CACHE_SECONDS = 30

    def _cached(self, key, ttl, loader):
        """Return a cached value younger than ``ttl`` seconds, else reload it.

        The collector polls every second; app versions and preferences rarely
        change, so re-fetching them on every pass only loads qBittorrent.
        """
        now = time.monotonic()
        with self.lock:
            hit = self._value_cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = loader()
        with self.lock:
            self._value_cache[key] = (now, value)
        return value

    def invalidate_cache(self):
        with self.lock:
            self._value_cache.clear()

    def _optional_text(self, path):
        try:
            return self.get_text(path)
        except Exception:
            return None

    def info(self):
        torrents = self.get_json("/api/v2/torrents/info") or []
        transfer = self.get_json("/api/v2/transfer/info") or {}
        app_version = self._cached("app_version", self.VERSION_CACHE_SECONDS, lambda: self._optional_text("/api/v2/app/version"))
        api_version = self._cached("api_version", self.VERSION_CACHE_SECONDS, lambda: self._optional_text("/api/v2/app/webapiVersion"))
        return torrents, transfer, app_version, api_version

    def preferences(self):
        return self.get_json("/api/v2/app/preferences") or {}

    def cached_preferences(self):
        return self._cached("preferences", self.PREFERENCES_CACHE_SECONDS, self.preferences)

    def metadata(self):
        # Browser-facing metadata intentionally excludes app/preferences. qBitTorrent
        # preferences can contain proxy, mail, Web UI, and certificate secrets.
        out = {}
        for key, path in {
            "categories": "/api/v2/torrents/categories",
            "tags": "/api/v2/torrents/tags",
            "alt_speed": "/api/v2/transfer/speedLimitsMode",
            "global_dl_limit": "/api/v2/transfer/downloadLimit",
            "global_up_limit": "/api/v2/transfer/uploadLimit",
        }.items():
            try:
                out[key] = self.get_json(path)
            except Exception:
                out[key] = None
        return out

    def client_settings(self):
        prefs = self.preferences()
        try:
            alt_speed = int(self.get_json("/api/v2/transfer/speedLimitsMode") or 0) == 1
        except Exception:
            alt_speed = False
        raw_proxy_type = prefs.get("proxy_type")
        proxy_type = normalize_qbittorrent_proxy_type(raw_proxy_type)
        legacy_auth = raw_proxy_type in (3, 4)
        return {
            "downloads": {
                "save_path": str(prefs.get("save_path") or ""),
                "temp_path_enabled": bool(prefs.get("temp_path_enabled", False)),
                "temp_path": str(prefs.get("temp_path") or ""),
            },
            "speed": {
                "alternative_enabled": alt_speed,
                "download_limit_kb": _qbit_rate_to_kb(prefs.get("dl_limit", 0)),
                "upload_limit_kb": _qbit_rate_to_kb(prefs.get("up_limit", 0)),
                "alternative_download_limit_kb": _qbit_rate_to_kb(prefs.get("alt_dl_limit", 0)),
                "alternative_upload_limit_kb": _qbit_rate_to_kb(prefs.get("alt_up_limit", 0)),
            },
            "connection": {
                "listen_port": int(prefs.get("listen_port", 0) or 0),
                "random_port": bool(prefs.get("random_port", False) or int(prefs.get("listen_port", 0) or 0) == 0),
                "upnp": bool(prefs.get("upnp", False)),
                "max_connections": int(prefs.get("max_connec", -1) or 0),
                "max_connections_per_torrent": int(prefs.get("max_connec_per_torrent", -1) or 0),
                "max_upload_slots": int(prefs.get("max_uploads", -1) or 0),
                "max_upload_slots_per_torrent": int(prefs.get("max_uploads_per_torrent", -1) or 0),
            },
            "proxy": {
                "type": proxy_type,
                "host": str(prefs.get("proxy_ip") or ""),
                "port": int(prefs.get("proxy_port", 0) or 0),
                "authentication": bool(prefs.get("proxy_auth_enabled", legacy_auth)),
                "username": str(prefs.get("proxy_username") or ""),
                "password_configured": bool(prefs.get("proxy_password")),
                "hostname_lookup": bool(prefs.get("proxy_hostname_lookup", False)),
                "hostname_lookup_supported": "proxy_hostname_lookup" in prefs,
                "bittorrent": bool(prefs.get("proxy_bittorrent", True)),
                "bittorrent_supported": "proxy_bittorrent" in prefs,
                "peer_connections": bool(prefs.get("proxy_peer_connections", False)),
                "peer_connections_supported": "proxy_peer_connections" in prefs,
            },
        }

    def update_client_settings(self, data):
        if not isinstance(data, dict):
            raise RuntimeError("Client settings payload must be an object")
        downloads = data.get("downloads") or {}
        speed = data.get("speed") or {}
        connection = data.get("connection") or {}
        proxy = data.get("proxy") or {}
        if not all(isinstance(x, dict) for x in (downloads, speed, connection, proxy)):
            raise RuntimeError("Client settings sections must be objects")

        save_path = str(downloads.get("save_path") or "").strip()[:4096]
        temp_path = str(downloads.get("temp_path") or "").strip()[:4096]
        temp_path_enabled = bool(downloads.get("temp_path_enabled", False))
        if not save_path:
            raise RuntimeError("Default save path is required")
        if temp_path_enabled and not temp_path:
            raise RuntimeError("Incomplete torrent path is required when the separate path is enabled")

        current = self.preferences()
        update = {
            "save_path": save_path,
            "temp_path_enabled": temp_path_enabled,
            "temp_path": temp_path,
            "dl_limit": _qbit_rate_from_kb(speed.get("download_limit_kb", 0), "Download limit"),
            "up_limit": _qbit_rate_from_kb(speed.get("upload_limit_kb", 0), "Upload limit"),
            "alt_dl_limit": _qbit_rate_from_kb(speed.get("alternative_download_limit_kb", 0), "Alternative download limit"),
            "alt_up_limit": _qbit_rate_from_kb(speed.get("alternative_upload_limit_kb", 0), "Alternative upload limit"),
            "upnp": bool(connection.get("upnp", False)),
            "max_connec": _qbit_int(connection.get("max_connections", -1), "Global connection limit", -1, 1_000_000),
            "max_connec_per_torrent": _qbit_int(connection.get("max_connections_per_torrent", -1), "Per-torrent connection limit", -1, 1_000_000),
            "max_uploads": _qbit_int(connection.get("max_upload_slots", -1), "Global upload slot limit", -1, 1_000_000),
            "max_uploads_per_torrent": _qbit_int(connection.get("max_upload_slots_per_torrent", -1), "Per-torrent upload slot limit", -1, 1_000_000),
        }

        random_port = bool(connection.get("random_port", False))
        update["random_port"] = random_port
        if not random_port:
            update["listen_port"] = _qbit_int(connection.get("listen_port", 0), "Listening port", 1, 65535)

        proxy_type = str(proxy.get("type") or "none").strip().lower()
        proxy_auth = bool(proxy.get("authentication", False)) and proxy_type not in ("none", "socks4")
        update["proxy_type"] = encode_qbittorrent_proxy_type(proxy_type, proxy_auth, current.get("proxy_type"))
        if "proxy_auth_enabled" in current:
            update["proxy_auth_enabled"] = proxy_auth
        if proxy_type != "none":
            host = str(proxy.get("host") or "").strip()
            if not host:
                raise RuntimeError("Proxy host is required when a proxy is enabled")
            update["proxy_ip"] = host[:1024]
            update["proxy_port"] = _qbit_int(proxy.get("port", 0), "Proxy port", 1, 65535)
            update["proxy_username"] = str(proxy.get("username") or "")[:512] if proxy_auth else ""
            password = proxy.get("password")
            if proxy_auth and password not in (None, "", "<configured>") and "•" not in str(password):
                update["proxy_password"] = str(password)[:4096]
        if "proxy_hostname_lookup" in current:
            update["proxy_hostname_lookup"] = bool(proxy.get("hostname_lookup", False))
        if "proxy_bittorrent" in current:
            update["proxy_bittorrent"] = bool(proxy.get("bittorrent", True))
        if "proxy_peer_connections" in current:
            update["proxy_peer_connections"] = bool(proxy.get("peer_connections", False))

        self.post("/api/v2/app/setPreferences", {"json": json.dumps(update, separators=(",", ":"))})
        try:
            current_alt = int(self.get_json("/api/v2/transfer/speedLimitsMode") or 0) == 1
        except Exception:
            current_alt = False
        requested_alt = bool(speed.get("alternative_enabled", False))
        if requested_alt != current_alt:
            self.post("/api/v2/transfer/toggleSpeedLimitsMode", {})
        self.invalidate_cache()
        return self.client_settings()

    def detail(self, hash_):
        paths = {
            "properties": ("/api/v2/torrents/properties", {"hash": hash_}),
            "trackers": ("/api/v2/torrents/trackers", {"hash": hash_}),
            "files": ("/api/v2/torrents/files", {"hash": hash_}),
            "pieces": ("/api/v2/torrents/pieceStates", {"hash": hash_}),
            "peers": ("/api/v2/sync/torrentPeers", {"hash": hash_, "rid": 0}),
            "webseeds": ("/api/v2/torrents/webseeds", {"hash": hash_}),
        }
        out = {}
        for key, (path, params) in paths.items():
            try:
                out[key] = self.get_json(path, params)
            except Exception as e:
                out[key] = {"error": str(e)} if key == "peers" else [] if key in ("trackers", "files", "pieces") else {}
        return out

    def action(self, action, payload):
        hashes = payload.get("hashes") or payload.get("hash") or ""
        if isinstance(hashes, list):
            hashes = "|".join(hashes)
        simple = {
            "stop": ("/api/v2/torrents/stop", {"hashes": hashes}),
            "start": ("/api/v2/torrents/start", {"hashes": hashes}),
            "recheck": ("/api/v2/torrents/recheck", {"hashes": hashes}),
            "reannounce": ("/api/v2/torrents/reannounce", {"hashes": hashes}),
            "increase_priority": ("/api/v2/torrents/increasePrio", {"hashes": hashes}),
            "decrease_priority": ("/api/v2/torrents/decreasePrio", {"hashes": hashes}),
            "top_priority": ("/api/v2/torrents/topPrio", {"hashes": hashes}),
            "bottom_priority": ("/api/v2/torrents/bottomPrio", {"hashes": hashes}),
            "toggle_sequential": ("/api/v2/torrents/toggleSequentialDownload", {"hashes": hashes}),
            "toggle_first_last": ("/api/v2/torrents/toggleFirstLastPiecePrio", {"hashes": hashes}),
        }
        if action in simple:
            path, form = simple[action]
            try:
                return self.post(path, form)
            except RuntimeError as e:
                # Compatibility fallback for pre-5.0 qBittorrent.
                if action in ("stop", "start") and "404" in str(e):
                    old = "pause" if action == "stop" else "resume"
                    return self.post(f"/api/v2/torrents/{old}", {"hashes": hashes})
                raise
        if action == "delete":
            return self.post("/api/v2/torrents/delete", {"hashes": hashes, "deleteFiles": str(bool(payload.get("delete_files"))).lower()})
        if action == "force_start":
            return self.post("/api/v2/torrents/setForceStart", {"hashes": hashes, "value": str(bool(payload.get("value"))).lower()})
        if action == "set_location":
            return self.post("/api/v2/torrents/setLocation", {"hashes": hashes, "location": str(payload.get("location", ""))[:2048]})
        if action == "rename":
            return self.post("/api/v2/torrents/rename", {"hash": payload.get("hash", hashes), "name": str(payload.get("name", ""))[:512]})
        if action == "set_category":
            return self.post("/api/v2/torrents/setCategory", {"hashes": hashes, "category": str(payload.get("category", ""))[:256]})
        if action in ("add_tags", "remove_tags"):
            endpoint = "addTags" if action == "add_tags" else "removeTags"
            return self.post(f"/api/v2/torrents/{endpoint}", {"hashes": hashes, "tags": str(payload.get("tags", ""))[:1024]})
        if action in ("set_download_limit", "set_upload_limit"):
            endpoint = "setDownloadLimit" if action == "set_download_limit" else "setUploadLimit"
            return self.post(f"/api/v2/torrents/{endpoint}", {"hashes": hashes, "limit": max(0, int(payload.get("limit", 0)))})
        if action == "file_priority":
            ids = payload.get("ids", "")
            if isinstance(ids, list): ids = "|".join(map(str, ids))
            return self.post("/api/v2/torrents/filePrio", {"hash": payload.get("hash"), "id": ids, "priority": int(payload.get("priority", 1))})
        if action == "global_download_limit":
            return self.post("/api/v2/transfer/setDownloadLimit", {"limit": max(0, int(payload.get("limit", 0)))})
        if action == "global_upload_limit":
            return self.post("/api/v2/transfer/setUploadLimit", {"limit": max(0, int(payload.get("limit", 0)))})
        if action == "toggle_alt_speed":
            return self.post("/api/v2/transfer/toggleSpeedLimitsMode", {})
        if action == "create_category":
            return self.post("/api/v2/torrents/createCategory", {"category": str(payload.get("category", ""))[:256], "savePath": str(payload.get("save_path", ""))[:2048]})
        if action == "create_tags":
            return self.post("/api/v2/torrents/createTags", {"tags": str(payload.get("tags", ""))[:1024]})
        if action in ("add_magnet", "add_torrent"):
            stop_condition = str(payload.get("stop_condition") or "None")
            if stop_condition not in ("None", "MetadataReceived", "FilesChecked"):
                raise RuntimeError("Unsupported torrent stop condition")
            content_layout = str(payload.get("content_layout") or "Original")
            if content_layout not in ("Original", "Subfolder", "NoSubfolder"):
                raise RuntimeError("Unsupported torrent content layout")
            source = str(payload.get("source") if action == "add_torrent" else payload.get("urls", ""))[:16000]
            if not source.strip():
                raise RuntimeError("Torrent source is required")
            form = {
                "urls": source,
                "autoTMM": str(bool(payload.get("auto_tmm", False))).lower(),
                "savepath": str(payload.get("savepath", ""))[:2048],
                "useDownloadPath": str(bool(payload.get("use_download_path", False))).lower(),
                "downloadPath": str(payload.get("download_path", ""))[:2048],
                "rename": str(payload.get("rename", ""))[:512],
                "category": str(payload.get("category", ""))[:256],
                "tags": str(payload.get("tags", ""))[:1024],
                "stopped": str(bool(payload.get("stopped", False))).lower(),
                "stopCondition": stop_condition,
                "addToTopOfQueue": str(bool(payload.get("add_to_top", False))).lower(),
                "seedMode": str(bool(payload.get("seed_mode", False))).lower(),
                "sequentialDownload": str(bool(payload.get("sequential", False))).lower(),
                "firstLastPiecePrio": str(bool(payload.get("first_last", False))).lower(),
                "contentLayout": content_layout,
                "dlLimit": max(0, int(payload.get("dl_limit", 0) or 0)),
                "upLimit": max(0, int(payload.get("ul_limit", 0) or 0)),
            }
            priorities = payload.get("file_priorities")
            if priorities is not None:
                if not isinstance(priorities, list) or len(priorities) > 200000:
                    raise RuntimeError("Torrent file priorities must be an array")
                clean_priorities = []
                for value in priorities:
                    try:
                        priority = int(value)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("Torrent file priorities must be whole numbers") from exc
                    if priority not in (0, 1, 6, 7):
                        raise RuntimeError("Unsupported torrent file priority")
                    clean_priorities.append(str(priority))
                if clean_priorities:
                    form["filePriorities"] = ",".join(clean_priorities)
            return self.post("/api/v2/torrents/add", form)
        raise RuntimeError(f"Unsupported action: {action}")

    def upload_torrent(self, filename, content, fields):
        boundary = "----TorrentDashboard" + secrets.token_hex(12)
        chunks = []
        for k, v in fields.items():
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        safe_name = filename.replace('"', "")[:255]
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"{safe_name}\"\r\nContent-Type: application/x-bittorrent\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        raw = b"".join(chunks)
        return self._request("POST", "/api/v2/torrents/add", raw=raw, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


HISTORY = HistoryStore(DB_PATH)
CACHE_LOCK = threading.RLock()
CACHE = {}
CLIENTS = {}
LAST_COMPLETION_EVENT = set()


def disk_free_for(preferences):
    path = (preferences or {}).get("save_path") or ""
    try:
        p = Path(path)
        if p.exists():
            return shutil.disk_usage(p).free
    except Exception:
        pass
    return None


def get_server_config(cfg, server_id):
    for s in cfg.get("servers", []):
        if s.get("id") == server_id and s.get("enabled", True):
            return s
    raise RuntimeError(f"Unknown or disabled server: {server_id}")


def get_client(cfg, server_id):
    server = get_server_config(cfg, server_id)
    key = (server_id, server.get("base_url"), server.get("auth_method"), server.get("api_key"), server.get("username"), server.get("password"))
    with CACHE_LOCK:
        item = CLIENTS.get(server_id)
        if not item or item[0] != key:
            CLIENTS[server_id] = (key, QBitClient(server))
        return CLIENTS[server_id][1]


def send_notification(cfg, title, message):
    for integration in cfg.get("integrations", []):
        if not integration.get("enabled", True):
            continue
        provider = integration.get("type")
        try:
            if provider == "generic_webhook" and integration.get("webhook_url"):
                data = json.dumps({"title": title, "message": message}).encode("utf-8")
                req = urllib.request.Request(integration["webhook_url"], data=data, headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            elif provider == "discord" and integration.get("webhook_url"):
                data = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
                req = urllib.request.Request(integration["webhook_url"], data=data, headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            elif provider == "ntfy" and integration.get("topic_url"):
                headers = {"Title": title.encode("ascii", "ignore").decode() or "Torrent Dashboard"}
                if integration.get("access_token"):
                    headers["Authorization"] = f"Bearer {integration['access_token']}"
                req = urllib.request.Request(integration["topic_url"], data=message.encode("utf-8"), headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            elif provider == "home_assistant" and integration.get("webhook_url"):
                data = json.dumps({"title": title, "message": message}).encode("utf-8")
                req = urllib.request.Request(integration["webhook_url"], data=data, headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass

    # Keep manually configured legacy Gotify/Telegram delivery working until
    # those destinations are promoted into the modular Integrations catalog.
    n = cfg.get("notifications", {})
    if n.get("gotify_url") and n.get("gotify_token"):
        try:
            url = n["gotify_url"].rstrip("/") + "/message?token=" + urllib.parse.quote(n["gotify_token"])
            data = json.dumps({"title": title, "message": message, "priority": 5}).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST"), timeout=5).read()
        except Exception:
            pass
    if n.get("telegram_bot_token") and n.get("telegram_chat_id"):
        try:
            url = f"https://api.telegram.org/bot{n['telegram_bot_token']}/sendMessage"
            data = json.dumps({"chat_id": n["telegram_chat_id"], "text": f"{title}\n{message}"}).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST"), timeout=5).read()
        except Exception:
            pass


class NotificationDispatcher:
    """Deliver notifications on one background thread.

    Webhook calls can each block for up to 5 seconds. They used to run inline
    in the 1-second collector loop, so one slow endpoint froze live status for
    every server. Deliveries are now queued (bounded) and sent in order.
    """

    def __init__(self, sender, max_pending=100):
        self._sender = sender
        self._queue = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._thread = None

    def submit(self, cfg, title, message):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="notification-dispatcher", daemon=True)
                self._thread.start()
        try:
            self._queue.put_nowait((cfg, title, message))
            return True
        except queue.Full:
            logging.getLogger(__name__).warning("Notification queue is full; dropping %r", title)
            return False

    def _run(self):
        while True:
            cfg, title, message = self._queue.get()
            try:
                self._sender(cfg, title, message)
            except Exception:
                logging.getLogger(__name__).exception("Notification delivery failed")
            finally:
                self._queue.task_done()


NOTIFICATIONS = NotificationDispatcher(lambda cfg, title, message: send_notification(cfg, title, message))


def _bounded_int(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def collector_loop(stop_event):
    log = logging.getLogger(__name__)
    while not stop_event.is_set():
        try:
            _collect_once()
        except Exception:
            # Never let one unexpected error (for example a malformed config
            # value) permanently kill the only status/history thread.
            log.exception("Status collector pass failed")
        stop_event.wait(STATUS_REFRESH_SECONDS)


def _collect_once():
    """Poll every enabled client once and record cache, history, and notifications."""
    with STATE_GATE.activity():
        cfg = load_config()
        sample_every = _bounded_int(cfg["dashboard"].get("history_sample_seconds", 10), 10, 5, 3600)
        for server in cfg.get("servers", []):
            if not server.get("enabled", True): continue
            sid = server.get("id")
            try:
                client = get_client(cfg, sid)
                torrents, transfer, app_version, api_version = client.info()
                preferences = client.cached_preferences()
                meta = client.metadata()
                disk_free = disk_free_for(preferences)
                with CACHE_LOCK:
                    previous = CACHE.get(sid, {}).get("torrents", [])
                    prev_completed = {t.get("hash") for t in previous if float(t.get("progress",0) or 0) >= .999999}
                    now_completed = {t.get("hash") for t in torrents if float(t.get("progress",0) or 0) >= .999999}
                    newly = now_completed - prev_completed if previous else set()
                    CACHE[sid] = {
                        "ok": True, "ts": int(time.time()), "server": {"id":sid,"name":server.get("name",sid)},
                        "torrents": torrents, "transfer": transfer, "meta": meta,
                        "app_version": app_version, "api_version": api_version, "disk_free": disk_free
                    }
                HISTORY.sample(sid, torrents, transfer, disk_free, sample_every)
                for h in newly:
                    t = next((x for x in torrents if x.get("hash") == h), None)
                    if t:
                        NOTIFICATIONS.submit(cfg, "Torrent completed", f"{t.get('name','Torrent')} finished on {server.get('name',sid)}")
            except Exception as e:
                with CACHE_LOCK:
                    old = CACHE.get(sid, {})
                    CACHE[sid] = {**old, "ok": False, "ts": int(time.time()), "server": {"id":sid,"name":server.get("name",sid)}, "error": str(e)}
        try:
            # Retention cleanup used to run three DELETEs every second.
            HISTORY.maybe_cleanup(_bounded_int(cfg["dashboard"].get("history_retention_days", 30), 30, 1, 3650))
        except Exception:
            logging.getLogger(__name__).exception("History cleanup failed")


def integration_request(url, api_key=None, token=None, path="/api/v3/system/status"):
    if not url: return {"configured":False}
    full = url.rstrip("/") + path
    headers = {}
    if api_key: headers["X-Api-Key"] = api_key
    if token: headers["X-Plex-Token"] = token
    try:
        with urllib.request.urlopen(urllib.request.Request(full,headers=headers),timeout=5) as r:
            body = r.read(200000)
            ctype = r.headers.get("Content-Type","")
        data = None
        if "json" in ctype:
            data = json.loads(body.decode())
        return {"configured":True,"ok":True,"status":data or body.decode(errors="replace")[:200]}
    except Exception as e:
        return {"configured":True,"ok":False,"error":str(e)}


ARR_QUEUE_CACHE_SECONDS = 30
ARR_QUEUE_CACHE = {}
ARR_QUEUE_CACHE_LOCK = threading.Lock()


def _arr_queue_records(integration):
    """Fetch an *arr download queue, cached briefly per integration.

    The torrent detail panel refreshes every few seconds and each refresh used
    to re-download every configured Sonarr/Radarr/Lidarr queue (5 s timeout
    each, sequentially) before the detail response could be sent.
    """
    url=integration["url"].rstrip("/")+"/api/v3/queue?page=1&pageSize=200&includeUnknownSeriesItems=true&includeUnknownMovieItems=true"
    key=(url,hashlib.sha256(str(integration["api_key"]).encode()).hexdigest())
    now=time.monotonic()
    with ARR_QUEUE_CACHE_LOCK:
        hit=ARR_QUEUE_CACHE.get(key)
        if hit and now-hit[0] < ARR_QUEUE_CACHE_SECONDS:
            return hit[1]
    req=urllib.request.Request(url,headers={"X-Api-Key":integration["api_key"]})
    with urllib.request.urlopen(req,timeout=5) as resp:
        data=json.loads(resp.read(5_000_000).decode())
    records=data.get("records",[]) if isinstance(data,dict) else data if isinstance(data,list) else []
    with ARR_QUEUE_CACHE_LOCK:
        ARR_QUEUE_CACHE[key]=(now,records)
        for stale in [k for k,v in ARR_QUEUE_CACHE.items() if now-v[0] >= ARR_QUEUE_CACHE_SECONDS]:
            ARR_QUEUE_CACHE.pop(stale,None)
    return records


def torrent_integration_matches(cfg, hash_):
    out=[]
    for integration in cfg.get("integrations", []):
        name=integration.get("type")
        if name not in ("sonarr","radarr","lidarr") or not integration.get("enabled",True):
            continue
        if not integration.get("url") or not integration.get("api_key"):
            continue
        try:
            records=_arr_queue_records(integration)
            for rec in records:
                if str(rec.get("downloadId","")).lower()==hash_.lower():
                    out.append({"integration":integration.get("name") or INTEGRATION_TYPES[name]["label"],"title":rec.get("title") or rec.get("series",{}).get("title") or rec.get("movie",{}).get("title"),"status":rec.get("status"),"trackedDownloadStatus":rec.get("trackedDownloadStatus")})
        except Exception:
            pass
    return out

SOUND_MIME_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}


def store_custom_notification_sound(cfg, filename, content):
    filename = Path(str(filename or "sound")).name
    ext = Path(filename).suffix.lower()
    if ext not in SOUND_MIME_TYPES:
        raise RuntimeError("Custom sound must be a WAV, MP3, or OGG file")
    if not content or len(content) > MAX_CUSTOM_SOUND_BYTES:
        raise RuntimeError("Custom sound must be between 1 byte and 2 MB")
    if ext == ".wav" and not (content.startswith(b"RIFF") and content[8:12] == b"WAVE"):
        raise RuntimeError("The selected WAV file is not valid")
    if ext == ".ogg" and not content.startswith(b"OggS"):
        raise RuntimeError("The selected OGG file is not valid")
    if ext == ".mp3" and not (content.startswith(b"ID3") or (len(content) > 1 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0)):
        raise RuntimeError("The selected MP3 file is not valid")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for basename in (CUSTOM_SOUND_BASENAME, LEGACY_CUSTOM_SOUND_BASENAME):
        for old_ext in SOUND_MIME_TYPES:
            old = DATA_DIR / f"{basename}{old_ext}"
            if old.exists():
                try: old.unlink()
                except Exception: pass
    dest = DATA_DIR / f"{CUSTOM_SOUND_BASENAME}{ext}"
    dest.write_bytes(content)
    out = json.loads(json.dumps(cfg))
    n = out.setdefault("notifications", {})
    n["custom_sound_file"] = dest.name
    n["custom_sound_name"] = filename[:255]
    n["custom_sound_mime"] = SOUND_MIME_TYPES[ext]
    return out, {"name": n["custom_sound_name"], "mime": n["custom_sound_mime"]}


def configured_notification_sound(cfg):
    n = cfg.get("notifications", {})
    name = Path(str(n.get("custom_sound_file") or "")).name
    sound_name = Path(name)
    if sound_name.stem not in (CUSTOM_SOUND_BASENAME, LEGACY_CUSTOM_SOUND_BASENAME) or sound_name.suffix.lower() not in SOUND_MIME_TYPES:
        return None, None
    path = DATA_DIR / name
    if not path.exists() or not path.is_file():
        return None, None
    mime = str(n.get("custom_sound_mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    return path, mime


def github_headers(accept="application/vnd.github+json"):
    return {
        "User-Agent": f"TorrentDashboard/{VERSION}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def update_repository(cfg):
    updates = cfg.get("updates", {}) if isinstance(cfg.get("updates"), dict) else {}
    return normalize_github_repository(updates.get("repository") or DEFAULT_UPDATE_REPOSITORY)


def github_update_headers(cfg, accept="application/vnd.github+json"):
    update_repository(cfg)
    return github_headers(accept)


def save_update_source(cfg, repository):
    out = json.loads(json.dumps(cfg))
    repo = normalize_github_repository(repository)
    out["updates"] = {"repository": repo}
    out["integrations"] = [x for x in out.get("integrations", []) if x.get("type") != "github"]
    return out, repo


def _urlopen_json(url: str, timeout=10, headers=None):
    req=urllib.request.Request(url,headers=headers or {"User-Agent":f"TorrentDashboard/{VERSION}","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        if urllib.parse.urlparse(resp.geturl()).scheme != "https":
            raise RuntimeError("Update request redirected to a non-HTTPS URL")
        return json.loads(resp.read().decode("utf-8"))


def _urlopen_bytes(url: str, timeout=15, headers=None, max_bytes=8*1024*1024):
    req=urllib.request.Request(url,headers=headers or {"User-Agent":f"TorrentDashboard/{VERSION}"})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        if urllib.parse.urlparse(resp.geturl()).scheme != "https":
            raise RuntimeError("Update request redirected to a non-HTTPS URL")
        data=resp.read(max_bytes+1)
        if len(data)>max_bytes:
            raise RuntimeError("Update metadata exceeds the safety limit")
        return data




def is_newer_version(candidate: str, current: str = VERSION) -> bool:
    return _version_key(candidate) > _version_key(current)


def _github_releases(cfg, repo: str):
    url=f"https://api.github.com/repos/{repo}/releases?per_page=20"
    try:
        releases=_urlopen_json(url, timeout=10, headers=github_update_headers(cfg))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("GitHub denied the request or the unauthenticated API rate limit was reached. Try again later.") from exc
        if exc.code == 404:
            raise RuntimeError("Public GitHub repository or releases were not found. Verify the repository name and published releases.") from exc
        raise
    return [r for r in (releases or []) if not r.get("draft")]


def _latest_github_release(cfg, repo: str):
    releases=_github_releases(cfg,repo)
    if not releases:
        raise RuntimeError("No GitHub release was found for the configured repository")
    # Releases are returned newest first. During the 0.x prerelease phase we
    # intentionally include prereleases instead of using /releases/latest,
    # which excludes them.
    return releases[0]














def validate_update_repository(repository: str):
    repo = normalize_github_repository(repository)
    try:
        info = _urlopen_json(f"https://api.github.com/repos/{repo}", timeout=10, headers=github_headers())
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RuntimeError("GitHub denied the request or the unauthenticated API rate limit was reached. Try again later.") from exc
        if exc.code == 404:
            raise RuntimeError("Public GitHub repository not found. Verify owner/repository and make sure the repository is public.") from exc
        raise RuntimeError(f"GitHub repository check failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to GitHub: {exc.reason}") from exc
    if bool(info.get("private", False)):
        raise RuntimeError("Torrent Dashboard updates require a public GitHub repository")
    return str(info.get("full_name") or repo)








def fetch_update_release(cfg):
    repo = validate_update_repository(update_repository(cfg))
    releases=_github_releases(cfg,repo)
    if not releases:
        raise RuntimeError("No GitHub release was found for the configured repository")
    release=releases[0]
    tag=str(release.get("tag_name") or "").strip()
    version=tag.lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",version):
        raise RuntimeError("GitHub release tag must use semantic versioning, for example v0.4.0")
    distribution=runtime_distribution(APP_DIR)
    asset=_find_dashboard_asset(release,distribution)
    if not asset:
        raise RuntimeError("The latest GitHub release does not contain a Torrent Dashboard ZIP")
    api_url=str(asset.get("url") or "")
    if not api_url.startswith("https://api.github.com/"):
        raise RuntimeError("GitHub release asset URL is invalid")
    integrity_history=_github_release_integrity(releases,20,distribution)
    data={
        "version":version,
        "channel":"prerelease" if release.get("prerelease") else "stable",
        "publishedAt":str(release.get("published_at") or release.get("created_at") or ""),
        "releaseUrl":str(release.get("html_url") or ""),
        "notes":str(release.get("body") or ""),
        "title":str(release.get("name") or f"Torrent Dashboard v{version}"),
        "asset":{
            "name":str(asset.get("name") or f"Torrent-Dashboard-{version}.zip"),
            "githubApiUrl":api_url,
            "url":str(asset.get("browser_download_url") or ""),
            "sha256":_asset_sha256(asset),
            "size":int(asset.get("size") or 0),
        },
        "currentVersion":VERSION,
        "releaseHistory":integrity_history[:2],
    }
    try:
        write_release_integrity_cache(integrity_history)
    except Exception:
        pass
    data["updateAvailable"]=is_newer_version(version)
    if version == VERSION and not installed_release_info():
        try:
            write_release_info(RELEASE_INFO_PATH,_release_info_payload(
                version,data["asset"]["name"],data["asset"]["sha256"],repo,data.get("releaseUrl",""),
                data.get("publishedAt",""),data.get("channel",""),str(release.get("target_commitish") or ""),
            ))
        except Exception:
            pass
    return data

# Compatibility alias for older front-end/update-state code. The update
# metadata is now synthesized directly from GitHub Release metadata; there is
# no external update-manifest.json asset.
def fetch_update_manifest(cfg):
    return fetch_update_release(cfg)





def update_state():
    if not UPDATE_STATE_PATH.exists(): return {"state":"idle","currentVersion":VERSION}
    try:
        d=json.loads(UPDATE_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(d,dict):
            d.setdefault("currentVersion",VERSION)
            return d
    except Exception: pass
    return {"state":"idle","currentVersion":VERSION}


def write_update_state(data):
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    payload={**data,"currentVersion":VERSION,"ts":int(time.time())}
    tmp=UPDATE_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
    tmp.replace(UPDATE_STATE_PATH)
    return payload




def safe_extract_zip(zip_path: Path, dest: Path):
    if dest.exists(): shutil.rmtree(dest)
    dest.mkdir(parents=True,exist_ok=True)
    root=dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            member=(dest/info.filename).resolve()
            if root != member and root not in member.parents:
                raise RuntimeError("Update ZIP contains an unsafe path")
        z.extractall(dest)
    entries=[p for p in dest.iterdir() if p.name not in ("__MACOSX",)]
    source=entries[0] if len(entries)==1 and entries[0].is_dir() else dest
    distribution=runtime_distribution(APP_DIR)
    if distribution == "windows-x64":
        try:
            package_info=json.loads((source/"package-info.json").read_text(encoding="utf-8"))
        except Exception:
            package_info={}
        valid=(
            package_info.get("distribution") == "windows-x64"
            and (source/"Dashboard.exe").is_file()
            and (source/"Recovery.exe").is_file()
            and (source/"Updater.exe").is_file()
            and (source/"static"/"index.html").is_file()
        )
    else:
        valid=(
            ((source/"dashboard.py").is_file() or (source/"src"/"torrent_dashboard"/"dashboard.py").is_file())
            and (source/"static"/"index.html").is_file()
        )
    if not valid:
        raise RuntimeError("Update ZIP does not contain a valid Torrent Dashboard application")
    return source


def stage_update(cfg):
    manifest=fetch_update_manifest(cfg)
    if not manifest["updateAvailable"]:
        return {"state":"upToDate","manifest":manifest,"currentVersion":VERSION}
    version=manifest["version"]
    stage=UPDATE_DIR/version
    stage.mkdir(parents=True,exist_ok=True)
    package=stage/manifest["asset"]["name"]
    package_url=manifest["asset"].get("githubApiUrl") or manifest["asset"]["url"]
    headers=github_update_headers(cfg,"application/octet-stream") if manifest["asset"].get("githubApiUrl") else {"User-Agent":f"TorrentDashboard/{VERSION}"}
    req=urllib.request.Request(package_url,headers=headers)
    write_update_state({"state":"downloading","version":version,"manifest":manifest})
    h=hashlib.sha256(); total=0
    try:
        expected=int(manifest["asset"].get("size") or 0)
        max_bytes=512*1024*1024
        if expected>max_bytes: raise RuntimeError("Update package exceeds the 512 MB safety limit")
        with urllib.request.urlopen(req,timeout=30) as resp, package.open("wb") as out:
            if urllib.parse.urlparse(resp.geturl()).scheme != "https":
                raise RuntimeError("Update package redirected to a non-HTTPS URL")
            while True:
                chunk=resp.read(1024*1024)
                if not chunk: break
                total+=len(chunk)
                if total>max_bytes or (expected and total>expected):
                    raise RuntimeError("Downloaded update exceeds the GitHub release size or safety limit")
                out.write(chunk); h.update(chunk)
        got=h.hexdigest()
        if got != manifest["asset"]["sha256"]:
            package.unlink(missing_ok=True)
            raise RuntimeError("Downloaded update failed SHA-256 verification")
        if expected and total != expected:
            package.unlink(missing_ok=True)
            raise RuntimeError("Downloaded update size does not match the GitHub release asset")
        source=safe_extract_zip(package,stage/"extracted")
        write_release_info(source/"release-info.json",_release_info_payload(
            version,manifest["asset"]["name"],got,update_repository(cfg),manifest.get("releaseUrl",""),
            manifest.get("publishedAt",""),manifest.get("channel",""),
        ))
        payload={"state":"readyToInstall","version":version,"package":str(package),"source":str(source),"manifest":manifest,"sha256":got,"bytes":total}
        write_update_state(payload)
        return payload
    except Exception as exc:
        write_update_state({"state":"failed","version":version,"error":str(exc),"manifest":manifest})
        raise


def launch_update_installer(handler, cfg, requested_version=None):
    state=update_state()
    if state.get("state") != "readyToInstall":
        raise RuntimeError("No verified update is ready to install")
    if not is_newer_version(str(state.get("version") or "")):
        raise RuntimeError("The staged update is not newer than the running version")
    if requested_version and str(requested_version) != str(state.get("version")):
        raise RuntimeError("The staged update version changed; check for updates again")
    source=Path(state.get("source","")).resolve()
    if not source.exists(): raise RuntimeError("The staged update files are missing")
    cmd=updater_command(APP_DIR,detached=is_frozen())
    cmd += ["--pid",str(os.getpid()),"--source",str(source),"--target",str(APP_DIR),"--version",str(state.get("version"))]
    kwargs={"cwd":str(APP_DIR),"stdin":subprocess.DEVNULL,"stdout":subprocess.DEVNULL,"stderr":subprocess.DEVNULL,"env":source_python_env(APP_DIR)}
    if os.name=="nt":
        kwargs["creationflags"]=getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)|getattr(subprocess,"DETACHED_PROCESS",0)
    else: kwargs["start_new_session"]=True
    subprocess.Popen(cmd,**kwargs)
    write_update_state({**state,"state":"installing"})
    threading.Timer(.5,handler.server.shutdown).start()
    return {"ok":True,"state":"installing","version":state.get("version")}


def frontend_recovery_script(version):
    build = json.dumps(str(version))
    script = '''(() => {
  const build = __BUILD__;
  if (window.__tdFrontendRecoveryStarted) return;
  window.__tdFrontendRecoveryStarted = true;
  console.error('[Torrent Dashboard] Frontend build mismatch detected. Clearing cached application shell.', { build });
  (async () => {
    try {
      if ('serviceWorker' in navigator) {
        const registrations = await navigator.serviceWorker.getRegistrations();
        await Promise.all(registrations.map(registration => registration.unregister()));
      }
    } catch (error) { console.error('[Torrent Dashboard] Service worker cleanup failed', error); }
    try {
      if ('caches' in window) {
        const keys = await caches.keys();
        await Promise.all(keys.filter(key => key.startsWith('torrent-dashboard-')).map(key => caches.delete(key)));
      }
    } catch (error) { console.error('[Torrent Dashboard] Cache cleanup failed', error); }
    const url = new URL(window.location.href);
    url.searchParams.set('td-recover', build);
    window.location.replace(url.toString());
  })();
})();'''
    return script.replace("__BUILD__", build).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "TorrentDashboard/0"
    timeout = 30

    def log_message(self, fmt, *args):
        # log_request passes (requestline, code, size); log_error can pass a
        # single argument (for example "Request timed out: %r"), which used to
        # raise IndexError here. Log server errors and all log_error calls.
        if len(args) >= 2 and str(args[1]).startswith("5"):
            super().log_message(fmt,*args)
        elif len(args) < 2:
            super().log_message(fmt,*args)

    def client_ip(self):
        return self.client_address[0]

    def cookie_value(self, name):
        raw=self.headers.get("Cookie","")
        c=cookies.SimpleCookie()
        try: c.load(raw)
        except Exception: return None
        return c.get(name).value if c.get(name) else None

    def cookie_token(self):
        return self.cookie_value("td_session")

    def auth(self, create_bypass=True):
        cfg=load_config(); a=cfg["auth"]; mode=a.get("mode","lan_bypass")
        token=self.cookie_token(); sess=SESSIONS.get(token)
        if sess and sess.get("auth_kind") in ("lan_bypass", "disabled"):
            allowed = (
                sess["auth_kind"] == "disabled" and mode == "disabled"
                or sess["auth_kind"] == "lan_bypass" and mode == "lan_bypass"
                and is_trusted_ip(self.client_ip(), effective_trusted_cidrs(a))
            )
            if not allowed:
                SESSIONS.remove(token)
                sess = None
        if sess: return cfg,token,sess,None
        bypass = mode=="disabled" or (mode=="lan_bypass" and is_trusted_ip(self.client_ip(),effective_trusted_cidrs(a)))
        if bypass and create_bypass:
            token,sess=SESSIONS.create("LAN" if mode!="disabled" else "Guest",a.get("session_hours",24),"lan_bypass" if mode!="disabled" else "disabled",group="administrator",display_name="Trusted Network" if mode!="disabled" else "Guest")
            return cfg,token,sess,token
        return cfg,None,None,None

    def csrf_ok(self,sess):
        return bool(sess and self.headers.get("X-CSRF-Token") and hmac.compare_digest(self.headers.get("X-CSRF-Token"),sess.get("csrf","")))

    def send_bytes(self, code, body, content_type, cookie_token=None, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; manifest-src 'self'; worker-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if cookie_token:
            secure = "; Secure" if load_config()["dashboard"].get("https_enabled") else ""
            self.send_header("Set-Cookie", f"td_session={cookie_token}; Path=/; HttpOnly; SameSite=Lax{secure}")
        if extra:
            for k,v in extra.items(): self.send_header(k,str(v))
        self.end_headers(); self.wfile.write(body)

    def send_json(self, code, obj, cookie_token=None, extra=None):
        self.send_bytes(code,json.dumps(obj,separators=(",",":"),default=str).encode(),"application/json; charset=utf-8",cookie_token,extra)

    def send_file(self, code, path, content_type="application/octet-stream", filename=None, cookie_token=None):
        path = Path(path)
        safe_name = Path(str(filename or path.name)).name.replace('"', '')
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if cookie_token:
            secure = "; Secure" if load_config()["dashboard"].get("https_enabled") else ""
            self.send_header("Set-Cookie", f"td_session={cookie_token}; Path=/; HttpOnly; SameSite=Lax{secure}")
        self.end_headers()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def require_auth(self, mutation=False):
        cfg,token,sess,new_cookie=self.auth()
        if not sess:
            self.send_json(401,{"error":"Authentication required","auth_mode":cfg["auth"].get("mode")})
            return None
        if mutation and not self.csrf_ok(sess):
            self.send_json(403,{"error":"CSRF token missing or invalid"},new_cookie)
            return None
        return cfg,token,sess,new_cookie



    def recovery_login_route(self):
        ip=self.client_ip(); now=time.time(); limit=10
        with RECOVERY_LOCK:
            if not _record_attempt(RECOVERY_ATTEMPTS,ip,now,600,limit):
                return self.send_json(429,{"error":"Too many recovery attempts"})
        try:
            data=parse_json_body(self,12000)
        except Exception as exc:
            return self.send_json(400,{"error":str(exc)})
        cfg=load_config(); supplied=str(data.get("recovery_key") or "").strip(); encoded=str(cfg.get("recovery",{}).get("key_hash") or "")
        if not encoded:
            return self.send_json(503,{"error":"Dashboard recovery has not been initialized yet"})
        if not supplied or not verify_dashboard_recovery_key(supplied,encoded):
            HISTORY.event("dashboard","recovery_login_failed",RECOVERY_ACCOUNT_USERNAME,"",{"client_ip":ip})
            return self.send_json(401,{"error":"Invalid recovery key"})
        with RECOVERY_LOCK:
            RECOVERY_ATTEMPTS.pop(ip,None)
        token,sess=SESSIONS.create(RECOVERY_ACCOUNT_USERNAME,cfg["auth"].get("session_hours",24),"recovery_key",group="administrator",user_id=RECOVERY_ACCOUNT_ID,display_name=RECOVERY_ACCOUNT_USERNAME)
        HISTORY.event("dashboard","recovery_login_success",RECOVERY_ACCOUNT_USERNAME,"",{"client_ip":ip})
        return self.send_json(200,{"ok":True,"csrf":sess["csrf"],"group":"administrator","recovery":True},token)
    def do_GET(self):
        with STATE_GATE.activity():
            try:
                return self._do_GET()
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception as exc:
                # Previously an unexpected error (for example
                # /api/history?minutes=abc) dropped the connection without
                # any response. Answer with JSON instead.
                status = 400 if isinstance(exc, (ValueError, RuntimeError)) else 500
                if status == 500:
                    logging.getLogger(__name__).exception("GET %s failed", self.path.partition("?")[0])
                try:
                    return self.send_json(status,{"error":str(exc) if status == 400 else "Internal server error"})
                except Exception:
                    return None

    def _do_GET(self):
        path,_,query=self.path.partition("?"); qs=urllib.parse.parse_qs(query)
        if path=="/health":
            health={"ok":True,"version":VERSION}
            # Process ID and install paths are only useful to local tooling;
            # do not disclose them to other machines on the network.
            if is_loopback_ip(self.client_ip()):
                health.update({"pid":os.getpid(),"application":str(APP_DIR),"python":sys.executable})
            return self.send_json(200,health)
        if path=="/manifest.webmanifest": return self.serve_static("manifest.webmanifest")
        if path=="/sw.js": return self.serve_static("sw.js","application/javascript; charset=utf-8")
        if path.startswith("/static/"):
            name=path[len("/static/"):]
            requested=(qs.get("v") or [""])[0]
            if name in ("app.js","settings.js") and requested and requested != VERSION:
                return self.send_bytes(200,frontend_recovery_script(VERSION),"application/javascript; charset=utf-8")
            return self.serve_static(name)
        if path=="/" or path=="/index.html": return self.serve_static("index.html")

        if path=="/api/setup/status":
            cfg=load_config(); required=not bool(cfg.get("setup",{}).get("complete"))
            if not required:
                # These endpoints are unauthenticated. After setup the browser
                # only needs to know setup is done; do not keep publishing the
                # host's interfaces and trusted networks to anyone who asks.
                return self.send_json(200,{"required":False,"code_required":False})
            interfaces=detect_network_interfaces()
            return self.send_json(200,{
                "required": required,
                "code_required": bool(required and not is_loopback_ip(self.client_ip())),
                "client_ip": self.client_ip(),
                "lan_ip": local_lan_ip(),
                "port": cfg["dashboard"].get("port",8765),
                "trusted_interfaces": cfg["auth"].get("trusted_interfaces",[]),
                "trusted_ips": cfg["auth"].get("trusted_ips",[]),
                "effective_trusted_cidrs": effective_trusted_cidrs(cfg["auth"]),
                "network_interfaces": interfaces,
                "detected_lan": detect_lan_network(),
        "local_ip": local_lan_ip(),
            })
        if path=="/api/setup/network-interfaces":
            if load_config().get("setup",{}).get("complete"):
                return self.send_json(403,{"error":"Setup has already been completed"})
            return self.send_json(200,{"interfaces":detect_network_interfaces(qs.get("refresh",["0"])[0]=="1")})

        if path=="/api/me":
            cfg,token,sess,new_cookie=self.auth()
            if not sess:
                return self.send_json(401,{"authenticated":False,"auth_mode":cfg["auth"].get("mode")})
            safe={"authenticated":True,"username":sess["username"],"display_name":sess.get("display_name") or sess["username"],"user_id":sess.get("user_id","") ,"group":sess.get("group","standard"),"group_label":USER_GROUPS.get(sess.get("group"),"Standard User"),"can_manage":session_is_admin(sess),"is_recovery_account":sess.get("user_id","")==RECOVERY_ACCOUNT_ID,"auth_kind":sess["auth_kind"],"csrf":sess["csrf"],"auth_mode":cfg["auth"].get("mode"),"title":cfg["dashboard"].get("title"),"version":VERSION,"lan_ip":local_lan_ip(),"port":cfg["dashboard"].get("port",8765),"scheme":"https" if cfg["dashboard"].get("https_enabled") else "http"}
            return self.send_json(200,safe,new_cookie)

        ctx=self.require_auth(False)
        if not ctx: return
        cfg,token,sess,new_cookie=ctx
        if sess.get("user_id")==RECOVERY_ACCOUNT_ID and path.startswith("/api/account"):
            return self.send_json(403,{"error":"The built-in Administrator recovery account cannot be edited"},new_cookie)
        if path=="/api/account":
            user=user_by_id(cfg,sess.get("user_id",""))
            if not user:
                return self.send_json(404,{"error":"This session is not linked to a user account"},new_cookie)
            return self.send_json(200,{"user":public_user(user)},new_cookie)
        if path=="/api/account/avatar":
            user=user_by_id(cfg,sess.get("user_id",""))
            if not user:
                return self.send_json(404,{"error":"This session is not linked to a user account"},new_cookie)
            avatar_path, avatar_mime = configured_user_avatar(user)
            if not avatar_path:
                return self.send_json(404,{"error":"No profile picture is configured"},new_cookie)
            return self.send_bytes(200,avatar_path.read_bytes(),avatar_mime,new_cookie)
        if path in ("/api/settings","/api/integrations","/api/integration-health","/api/integrations/jellyfin/status","/api/integrations/jellyfin/tasks","/api/users","/api/network/interfaces","/api/client-settings","/api/torrent-metadata/save","/api/backups","/api/backups/export") and not session_is_admin(sess):
            return self.send_json(403,{"error":"Administrator access is required"},new_cookie)

        if path=="/api/torrent-metadata/save":
            sid=qs.get("server",["local"])[0]; source=qs.get("source",[""])[0]; torrent_id=qs.get("hash",[""])[0]
            try:
                _,body=get_client(cfg,sid).save_torrent_metadata(source,torrent_id)
                return self.send_bytes(200,body,"application/x-bittorrent",new_cookie,{"Content-Disposition":'attachment; filename="torrent.torrent"'})
            except Exception as e:
                return self.send_json(502,{"error":str(e)},new_cookie)

        if path=="/api/status":
            sid=qs.get("server",["all"])[0]
            with CACHE_LOCK:
                if sid=="all":
                    items=[dict(v) for k,v in CACHE.items()]
                    torrents=[]; transfer={"dl_info_speed":0,"up_info_speed":0,"dl_info_data":0,"up_info_data":0}; errors=[]
                    for item in items:
                        if not item.get("ok"):
                            errors.append({"server":item.get("server"),"error":item.get("error")}); continue
                        for t in item.get("torrents",[]): torrents.append({**t,"_server_id":item["server"]["id"],"_server_name":item["server"]["name"]})
                        for key in transfer: transfer[key]+=int(item.get("transfer",{}).get(key,0) or 0)
                    payload={"ok":len(errors)<len(items) if items else False,"server":{"id":"all","name":"All servers"},"torrents":torrents,"transfer":transfer,"errors":errors,"servers":[x.get("server") for x in items],"ts":int(time.time())}
                else:
                    payload=dict(CACHE.get(sid) or {"ok":False,"error":"No data yet; collector is connecting","server":{"id":sid,"name":sid}})
            payload["tab_counts"]={
                "all":len(payload.get("torrents",[])),
                "downloading":sum(1 for t in payload.get("torrents",[]) if float(t.get("progress",0) or 0)<1 and "paused" not in str(t.get("state","")).lower() and "stopped" not in str(t.get("state","")).lower()),
                "completed":sum(1 for t in payload.get("torrents",[]) if float(t.get("progress",0) or 0)>=.999999),
                "paused":sum(1 for t in payload.get("torrents",[]) if float(t.get("progress",0) or 0)<.999999 and ("paused" in str(t.get("state","")).lower() or "stopped" in str(t.get("state","")).lower())),
            }
            return self.send_json(200,payload,new_cookie)

        if path=="/api/servers":
            servers=[{"id":s.get("id"),"name":s.get("name",s.get("id")),"enabled":s.get("enabled",True)} for s in cfg.get("servers",[])]
            return self.send_json(200,{"servers":servers},new_cookie)

        if path=="/api/detail":
            sid=qs.get("server",["local"])[0]; h=qs.get("hash",[""])[0]
            try:
                d=get_client(cfg,sid).detail(h); d["integrations"]=torrent_integration_matches(cfg,h)
                return self.send_json(200,d,new_cookie)
            except Exception as e: return self.send_json(502,{"error":str(e)},new_cookie)

        if path=="/api/meta":
            sid=qs.get("server",["local"])[0]
            try: return self.send_json(200,get_client(cfg,sid).metadata(),new_cookie)
            except Exception as e: return self.send_json(502,{"error":str(e)},new_cookie)
        if path=="/api/client-settings":
            sid=qs.get("server",["local"])[0]
            try: return self.send_json(200,{"settings":get_client(cfg,sid).client_settings()},new_cookie)
            except Exception as e: return self.send_json(502,{"error":str(e)},new_cookie)

        if path=="/api/history":
            return self.send_json(200,{"points":HISTORY.history(qs.get("server",["all"])[0],qs.get("minutes",["60"])[0])},new_cookie)
        if path=="/api/events": return self.send_json(200,{"events":HISTORY.events(qs.get("limit",["100"])[0])},new_cookie)
        if path=="/api/analytics": return self.send_json(200,HISTORY.analytics(qs.get("server",["all"])[0]),new_cookie)
        if path=="/api/integration-health": return self.send_json(200,{"integrations":integration_health_statuses(cfg)},new_cookie)
        if path=="/api/integrations": return self.send_json(200,{"types":integration_catalog(),"integrations":redacted_integrations(cfg)},new_cookie)
        if path=="/api/integrations/jellyfin/status":
            try:
                item=find_jellyfin_integration(cfg,qs.get("id",[""])[0])
                return self.send_json(200,jellyfin_overview(item),new_cookie)
            except Exception as e:
                return self.send_json(502,{"error":str(e)},new_cookie)
        if path=="/api/integrations/jellyfin/tasks":
            try:
                item=find_jellyfin_integration(cfg,qs.get("id",[""])[0])
                return self.send_json(200,{"tasks":jellyfin_scheduled_tasks(item)},new_cookie)
            except Exception as e:
                return self.send_json(502,{"error":str(e)},new_cookie)
        if path=="/api/users": return self.send_json(200,{"users":[public_user(u) for u in cfg.get("users",[])],"current_user_id":sess.get("user_id","")},new_cookie)
        if path=="/api/backups":
            return self.send_json(200,{"backups":list_backups(APP_DIR),"extension":BACKUP_EXTENSION,"max_bytes":MAX_BACKUP_BYTES},new_cookie)
        if path=="/api/backups/export":
            try:
                item=backup_path(APP_DIR,qs.get("name",[""])[0])
                return self.send_file(200,item,"application/octet-stream",item.name,new_cookie)
            except Exception as exc:
                return self.send_json(404,{"error":str(exc)},new_cookie)
        if path=="/api/settings": return self.send_json(200,redacted_config(cfg),new_cookie)
        if path=="/api/network/interfaces": return self.send_json(200,{"interfaces":detect_network_interfaces(qs.get("refresh",["0"])[0]=="1")},new_cookie)
        if path=="/api/notification-sound":
            sound_path, sound_mime = configured_notification_sound(cfg)
            if not sound_path:
                return self.send_json(404,{"error":"No custom notification sound is configured"},new_cookie)
            return self.send_bytes(200,sound_path.read_bytes(),sound_mime,new_cookie)
        if path=="/api/update-check": return self.update_check(cfg,new_cookie)
        if path=="/api/update-status": return self.send_json(200,update_state(),new_cookie)
        return self.send_json(404,{"error":"Not found"},new_cookie)

    def do_POST(self):
        path = self.path.partition("?")[0]
        if path == "/api/backups/restore":
            # Reject unauthorized callers before allowing them to queue maintenance.
            # _do_POST reauthenticates after entry in case a prior restore revoked it.
            ctx = self.require_auth(True)
            if not ctx:
                return
            if not session_is_admin(ctx[2]):
                return self.send_json(403, {"error": "Administrator access is required"}, ctx[3])
        gate = STATE_GATE.maintenance if path == "/api/backups/restore" else STATE_GATE.activity
        with gate():
            return self._do_POST()

    def _do_POST(self):
        path=self.path.partition("?")[0]
        if path=="/api/setup/test-client": return self.setup_test_client()
        if path=="/api/setup/complete": return self.setup_complete()
        if path=="/api/register": return self.register_route()
        if path=="/api/login": return self.login_route()
        if path=="/api/recovery/login": return self.recovery_login_route()
        if path=="/api/logout":
            token=self.cookie_token(); SESSIONS.remove(token)
            return self.send_json(200,{"ok":True},None)
        ctx=self.require_auth(True)
        if not ctx: return
        cfg,token,sess,new_cookie=ctx
        try:
            if sess.get("user_id")==RECOVERY_ACCOUNT_ID and path.startswith("/api/account"):
                return self.send_json(403,{"error":"The built-in Administrator recovery account cannot be edited"},new_cookie)
            if path=="/api/account":
                data=parse_json_body(self,20000)
                updated,user=mutate_config(lambda current: save_current_user_profile(current,sess.get("user_id",""),data))
                SESSIONS.update_user(user)
                HISTORY.event("dashboard","account_profile_changed",user.get("username",""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"user":public_user(user)},new_cookie)
            if path=="/api/account/password":
                data=parse_json_body(self,20000)
                updated,user=mutate_config(lambda current: change_current_user_password(current,sess.get("user_id",""),data.get("current_password"),data.get("new_password")))
                SESSIONS.remove_user_except(user.get("id",""),token)
                HISTORY.event("dashboard","account_password_changed",user.get("username",""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True},new_cookie)
            if path=="/api/account/avatar":
                fields,files=parse_multipart(self,max_bytes=MAX_AVATAR_BYTES+256000)
                if not files:
                    raise RuntimeError("Choose a profile picture")
                _,filename,content=files[0]
                updated,user=mutate_config(lambda current: store_user_avatar(current,sess.get("user_id",""),filename,content))
                HISTORY.event("dashboard","account_avatar_changed",user.get("username",""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"user":public_user(user)},new_cookie)
            if path=="/api/account/avatar/delete":
                updated,user=mutate_config(lambda current: remove_user_avatar(current,sess.get("user_id","")))
                HISTORY.event("dashboard","account_avatar_removed",user.get("username",""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"user":public_user(user)},new_cookie)
            if not session_is_admin(sess):
                return self.send_json(403,{"error":"Administrator access is required"},new_cookie)
            if path=="/api/torrent-metadata/fetch":
                data=parse_json_body(self,50000); sid=str(data.get("server") or "local")
                result=get_client(cfg,sid).fetch_torrent_metadata(data.get("source"),data.get("downloader"))
                return self.send_json(200,result,new_cookie)
            if path=="/api/torrent-metadata/parse":
                fields,files=parse_multipart(self)
                sid=str(fields.get("server") or "local")
                if not files:
                    raise RuntimeError("No .torrent file supplied")
                _,filename,content=files[0]
                result=get_client(cfg,sid).parse_torrent_metadata(filename,content)
                return self.send_json(200,result,new_cookie)
            if path=="/api/client-settings":
                data=parse_json_body(self,50000); sid=str(data.pop("server","local")); settings=get_client(cfg,sid).update_client_settings(data)
                HISTORY.event(sid,"client_settings_changed",sess.get("username",""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"settings":settings},new_cookie)
            if path=="/api/action":
                data=parse_json_body(self); sid=data.pop("server","local"); action=data.pop("action"); result=get_client(cfg,sid).action(action,data)
                HISTORY.event(sid, "action:"+action, sess.get("username",""), data.get("hash") or "", {"client_ip": self.client_ip()})
                return self.send_json(200,{"ok":True,"status":result[0] if isinstance(result,tuple) else 200},new_cookie)
            if path=="/api/upload":
                fields,files=parse_multipart(self); sid=fields.pop("server","local")
                if not files: raise RuntimeError("No .torrent file supplied")
                _,filename,content=files[0]
                get_client(cfg,sid).upload_torrent(filename,content,fields)
                HISTORY.event(sid, "torrent_upload", filename, "", {"client_ip": self.client_ip()})
                return self.send_json(200,{"ok":True},new_cookie)
            if path=="/api/client-test":
                data=parse_json_body(self); sid=str(data.get("id") or "")
                existing=next((x for x in cfg.get("servers",[]) if x.get("id")==sid),{})
                server=normalize_qbittorrent_server(data,existing)
                return self.send_json(200,test_server_connection(server),new_cookie)
            if path=="/api/integration-test":
                data=parse_json_body(self,20000); iid=str(data.get("id") or "")
                existing=next((x for x in cfg.get("integrations",[]) if str(x.get("id") or "")==iid),{})
                item=normalize_integration(data,existing)
                return self.send_json(200,test_integration_connection(item),new_cookie)
            if path=="/api/integrations":
                data=parse_json_body(self,20000); updated,item=mutate_config(lambda current: save_integration(current,data))
                HISTORY.event("dashboard","integration_saved",item.get("name",item.get("type","")),"",{"client_ip":self.client_ip(),"type":item.get("type")})
                return self.send_json(200,{"ok":True,"integration":redacted_integrations({"integrations":[item]})[0]},new_cookie)
            if path=="/api/integrations/delete":
                data=parse_json_body(self,10000); iid=str(data.get("id") or ""); updated,_=mutate_config(lambda current: (delete_integration(current,iid),None))
                HISTORY.event("dashboard","integration_deleted",iid,"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True},new_cookie)
            if path=="/api/integrations/jellyfin/favorites":
                data=parse_json_body(self,20000); iid=str(data.get("id") or "")
                updated,item=mutate_config(lambda current: save_jellyfin_task_favorites(current,iid,data.get("task_ids",[])))
                favorites=item.get("favorite_task_ids",[])
                HISTORY.event("dashboard","jellyfin_task_favorites_changed",item.get("name","Jellyfin"),"",{"client_ip":self.client_ip(),"integration_id":iid,"count":len(favorites)})
                return self.send_json(200,{"ok":True,"favorite_task_ids":favorites},new_cookie)
            if path=="/api/integrations/jellyfin/refresh":
                data=parse_json_body(self,10000); item=find_jellyfin_integration(cfg,data.get("id"))
                try:
                    result=refresh_jellyfin_libraries(item)
                except Exception as exc:
                    HISTORY.event("dashboard","jellyfin_library_refresh_failed",item.get("name","Jellyfin"),"",{"client_ip":self.client_ip(),"integration_id":item.get("id","")})
                    return self.send_json(502,{"error":str(exc)},new_cookie)
                HISTORY.event("dashboard","jellyfin_library_refresh_requested",item.get("name","Jellyfin"),"",{"client_ip":self.client_ip(),"integration_id":item.get("id","")})
                return self.send_json(200,result,new_cookie)
            if path=="/api/integrations/jellyfin/task":
                data=parse_json_body(self,10000); item=find_jellyfin_integration(cfg,data.get("id")); task_id=str(data.get("task_id") or "").strip(); action=str(data.get("action") or "start").strip().lower()
                if not task_id:
                    raise RuntimeError("Jellyfin scheduled task ID is required")
                if action=="start":
                    result=start_jellyfin_scheduled_task(item,task_id)
                elif action=="stop":
                    result=stop_jellyfin_scheduled_task(item,task_id)
                else:
                    raise RuntimeError("Jellyfin scheduled task action must be start or stop")
                HISTORY.event("dashboard",f"jellyfin_scheduled_task_{action}",task_id,"",{"client_ip":self.client_ip(),"integration_id":item.get("id","")})
                return self.send_json(200,result,new_cookie)
            if path=="/api/users/approve":
                data=parse_json_body(self,10000); uid=str(data.get("id") or "")
                updated,user=mutate_config(lambda current: approve_user(current,uid))
                HISTORY.event("dashboard","user_registration_approved",user.get("username",""),"",{"client_ip":self.client_ip(),"user_id":uid,"approved_by":sess.get("username","")})
                return self.send_json(200,{"ok":True,"user":public_user(user)},new_cookie)
            if path=="/api/users/reject":
                data=parse_json_body(self,10000); uid=str(data.get("id") or "")
                updated,user=mutate_config(lambda current: reject_user(current,uid)); delete_user_avatar_files(uid); SESSIONS.remove_user(uid)
                HISTORY.event("dashboard","user_registration_rejected",user.get("username",""),"",{"client_ip":self.client_ip(),"user_id":uid,"rejected_by":sess.get("username","")})
                return self.send_json(200,{"ok":True},new_cookie)
            if path=="/api/users":
                data=parse_json_body(self,20000); updated,user=mutate_config(lambda current: save_user(current,data)); SESSIONS.update_user(user)
                if data.get("password"):
                    SESSIONS.remove_user_except(user.get("id", ""), token)
                HISTORY.event("dashboard","user_saved",user.get("username",""),"",{"client_ip":self.client_ip(),"group":user.get("group")})
                return self.send_json(200,{"ok":True,"user":public_user(user)},new_cookie)
            if path=="/api/users/delete":
                data=parse_json_body(self,10000); uid=str(data.get("id") or ""); updated,_=mutate_config(lambda current: (delete_user(current,uid,sess.get("user_id","")),None)); delete_user_avatar_files(uid); SESSIONS.remove_user(uid)
                HISTORY.event("dashboard","user_deleted",uid,"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True},new_cookie)
            if path=="/api/settings":
                data=parse_json_body(self); updated,_=mutate_config(lambda current: (apply_settings_update(current,data),None))
                HISTORY.event("dashboard", "settings_changed", sess.get("username",""), "", {"client_ip": self.client_ip()})
                return self.send_json(200,{"ok":True,"settings":redacted_config(updated)},new_cookie)
            if path=="/api/backups/create":
                with CONFIG_STORE.exclusive() as current:
                    item=create_backup(APP_DIR,VERSION,current,history_lock=HISTORY.lock)
                HISTORY.event("dashboard","backup_created",item.get("name", ""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"backup":item},new_cookie)
            if path=="/api/backups/delete":
                data=parse_json_body(self,12000)
                name=delete_backup(APP_DIR,data.get("name"))
                HISTORY.event("dashboard","backup_deleted",name,"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"name":name},new_cookie)
            if path=="/api/backups/import":
                fields,files=parse_multipart(self,max_bytes=MAX_BACKUP_BYTES+256000)
                if not files:
                    raise RuntimeError("Choose a Torrent Dashboard backup file")
                _,filename,content=files[0]
                item=import_backup(APP_DIR,filename,content)
                HISTORY.event("dashboard","backup_imported",item.get("name", ""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"backup":item},new_cookie)
            if path=="/api/backups/restore":
                if update_state().get("state") in {"installing", "installingRecovery", "waitingForShutdown", "restarting"}:
                    raise RuntimeError("Wait for the application update to finish before restoring a backup")
                data=parse_json_body(self,12000)
                def validate_restored_state():
                    load_config()
                    HISTORY.initialize()
                with CONFIG_STORE.exclusive() as current:
                    result=restore_backup(
                        APP_DIR,str(data.get("name") or ""),current_version=VERSION,current_config=current,
                        history_lock=HISTORY.lock,validator=validate_restored_state,
                    )
                HISTORY.reset_tracking()
                with CACHE_LOCK:
                    CLIENTS.clear(); CACHE.clear()
                SESSIONS.clear()
                try:
                    HISTORY.event("dashboard","backup_restored",result.get("backup",{}).get("name", ""),"",{"client_ip":self.client_ip()})
                except Exception:
                    logging.getLogger(__name__).exception("Backup restored, but its history event could not be recorded")
                return self.send_json(200,{"ok":True,"backup":result.get("backup"),"safety_backup":result.get("safety_backup"),"reauthenticate":True},None)
            if path=="/api/update-source":
                data=parse_json_body(self,10000)
                def update_source_mutation(current):
                    previous_repo=update_repository(current)
                    updated,repo=save_update_source(current,data.get("repository") or "")
                    return updated,(previous_repo,repo)
                updated,(previous_repo,repo)=mutate_config(update_source_mutation)
                if repo != previous_repo:
                    UPDATE_STATE_PATH.unlink(missing_ok=True)
                    if UPDATE_DIR.exists(): shutil.rmtree(UPDATE_DIR, ignore_errors=True)
                HISTORY.event("dashboard","update_source_changed",repo,"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,"repository":repo,"settings":redacted_config(updated)},new_cookie)
            if path=="/api/update-download":
                result=stage_update(cfg)
                HISTORY.event("dashboard","update_downloaded",str(result.get("version") or ""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,result,new_cookie)
            if path=="/api/update-install":
                data=parse_json_body(self,10000)
                result=launch_update_installer(self,cfg,data.get("version"))
                HISTORY.event("dashboard","update_install_started",str(result.get("version") or ""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,result,new_cookie)
            if path=="/api/notification-sound":
                fields, files = parse_multipart(self, max_bytes=MAX_CUSTOM_SOUND_BYTES + 256000)
                if not files:
                    raise RuntimeError("Choose a custom sound file")
                _, filename, content = files[0]
                updated, info = mutate_config(lambda current: store_custom_notification_sound(current, filename, content))
                HISTORY.event("dashboard","notification_sound_changed",info.get("name", ""),"",{"client_ip":self.client_ip()})
                return self.send_json(200,{"ok":True,**info},new_cookie)
            if path=="/api/notification-test":
                send_notification(cfg,"Torrent Dashboard Test","Notifications are configured correctly.")
                return self.send_json(200,{"ok":True},new_cookie)
        except Exception as e:
            return self.send_json(400,{"error":str(e)},new_cookie)
        return self.send_json(404,{"error":"Not found"},new_cookie)

    def setup_authorized(self, data):
        cfg=load_config()
        if cfg.get("setup",{}).get("complete"):
            raise RuntimeError("Setup has already been completed")
        if is_loopback_ip(self.client_ip()):
            return cfg
        supplied=str(data.get("setup_code","")).strip().upper()
        if not supplied or not hmac.compare_digest(supplied,SETUP_CODE):
            raise RuntimeError("The setup code is required for remote first-run setup")
        return cfg

    def setup_test_client(self):
        try:
            data=parse_json_body(self,20000); self.setup_authorized(data)
            server=normalize_qbittorrent_server(data.get("server") or {})
            return self.send_json(200,test_server_connection(server))
        except Exception as e:
            return self.send_json(400,{"error":str(e)})

    def setup_complete(self):
        try:
            data=parse_json_body(self,50000); cfg=self.setup_authorized(data)
            dashboard=data.get("dashboard") or {}; auth=data.get("auth") or {}; servers=data.get("servers") or []
            if not servers:
                raise RuntimeError("Add at least one download client")
            mode=str(auth.get("mode","lan_bypass"))
            if mode not in ("required","lan_bypass","disabled"):
                raise RuntimeError("Invalid authentication mode")
            username=str(auth.get("username") or "admin")[:128]
            password=str(auth.get("password") or "")
            if mode in ("required","lan_bypass") and not password:
                raise RuntimeError("Set a dashboard password. LAN bypass will skip it only for trusted LAN addresses.")
            trusted_interfaces=[str(x) for x in (auth.get("trusted_interfaces") or []) if str(x)]
            trusted_ips=[str(x).strip() for x in (auth.get("trusted_ips") or []) if str(x).strip()]
            detected_ids={x.get("interface_id") for x in detect_network_interfaces()}
            missing=[x for x in trusted_interfaces if x not in detected_ids]
            if missing:
                raise RuntimeError("Selected network interface is unavailable: " + ", ".join(missing))
            for value in trusted_ips:
                normalize_trusted_entry(value)
            if mode=="lan_bypass" and not trusted_interfaces and not trusted_ips:
                raise RuntimeError("Select at least one trusted network interface or add an IP address to the whitelist.")
            normalized=[]
            for item in servers:
                server=normalize_qbittorrent_server(item)
                test_server_connection(server)  # Final verification before anything is saved.
                normalized.append(server)
            out=json.loads(json.dumps(DEFAULT_CONFIG))
            out["setup"]={"complete":True}
            recovery_key=generate_dashboard_recovery_key(); out["recovery"]=recovery_key_record(recovery_key)
            out["dashboard"]["title"]=str(dashboard.get("title") or "Torrent Dashboard")[:128]
            out["dashboard"]["bind_host"]="0.0.0.0"
            out["dashboard"]["port"]=int(dashboard.get("port") or 8765)
            out["auth"]["mode"]=mode
            out["auth"]["trusted_interfaces"]=trusted_interfaces
            out["auth"]["trusted_ips"]=trusted_ips
            admin_user=normalize_user({"username":username,"password":password,"group":"administrator"},require_password=mode in ("required","lan_bypass"))
            out["users"]=[admin_user]
            out["integrations"]=[]
            sync_legacy_auth(out)
            out["servers"]=normalized
            def commit_setup(current):
                if current.get("setup",{}).get("complete"):
                    raise RuntimeError("Setup has already been completed")
                return out,None
            out,_=mutate_config(commit_setup)
            with CACHE_LOCK:
                CLIENTS.clear()
            with CACHE_LOCK:
                CACHE.clear()
            if mode=="disabled": auth_kind="disabled"
            elif mode=="lan_bypass" and is_trusted_ip(self.client_ip(),effective_trusted_cidrs(out["auth"])): auth_kind="lan_bypass"
            else: auth_kind="password"
            token,sess=SESSIONS.create(username,out["auth"].get("session_hours",24),auth_kind,group="administrator",user_id=admin_user["id"],display_name=user_display_name(admin_user))
            HISTORY.event("dashboard","setup_completed",username,"",{"client_ip":self.client_ip(),"servers":len(normalized),"auth_mode":mode})
            return self.send_json(200,{"ok":True,"csrf":sess["csrf"],"message":"Setup complete","recovery_key":recovery_key},token)
        except Exception as e:
            return self.send_json(400,{"error":str(e)})

    def register_route(self):
        cfg=load_config(); ip=self.client_ip(); now=time.time()
        if not cfg.get("setup",{}).get("complete"):
            return self.send_json(409,{"error":"Finish dashboard setup before registering an account"})
        if str((cfg.get("auth") or {}).get("mode") or "required") == "disabled":
            return self.send_json(409,{"error":"Account registration is unavailable while authentication is disabled"})
        with REGISTRATION_LOCK:
            if not _record_attempt(REGISTRATION_ATTEMPTS,ip,now,3600,5):
                return self.send_json(429,{"error":"Too many registration attempts. Try again later."})
        try: data=parse_json_body(self,20000)
        except Exception as e: return self.send_json(400,{"error":str(e)})
        if str(data.get("password") or "") != str(data.get("password2") or ""):
            return self.send_json(400,{"error":"Passwords do not match"})
        try:
            updated,user=mutate_config(lambda current: register_user(current,data))
            HISTORY.event("dashboard","user_registration_pending",user.get("username",""),"",{"client_ip":ip,"user_id":user.get("id","")})
            return self.send_json(202,{"ok":True,"status":"pending","message":"Registration submitted. Your account is Pending."})
        except Exception as e:
            return self.send_json(400,{"error":str(e)})

    def login_route(self):
        cfg=load_config(); a=cfg["auth"]; ip=self.client_ip(); now=time.time(); limit=max(1,int(a.get("max_login_attempts_per_10m",20)))
        with LOGIN_LOCK:
            if not _record_attempt(LOGIN_ATTEMPTS,ip,now,600,limit):
                return self.send_json(429,{"error":"Too many login attempts"})
        try: data=parse_json_body(self,10000)
        except Exception as e: return self.send_json(400,{"error":str(e)})
        username=str(data.get("username","")).strip()
        user=user_by_username(cfg,username)
        encoded=str((user or {}).get("password_hash") or "")
        # Always run one PBKDF2 verification so unknown usernames take as long
        # to reject as wrong passwords.
        password_ok=verify_password(str(data.get("password","")),encoded or _DUMMY_PASSWORD_HASH)
        if not user or not encoded or not password_ok:
            HISTORY.event("dashboard", "login_failed", username[:128], "", {"client_ip": ip})
            return self.send_json(401,{"error":"Invalid username or password"})
        if user.get("status") == "pending":
            HISTORY.event("dashboard", "login_pending_approval", username[:128], "", {"client_ip": ip, "user_id": user.get("id", "")})
            return self.send_json(403,{"error":"Your account is Pending. An administrator must approve it before you can sign in."})
        token,sess=SESSIONS.create(user["username"],a.get("session_hours",24),"password",group=user.get("group","standard"),user_id=user.get("id",""),display_name=user_display_name(user))
        HISTORY.event("dashboard", "login_success", user["username"], "", {"client_ip": ip,"group":user.get("group")})
        return self.send_json(200,{"ok":True,"csrf":sess["csrf"],"group":user.get("group")},token)

    def serve_static(self,name,content_type=None):
        safe=(STATIC_DIR/name).resolve()
        if STATIC_DIR.resolve() not in safe.parents and safe!=STATIC_DIR.resolve(): return self.send_bytes(403,b"Forbidden","text/plain")
        if not safe.exists() or not safe.is_file(): return self.send_bytes(404,b"Not found","text/plain")
        ctype=content_type or mimetypes.guess_type(str(safe))[0] or "application/octet-stream"
        return self.send_bytes(200,safe.read_bytes(),ctype)

    def update_check(self,cfg,new_cookie):
        try:
            repo = update_repository(cfg)
        except Exception as e:
            return self.send_json(200,{"configured":False,"currentVersion":VERSION,"error":str(e),"state":update_state(),"releaseHistory":local_release_history()},new_cookie)
        try:
            manifest=fetch_update_manifest(cfg)
            return self.send_json(200,{"configured":True,"repository":repo,"currentVersion":VERSION,"manifest":manifest,"updateAvailable":manifest.get("updateAvailable",False),"state":update_state(),"releaseHistory":local_release_history(manifest)},new_cookie)
        except Exception as e:
            return self.send_json(502,{"configured":True,"repository":repo,"currentVersion":VERSION,"error":str(e),"state":update_state(),"releaseHistory":local_release_history()},new_cookie)


def redacted_config(cfg):
    out = public_config(cfg)
    out["runtime"] = {
        "detected_lan": detect_lan_network(),
        "local_ip": local_lan_ip(),
        "network_interfaces": detect_network_interfaces(),
        "trusted_interface_networks": interface_networks(cfg.get("auth", {}).get("trusted_interfaces", [])),
        "effective_trusted_cidrs": effective_trusted_cidrs(cfg.get("auth", {})),
        "updateState": update_state(),
        "releaseHistory": local_release_history(),
    }
    return out


def apply_settings_update(cfg,data):
    # Core settings are intentionally separate from user and integration CRUD.
    out=json.loads(json.dumps(cfg))
    dash=data.get("dashboard",{})
    if not isinstance(dash,dict): raise RuntimeError("Dashboard settings must be an object")
    out.setdefault("dashboard",{})
    # Validate types and ranges instead of storing whatever was posted. A
    # non-numeric history_sample_seconds used to crash the collector thread.
    numeric={
        "port":("Dashboard port",1,65535),
        "history_retention_days":("History retention",1,3650),
        "history_sample_seconds":("History sample interval",5,3600),
        "low_disk_gb":("Low disk warning",0,1_000_000),
    }
    for k,(label,low,high) in numeric.items():
        if k in dash:
            try:
                value=float(dash[k]) if k=="low_disk_gb" else int(dash[k])
            except (TypeError,ValueError) as exc:
                raise RuntimeError(f"{label} must be a number") from exc
            if value!=value or value<low or value>high:
                raise RuntimeError(f"{label} must be between {low} and {high}")
            out["dashboard"][k]=int(value) if float(value).is_integer() else value
    if "title" in dash:
        out["dashboard"]["title"]=str(dash.get("title") or "Torrent Dashboard").strip()[:128] or "Torrent Dashboard"
    if "https_enabled" in dash:
        out["dashboard"]["https_enabled"]=bool(dash["https_enabled"])
    for k in ("https_cert","https_key"):
        if k in dash: out["dashboard"][k]=str(dash.get(k) or "").strip()[:4096]
    if out["dashboard"].get("https_enabled") and not (out["dashboard"].get("https_cert") and out["dashboard"].get("https_key")):
        raise RuntimeError("HTTPS requires both a certificate and a key path")
    out["dashboard"].pop("read_only",None)
    out["dashboard"].pop("refresh_seconds",None)
    auth=data.get("auth",{})
    if "mode" in auth:
        if auth["mode"] not in ("required","lan_bypass","disabled"): raise RuntimeError("Invalid auth mode")
        if auth["mode"] in ("required","lan_bypass") and not any(u.get("password_hash") for u in out.get("users",[])):
            raise RuntimeError("Set a user password in User Management before enabling password-protected access")
        out["auth"]["mode"]=auth["mode"]
    if "trusted_interfaces" in auth:
        ids=[str(x) for x in (auth.get("trusted_interfaces") or []) if str(x)]
        detected_ids={x.get("interface_id") for x in detect_network_interfaces()}
        missing=[x for x in ids if x not in detected_ids]
        if missing: raise RuntimeError("Selected network interface is unavailable: " + ", ".join(missing))
        out["auth"]["trusted_interfaces"]=ids
    if "trusted_ips" in auth:
        values=[str(x).strip() for x in (auth.get("trusted_ips") or []) if str(x).strip()]
        for value in values: normalize_trusted_entry(value)
        out["auth"]["trusted_ips"]=values
    if out["auth"].get("mode")=="lan_bypass" and not out["auth"].get("trusted_interfaces") and not out["auth"].get("trusted_ips"):
        raise RuntimeError("Select at least one trusted network interface or add an IP address to the whitelist.")
    if "servers" in data:
        existing={s.get("id"):s for s in out.get("servers",[])}; new=[]
        for s in data["servers"]:
            sid=str(s.get("id") or uuid.uuid4().hex[:8])[:64]
            prev=existing.get(sid,{})
            item=normalize_qbittorrent_server({**s,"id":sid},prev)
            new.append(item)
        out["servers"]=new
    if "notifications" in data:
        if not isinstance(data["notifications"],dict): raise RuntimeError("Notification settings must be an object")
        for k,v in data["notifications"].items():
            if k not in out["notifications"] or v=="<configured>": continue
            if k in ("custom_sound_file","custom_sound_name","custom_sound_mime"):
                continue  # Managed only by the sound upload endpoint.
            current=out["notifications"][k]
            if isinstance(current,bool):
                v=bool(v)
            elif k=="volume":
                v=_bounded_int(v,current if isinstance(current,int) else 72,0,100)
            elif isinstance(current,str):
                v=str(v if v is not None else "")[:4096]
            out["notifications"][k]=v
    sync_legacy_auth(out)
    return out


def set_password_cli(password):
    def update_password(cfg):
        admin=next((u for u in cfg.get("users",[]) if u.get("group")=="administrator"),None)
        if admin:
            admin["password_hash"]=hash_password(password)
        else:
            cfg.setdefault("users",[]).append(normalize_user({"username":cfg.get("auth",{}).get("username") or "admin","password":password,"group":"administrator"},require_password=True))
        sync_legacy_auth(cfg)
        return cfg,None
    mutate_config(update_password)
    print("Dashboard password updated.")


def main():
    parser=argparse.ArgumentParser(description="Torrent Dashboard")
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--set-password",action="store_true")
    args=parser.parse_args()
    instance_lock=SingleInstanceLock()
    if not instance_lock.acquire():
        print("Torrent Dashboard is already running on this computer.")
        print("Close the existing dashboard process before starting another instance.")
        raise SystemExit(3)
    atexit.register(instance_lock.release)
    if args.set_password:
        import getpass
        p1=getpass.getpass("New dashboard password: "); p2=getpass.getpass("Confirm password: ")
        if not p1 or p1!=p2: raise SystemExit("Passwords did not match or were empty.")
        set_password_cli(p1); return
    cfg=load_config(); host=str(cfg["dashboard"].get("bind_host","0.0.0.0")); port=int(cfg["dashboard"].get("port",8765))
    stop=threading.Event(); threading.Thread(target=collector_loop,args=(stop,),daemon=True).start()
    server=ThreadingHTTPServer((host,port),Handler)
    scheme="http"
    if cfg["dashboard"].get("https_enabled"):
        cert=cfg["dashboard"].get("https_cert",""); key=cfg["dashboard"].get("https_key","")
        if not cert or not key: raise SystemExit("HTTPS is enabled but https_cert/https_key are not configured")
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version=ssl.TLSVersion.TLSv1_2; ctx.load_cert_chain(cert,key)
        # Defer the TLS handshake to the per-connection handler thread. With the
        # default (handshake inside accept()), one client that connects and
        # never completes the handshake stalls the accept loop for everyone.
        server.socket=ctx.wrap_socket(server.socket,server_side=True,do_handshake_on_connect=False); scheme="https"
    print(f"Torrent Dashboard {VERSION}")
    print(f"Process ID: {os.getpid()}")
    print(f"Application: {APP_DIR}")
    print(f"Python: {sys.executable}")
    print(f"Listening on {scheme}://{host}:{port}")
    print(f"Local IP Address: {local_lan_ip()}")
    print(f"Port: {port}")
    if cfg.get("setup",{}).get("complete"):
        print(f"Recovery: {scheme}://127.0.0.1:{port}/ (Recovery tab)")
    if not cfg.get("setup",{}).get("complete"):
        print("First-run setup is required. The local browser can configure Torrent Dashboard directly.")
        print(f"Remote setup code: {SETUP_CODE}")
    mode=cfg["auth"].get("mode")
    if mode=="disabled" and host not in ("127.0.0.1","::1","localhost"):
        print("WARNING: dashboard authentication is DISABLED on a non-loopback interface.")
    elif mode=="lan_bypass":
        print("Authentication mode: LAN bypass (selected trusted networks and whitelisted IPs do not require a password).")
    else: print("Authentication mode: required")
    if cfg["dashboard"].get("open_browser",True) and not args.no_browser:
        threading.Timer(.7,lambda:webbrowser.open(f"{scheme}://127.0.0.1:{port}")).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: stop.set(); server.server_close()

if __name__=="__main__": main()
