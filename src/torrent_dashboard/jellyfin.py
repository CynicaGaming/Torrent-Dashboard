"""Jellyfin service-integration runtime for status, libraries, scheduled tasks, and explicit actions."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from torrent_dashboard import __version__

MAX_JELLYFIN_RESPONSE_BYTES = 2_000_000
DEFAULT_JELLYFIN_TIMEOUT = 7


def find_jellyfin_integration(cfg, integration_id):
    """Return one saved Jellyfin integration without exposing any secret fields."""
    integration_id = str(integration_id or "").strip()
    if not integration_id:
        raise RuntimeError("Jellyfin integration ID is required")
    item = next(
        (
            entry
            for entry in cfg.get("integrations", [])
            if str(entry.get("id") or "") == integration_id
        ),
        None,
    )
    if not item:
        raise RuntimeError("Jellyfin integration was not found")
    if str(item.get("type") or "").lower() != "jellyfin":
        raise RuntimeError("Integration is not a Jellyfin connection")
    return item


def _jellyfin_connection(item):
    if str(item.get("type") or "").lower() != "jellyfin":
        raise RuntimeError("Integration is not a Jellyfin connection")
    url = str(item.get("url") or "").strip().rstrip("/")
    api_key = str(item.get("api_key") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("Jellyfin URL must start with http:// or https://")
    if not api_key:
        raise RuntimeError("Jellyfin API key is required")
    return url, api_key


def _header_value(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def jellyfin_authorization_header(api_key):
    """Build the ``Authorization: MediaBrowser ...`` header value.

    Jellyfin 12 disables the legacy ``X-Emby-Token``/``X-Emby-Authorization``
    headers and ``api_key`` query parameter by default, so those requests get
    HTTP 401. The ``Authorization`` header form has been accepted since
    Jellyfin 10.8, so it works for both older and current servers.
    """
    fields = (
        ("Client", "Torrent Dashboard"),
        ("Device", "Torrent Dashboard"),
        ("DeviceId", "torrent-dashboard"),
        ("Version", __version__),
        ("Token", api_key),
    )
    return "MediaBrowser " + ", ".join(f'{name}="{_header_value(value)}"' for name, value in fields)


def _jellyfin_request(item, endpoint, *, method="GET", expect_json=True, opener=None, timeout=DEFAULT_JELLYFIN_TIMEOUT):
    url, api_key = _jellyfin_connection(item)
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        url + endpoint,
        headers={
            "Authorization": jellyfin_authorization_header(api_key),
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(MAX_JELLYFIN_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Jellyfin returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to Jellyfin: {exc.reason}") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not connect to Jellyfin: {exc}") from exc
    if len(body) > MAX_JELLYFIN_RESPONSE_BYTES:
        raise RuntimeError("Jellyfin returned an unexpectedly large response")
    if not expect_json:
        return None
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Jellyfin returned an invalid response") from exc


def jellyfin_server_info(item, *, opener=None):
    """Return a browser-safe summary of the connected Jellyfin server."""
    data = _jellyfin_request(item, "/System/Info", opener=opener)
    if not isinstance(data, dict):
        raise RuntimeError("Jellyfin returned an invalid system response")
    return {
        "name": str(data.get("ServerName") or item.get("name") or "Jellyfin").strip(),
        "version": str(data.get("Version") or data.get("ProductVersion") or "").strip(),
        "operating_system": str(data.get("OperatingSystem") or "").strip(),
        "pending_restart": bool(data.get("HasPendingRestart", False)),
    }


def jellyfin_libraries(item, *, opener=None):
    """Return normalized Jellyfin virtual-folder/library information."""
    data = _jellyfin_request(item, "/Library/VirtualFolders", opener=opener)
    if not isinstance(data, list):
        raise RuntimeError("Jellyfin returned an invalid library response")
    libraries = []
    for source in data:
        if not isinstance(source, dict):
            continue
        raw_progress = source.get("RefreshProgress")
        try:
            progress = float(raw_progress) if raw_progress is not None else None
        except (TypeError, ValueError):
            progress = None
        if progress is not None:
            progress = max(0.0, min(100.0, progress))
        locations = [
            str(value).strip()
            for value in (source.get("Locations") or [])
            if str(value).strip()
        ]
        libraries.append({
            "id": str(source.get("ItemId") or "").strip(),
            "name": str(source.get("Name") or "Library").strip(),
            "collection_type": str(source.get("CollectionType") or "").strip(),
            "locations": locations,
            "refresh_status": str(source.get("RefreshStatus") or "").strip(),
            "refresh_progress": progress,
        })
    libraries.sort(key=lambda item: item["name"].lower())
    return libraries



def _normalized_task_progress(value):
    try:
        progress = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if progress is None:
        return None
    return max(0.0, min(100.0, progress))


def _normalized_scheduled_task(source):
    if not isinstance(source, dict):
        return None
    task_id = str(source.get("Id") or "").strip()
    if not task_id:
        return None
    result = source.get("LastExecutionResult") if isinstance(source.get("LastExecutionResult"), dict) else {}
    state = str(source.get("State") or "Idle").strip() or "Idle"
    return {
        "id": task_id,
        "key": str(source.get("Key") or "").strip(),
        "name": str(source.get("Name") or "Scheduled task").strip(),
        "description": str(source.get("Description") or "").strip(),
        "category": str(source.get("Category") or "Other").strip() or "Other",
        "state": state,
        "running": state.lower() in ("running", "cancelling"),
        "progress": _normalized_task_progress(source.get("CurrentProgressPercentage")),
        "last_status": str(result.get("Status") or "").strip(),
        "last_start": str(result.get("StartTimeUtc") or "").strip(),
        "last_end": str(result.get("EndTimeUtc") or "").strip(),
        "last_error": str(result.get("ErrorMessage") or "").strip(),
    }


def jellyfin_scheduled_tasks(item, *, opener=None):
    """Return all non-hidden Jellyfin scheduled tasks, including plugin tasks."""
    data = _jellyfin_request(item, "/ScheduledTasks?isHidden=false", opener=opener)
    if not isinstance(data, list):
        raise RuntimeError("Jellyfin returned an invalid scheduled-task response")
    tasks = []
    for source in data:
        task = _normalized_scheduled_task(source)
        if task:
            tasks.append(task)
    tasks.sort(key=lambda task: (task["category"].lower(), task["name"].lower()))
    return tasks


def _scheduled_task_endpoint(task_id):
    task_id = str(task_id or "").strip()
    if not task_id:
        raise RuntimeError("Jellyfin scheduled task ID is required")
    return "/ScheduledTasks/Running/" + urllib.parse.quote(task_id, safe="")


def start_jellyfin_scheduled_task(item, task_id, *, opener=None):
    """Start one Jellyfin scheduled task by ID."""
    _jellyfin_request(
        item,
        _scheduled_task_endpoint(task_id),
        method="POST",
        expect_json=False,
        opener=opener,
    )
    return {"ok": True, "message": "Jellyfin scheduled task started"}


def stop_jellyfin_scheduled_task(item, task_id, *, opener=None):
    """Stop one running Jellyfin scheduled task by ID."""
    _jellyfin_request(
        item,
        _scheduled_task_endpoint(task_id),
        method="DELETE",
        expect_json=False,
        opener=opener,
    )
    return {"ok": True, "message": "Jellyfin scheduled task stop requested"}

def jellyfin_overview(item, *, opener=None):
    """Return server health metadata and configured libraries for Settings."""
    return {
        "ok": True,
        "server": jellyfin_server_info(item, opener=opener),
        "libraries": jellyfin_libraries(item, opener=opener),
    }


def _is_library_scan_task(task):
    key = str(task.get("key") or "").strip().lower()
    name = str(task.get("name") or "").strip().lower()
    return key == "refreshmedialibrarytask" or "refreshmedialibrary" in key or name in {"scan media library", "scan library"}


def jellyfin_library_scan_task(item, *, opener=None):
    """Return Jellyfin's real Scan Media Library scheduled task."""
    task = next((task for task in jellyfin_scheduled_tasks(item, opener=opener) if _is_library_scan_task(task)), None)
    if not task:
        raise RuntimeError("Jellyfin did not report a Scan Media Library scheduled task")
    return task


def refresh_jellyfin_libraries(item, *, opener=None):
    """Start Jellyfin's real Scan Media Library scheduled task."""
    task = jellyfin_library_scan_task(item, opener=opener)
    start_jellyfin_scheduled_task(item, task["id"], opener=opener)
    return {"ok": True, "message": "Jellyfin library scan started", "task": task}


__all__ = [
    "find_jellyfin_integration",
    "jellyfin_authorization_header",
    "jellyfin_libraries",
    "jellyfin_library_scan_task",
    "jellyfin_overview",
    "jellyfin_scheduled_tasks",
    "jellyfin_server_info",
    "refresh_jellyfin_libraries",
    "start_jellyfin_scheduled_task",
    "stop_jellyfin_scheduled_task",
]
