#!/usr/bin/env python3
"""Torrent Dashboard update and recovery installer.

The updater supports both the editable source distribution and the compiled
Windows distribution. Release assets are selected explicitly for the installed
distribution, verified against GitHub's SHA-256 digest, installed with a local
rollback backup, restarted, health-checked, and rolled back on failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# Allow this entry point to keep working after it moves under src/torrent_dashboard.
if __package__ in (None, ""):
    _candidate = Path(__file__).resolve().parents[1]
    if _candidate.name == "src" and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from torrent_dashboard.runtime_paths import (
    app_dir,
    dashboard_command,
    runtime_distribution,
    source_python_env,
)

PRESERVE_TOP_LEVEL = {"config.json", "data", ".git"}
DEFAULT_REPOSITORY = "CynicaGaming/Torrent-Dashboard"
RETIRED_REPOSITORIES = ("CynicaGaming/TorrentDashboard",)
GITHUB_API = "https://api.github.com"
USER_AGENT = "Torrent-Dashboard-Recovery-Updater"
WINDOWS_DISTRIBUTION = "windows-x64"
SOURCE_DISTRIBUTION = "source"


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
            handle = ctypes.windll.kernel32.OpenProcess(process_query, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.25)
    return not process_exists(pid)


def dashboard_instance_running() -> bool:
    """Check the same machine-level guard used by the dashboard."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\TorrentDashboard.SingleInstance")
        if not handle:
            return True
        already_exists = kernel32.GetLastError() == 183
        kernel32.CloseHandle(handle)
        return already_exists

    import fcntl

    lock_path = Path(tempfile.gettempdir()) / "torrent-dashboard.lock"
    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        lock_file.close()


def read_package_info(root: Path) -> dict:
    path = Path(root) / "package-info.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def installation_distribution(target: Path) -> str:
    info = read_package_info(target)
    distribution = str(info.get("distribution") or "").strip().lower()
    if distribution in (SOURCE_DISTRIBUTION, WINDOWS_DISTRIBUTION):
        return distribution
    return runtime_distribution(target)


def source_dashboard_path(root: Path) -> Path | None:
    candidates = (
        root / "src" / "torrent_dashboard" / "dashboard.py",
        root / "dashboard.py",
    )
    return next((path for path in candidates if path.is_file()), None)


def source_updater_path(root: Path) -> Path | None:
    candidates = (
        root / "src" / "torrent_dashboard" / "updater.py",
        root / "updater.py",
    )
    return next((path for path in candidates if path.is_file()), None)


def detect_release_distribution(source: Path) -> str:
    info = read_package_info(source)
    distribution = str(info.get("distribution") or "").strip().lower()
    if distribution == WINDOWS_DISTRIBUTION:
        required = ("Dashboard.exe", "Recovery.exe", "Updater.exe")
        if all((source / name).is_file() for name in required):
            return WINDOWS_DISTRIBUTION
    if distribution == SOURCE_DISTRIBUTION and source_dashboard_path(source):
        return SOURCE_DISTRIBUTION
    if all((source / name).is_file() for name in ("Dashboard.exe", "Recovery.exe", "Updater.exe")):
        return WINDOWS_DISTRIBUTION
    if source_dashboard_path(source):
        return SOURCE_DISTRIBUTION
    raise RuntimeError("Release does not contain a recognized Torrent Dashboard application")


def iter_release_files(source: Path):
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        if rel.parts and rel.parts[0] in PRESERVE_TOP_LEVEL:
            continue
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        yield path, rel


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _safe_managed_rel(value: str) -> Path:
    rel = Path(str(value or ""))
    if not str(rel) or rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError(f"Invalid managed update path: {value}")
    if rel.parts[0] in PRESERVE_TOP_LEVEL:
        raise RuntimeError(f"Release attempted to manage preserved path: {value}")
    return rel


def _managed_removals(source: Path) -> list[Path]:
    info = read_package_info(source)
    distribution = detect_release_distribution(source)
    values = []
    if distribution == WINDOWS_DISTRIBUTION:
        values.extend(info.get("managed_roots") or [])
    values.extend(info.get("migration_remove") or [])
    out = []
    seen = set()
    for value in values:
        rel = _safe_managed_rel(value)
        key = rel.as_posix()
        if key not in seen:
            seen.add(key)
            out.append(rel)
    return out


def apply_update(source: Path, target: Path, backup_root: Path):
    """Install one staged release and return complete rollback state."""
    backup_root.mkdir(parents=True, exist_ok=True)
    overwritten: list[str] = []
    created: list[str] = []
    removed: list[str] = []

    # Compiled packages replace complete managed roots to avoid stale PyInstaller
    # libraries. Src-layout migrations can also retire legacy root files here.
    for rel in _managed_removals(source):
        existing = target / rel
        if not existing.exists() and not existing.is_symlink():
            continue
        backup = backup_root / "removed" / rel
        _copy_path(existing, backup)
        _remove_path(existing)
        removed.append(rel.as_posix())

    for src, rel in iter_release_files(source):
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            backup = backup_root / "overwritten" / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup)
            overwritten.append(rel.as_posix())
        else:
            created.append(rel.as_posix())
        shutil.copy2(src, dst)

    state = {"overwritten": overwritten, "created": created, "removed": removed}
    (backup_root / "rollback.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def rollback(target: Path, backup_root: Path, state: dict):
    for rel_value in state.get("created", []):
        try:
            path = target / Path(rel_value)
            if path.exists() and path.is_file():
                path.unlink()
        except Exception:
            pass

    for rel_value in state.get("overwritten", []):
        rel = Path(rel_value)
        source = backup_root / "overwritten" / rel
        destination = target / rel
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    for rel_value in state.get("removed", []):
        rel = Path(rel_value)
        backup = backup_root / "removed" / rel
        destination = target / rel
        try:
            if destination.exists() or destination.is_symlink():
                _remove_path(destination)
            if backup.exists():
                _copy_path(backup, destination)
        except Exception:
            pass


def start_dashboard(target: Path):
    cmd = dashboard_command(target)
    kwargs = {
        "cwd": str(target),
        "stdin": subprocess.DEVNULL,
        "env": source_python_env(target),
    }
    log_dir = target / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "update-restart.log", "ab", buffering=0)
    kwargs["stdout"] = log
    kwargs["stderr"] = log
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs), log


def health_url(target: Path, fallback_port: int = 8765):
    cfg_path = target / "config.json"
    scheme, port = "http", fallback_port
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        dashboard = cfg.get("dashboard", {})
        port = int(dashboard.get("port", fallback_port))
        scheme = "https" if dashboard.get("https_enabled") else "http"
    except Exception:
        pass
    return f"{scheme}://127.0.0.1:{port}/health"


def wait_for_health(url: str, expected_version: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2, context=context) as response:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("ok") and str(data.get("version")) == expected_version:
                    return True
        except Exception:
            pass
        time.sleep(0.75)
    return False


def _version_from_python(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    for pattern in (
        r'^VERSION\s*=\s*["\']([^"\']+)',
        r'^__version__\s*=\s*["\']([^"\']+)',
    ):
        match = re.search(pattern, text, re.M)
        if match:
            return match.group(1)
    return None


def current_version(target: Path) -> str:
    info = read_package_info(target)
    version = str(info.get("version") or "").strip()
    if version_key(version):
        return version.lstrip("vV")
    candidates = (
        target / "src" / "torrent_dashboard" / "__init__.py",
        target / "src" / "torrent_dashboard" / "dashboard.py",
        target / "dashboard.py",
    )
    for path in candidates:
        version = _version_from_python(path)
        if version:
            return version
    return "0.0.0"


def version_key(value: str):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    if not match:
        return None
    return tuple(int(value) for value in match.groups())


def configured_repository(target: Path) -> str:
    repository = DEFAULT_REPOSITORY
    try:
        cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
        repository = str(cfg.get("updates", {}).get("repository") or repository).strip()
    except Exception:
        pass
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError(f"Invalid GitHub repository: {repository}")
    if repository.lower() in {repo.lower() for repo in RETIRED_REPOSITORIES}:
        repository = DEFAULT_REPOSITORY
    return repository


def github_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def release_asset_name(version: str, distribution: str) -> str:
    if distribution == WINDOWS_DISTRIBUTION:
        return f"TorrentDashboard-Windows-{version}-x64.zip"
    return f"Torrent-Dashboard-{version}.zip"


def newest_release(repository: str, distribution: str = SOURCE_DISTRIBUTION):
    releases = github_json(f"{GITHUB_API}/repos/{repository}/releases?per_page=30")
    candidates = []
    for release in releases if isinstance(releases, list) else []:
        if release.get("draft"):
            continue
        version = str(release.get("tag_name") or "").lstrip("v")
        key = version_key(version)
        if not key:
            continue
        expected_name = release_asset_name(version, distribution)
        asset = next((item for item in release.get("assets", []) if item.get("name") == expected_name), None)
        if not asset:
            continue
        candidates.append((key, version, release, asset))
    if not candidates:
        raise RuntimeError(f"No installable Torrent Dashboard {distribution} GitHub release was found")
    return max(candidates, key=lambda item: item[0])[1:]


def download_file(url: str, destination: Path):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def extract_release(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            candidate = (destination / member.filename).resolve()
            if candidate != root and root not in candidate.parents:
                raise RuntimeError("Release archive contains an unsafe path")
        zipped.extractall(destination)

    entries = [path for path in destination.iterdir() if path.name != "__MACOSX"]
    candidates = [destination]
    if len(entries) == 1 and entries[0].is_dir():
        candidates.insert(0, entries[0])
    for candidate in candidates:
        try:
            detect_release_distribution(candidate)
            return candidate
        except RuntimeError:
            continue
    raise RuntimeError("Release archive does not contain a valid Torrent Dashboard application")


def write_staged_release_info(
    source: Path,
    version: str,
    asset: dict,
    sha256: str,
    repository: str,
    release: dict,
):
    digest = str(sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("Release package SHA-256 is invalid")
    payload = {
        "schema": 1,
        "version": str(version),
        "package": str(asset.get("name") or release_asset_name(version, detect_release_distribution(source))),
        "sha256": digest,
        "repository": str(repository or ""),
        "releaseUrl": str(release.get("html_url") or ""),
        "publishedAt": str(release.get("published_at") or release.get("created_at") or ""),
        "channel": "prerelease" if release.get("prerelease") else "stable",
        "commit": str(release.get("target_commitish") or ""),
    }
    (source / "release-info.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def validate_staged_source(source: Path, expected_version: str, expected_distribution: str | None = None):
    distribution = detect_release_distribution(source)
    if expected_distribution and distribution != expected_distribution:
        raise RuntimeError(
            f"Release distribution mismatch: expected {expected_distribution}, downloaded {distribution}"
        )
    if not (source / "static" / "index.html").is_file():
        raise RuntimeError("Release is missing static/index.html")

    if distribution == WINDOWS_DISTRIBUTION:
        for name in ("Dashboard.exe", "Recovery.exe", "Updater.exe"):
            if not (source / name).is_file():
                raise RuntimeError(f"Windows release is missing {name}")
        info = read_package_info(source)
        if str(info.get("version") or "").lstrip("vV") != expected_version:
            raise RuntimeError("Windows release version does not match the downloaded package")
    else:
        for path in (source_dashboard_path(source), source_updater_path(source)):
            if path is None:
                raise RuntimeError("Source release is missing a required Python entry point")
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        if current_version(source) != expected_version:
            raise RuntimeError("Source release version does not match the downloaded package")
    return distribution


def recovery_update(
    target: Path,
    repository: str | None = None,
    force: bool = False,
    wait_pid: int | None = None,
):
    target = target.resolve()
    if wait_pid and wait_pid != os.getpid() and not wait_for_exit(wait_pid, 30):
        raise RuntimeError("The recovery process did not exit before the update timeout")
    if dashboard_instance_running():
        raise RuntimeError(
            "Torrent Dashboard is still running. Close the existing dashboard process, then run the recovery update again."
        )

    repository = repository or configured_repository(target)
    distribution = installation_distribution(target)
    installed = current_version(target)
    version, release, asset = newest_release(repository, distribution)
    installed_key = version_key(installed)
    latest_key = version_key(version)
    if not force and installed_key and latest_key and latest_key <= installed_key:
        print(f"Torrent Dashboard {installed} is already current.")
        return

    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError("GitHub did not provide a SHA-256 digest for the release asset")
    expected_digest = digest.partition(":")[2].lower()
    download_url = str(asset.get("browser_download_url") or "")
    if not download_url.startswith("https://github.com/"):
        raise RuntimeError("Release asset has an invalid download URL")

    print(f"Installed version: {installed}")
    print(f"Latest version:    {version}")
    print(f"Distribution:      {distribution}")
    print(f"Repository:        {repository}")
    print("Downloading and verifying release...")

    data_dir = target / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    status_path = data_dir / "update-status.json"
    with tempfile.TemporaryDirectory(prefix="recovery-update-", dir=str(data_dir)) as temp_name:
        temp = Path(temp_name)
        archive = temp / str(asset.get("name") or release_asset_name(version, distribution))
        actual_digest = download_file(download_url, archive)
        if actual_digest.lower() != expected_digest:
            raise RuntimeError(
                f"Release SHA-256 verification failed (expected {expected_digest}, got {actual_digest})"
            )
        source = extract_release(archive, temp / "release")
        validate_staged_source(source, version, distribution)
        write_staged_release_info(source, version, asset, actual_digest, repository, release)

        backup_root = data_dir / "backups" / f"pre-{version}-{int(time.time())}"
        status_path.write_text(
            json.dumps({"state": "installingRecovery", "version": version, "distribution": distribution}),
            encoding="utf-8",
        )
        state = apply_update(source, target, backup_root)
        status_path.write_text(json.dumps({"state": "restarting", "version": version}), encoding="utf-8")
        process, log = start_dashboard(target)
        if wait_for_health(health_url(target), version):
            status_path.write_text(
                json.dumps({"state": "installed", "version": version, "backup": str(backup_root)}),
                encoding="utf-8",
            )
            log.close()
            print(f"Torrent Dashboard {version} installed and restarted successfully.")
            return

        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        rollback(target, backup_root, state)
        status_path.write_text(json.dumps({"state": "rolledBack", "version": version}), encoding="utf-8")
        try:
            _, old_log = start_dashboard(target)
            old_log.close()
        except Exception as exc:
            status_path.write_text(
                json.dumps({"state": "rollbackRestartFailed", "version": version, "error": str(exc)}),
                encoding="utf-8",
            )
        log.close()
        raise RuntimeError("The updated dashboard failed its health check and was rolled back")


def normal_update(args):
    missing = [name for name in ("pid", "source", "target", "version") if getattr(args, name) in (None, "")]
    if missing:
        raise RuntimeError("Normal update mode requires --pid, --source, --target, and --version")

    source = Path(args.source).resolve()
    target = Path(args.target).resolve()
    if not source.is_dir():
        raise RuntimeError("Invalid staged update source")
    expected_distribution = installation_distribution(target)
    validate_staged_source(source, str(args.version), expected_distribution)

    data_dir = target / "data"
    backup_root = data_dir / "backups" / f"pre-{args.version}-{int(time.time())}"
    status_path = data_dir / "update-status.json"
    data_dir.mkdir(parents=True, exist_ok=True)

    status_path.write_text(json.dumps({"state": "waitingForShutdown", "version": args.version}), encoding="utf-8")
    if not wait_for_exit(args.pid):
        status_path.write_text(
            json.dumps({"state": "failed", "error": "dashboardDidNotStop", "version": args.version}),
            encoding="utf-8",
        )
        raise RuntimeError("Dashboard did not stop before the update timeout")

    state = apply_update(source, target, backup_root)
    status_path.write_text(json.dumps({"state": "restarting", "version": args.version}), encoding="utf-8")
    process, log = start_dashboard(target)
    if wait_for_health(health_url(target), str(args.version)):
        status_path.write_text(
            json.dumps({"state": "installed", "version": args.version, "backup": str(backup_root)}),
            encoding="utf-8",
        )
        log.close()
        return

    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    rollback(target, backup_root, state)
    status_path.write_text(json.dumps({"state": "rolledBack", "version": args.version}), encoding="utf-8")
    try:
        _, old_log = start_dashboard(target)
        old_log.close()
    except Exception as exc:
        status_path.write_text(
            json.dumps({"state": "rollbackRestartFailed", "version": args.version, "error": str(exc)}),
            encoding="utf-8",
        )
    log.close()
    raise RuntimeError("Updated dashboard failed its health check and was rolled back")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Torrent Dashboard updater and recovery tool")
    parser.add_argument(
        "--github-update",
        action="store_true",
        help="Update directly from configured GitHub releases without using the dashboard UI",
    )
    parser.add_argument("--repository", help="Override the configured owner/repository for recovery mode")
    parser.add_argument("--force", action="store_true", help="Reinstall the latest release even when unchanged")
    parser.add_argument("--pid", type=int, help="Process that must exit before installation")
    parser.add_argument("--source")
    parser.add_argument("--target")
    parser.add_argument("--version")
    args = parser.parse_args(argv)

    try:
        if args.github_update:
            target = Path(args.target).resolve() if args.target else app_dir()
            recovery_update(target, args.repository, args.force, wait_pid=args.pid)
        else:
            normal_update(args)
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
