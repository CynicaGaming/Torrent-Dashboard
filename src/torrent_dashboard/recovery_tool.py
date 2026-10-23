#!/usr/bin/env python3
"""Key-gated local recovery tool for Torrent Dashboard.

This program never starts an HTTP server. It is intended to remain usable when
the dashboard UI or HTTP service cannot start. Update operations make outbound
HTTPS requests to GitHub through updater.py.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import subprocess
import re
import shutil
import shlex
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    _src = Path(__file__).resolve().parents[1]
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from torrent_dashboard.runtime_paths import app_dir, is_frozen, source_python_env, updater_command
from torrent_dashboard.recovery_operations import (
    SAFE_TORRENT_ACTIONS,
    history_events,
    jellyfin_task_action,
    list_clients,
    list_integrations,
    list_jellyfin_tasks,
    list_torrents,
    list_users,
    redacted_configuration,
    test_client,
    test_integration,
    torrent_action,
)
from torrent_dashboard.recovery_update_staging import read_staged_update, stage_latest_update

APP_DIR = app_dir()
CONFIG_PATH = APP_DIR / "config.json"
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "torrent_desk.sqlite3"
BACKUP_DIR = DATA_DIR / "recovery-backups"
UPDATE_STATE_PATH = DATA_DIR / "update-status.json"
UPDATES_DIR = DATA_DIR / "updates"
DEFAULT_REPOSITORY = "CynicaGaming/Torrent-Dashboard"
RETIRED_REPOSITORIES = ("CynicaGaming/TorrentDashboard",)
RECOVERY_KEY_PREFIX = "TDRK"


def normalize_recovery_key(value: str) -> str:
    raw = str(value or "").strip().upper()
    return "".join(ch for ch in raw if ch not in "- \t\r\n")


def verify_pbkdf2(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = str(encoded or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        pad = lambda value: value + "=" * (-len(value) % 4)
        salt = base64.urlsafe_b64decode(pad(salt_b64))
        expected = base64.urlsafe_b64decode(pad(digest_b64))
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(expected, got)
    except Exception:
        return False


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        raise RuntimeError(f"Configuration file was not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Configuration file is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Configuration root must be a JSON object")
    return value


def recovery_key_hash(cfg: dict) -> str:
    recovery = cfg.get("recovery") if isinstance(cfg.get("recovery"), dict) else {}
    return str(recovery.get("key_hash") or "")


def authenticate_key(cfg: dict, supplied: str) -> bool:
    encoded = recovery_key_hash(cfg)
    if not encoded:
        raise RuntimeError("This dashboard has no setup-generated recovery key. Local recovery cannot be unlocked.")
    normalized = normalize_recovery_key(supplied)
    return normalized.startswith(RECOVERY_KEY_PREFIX) and len(normalized) >= 40 and verify_pbkdf2(normalized, encoded)


def require_recovery_key(cfg: dict) -> None:
    for remaining in range(5, 0, -1):
        supplied = getpass.getpass("Recovery key: ")
        if authenticate_key(cfg, supplied):
            return
        if remaining > 1:
            print(f"Invalid recovery key. {remaining - 1} attempt(s) remaining.", file=sys.stderr)
    raise RuntimeError("Recovery authentication failed")


def normalize_repository(value: str) -> str:
    value = str(value or "").strip().removesuffix(".git").strip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):].strip("/")
            break
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise RuntimeError("GitHub repository must be owner/repository")
    return value


def configured_repository(cfg: dict) -> str:
    updates = cfg.get("updates") if isinstance(cfg.get("updates"), dict) else {}
    repository = normalize_repository(updates.get("repository") or DEFAULT_REPOSITORY)
    return DEFAULT_REPOSITORY if repository.lower() in {repo.lower() for repo in RETIRED_REPOSITORIES} else repository


def atomic_write_config(cfg: dict, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    tmp = path.with_suffix(path.suffix + ".recovery.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def backup_config(path: Path | None = None) -> Path:
    path = path or CONFIG_PATH
    if not path.exists():
        raise RuntimeError("config.json does not exist")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"config-{stamp}.json"
    index = 1
    while dest.exists():
        dest = BACKUP_DIR / f"config-{stamp}-{index}.json"
        index += 1
    shutil.copy2(path, dest)
    return dest


def list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("config-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def restore_config(name: str) -> Path:
    candidate = (BACKUP_DIR / Path(str(name)).name).resolve()
    if BACKUP_DIR.resolve() not in candidate.parents or not candidate.is_file():
        raise RuntimeError("Recovery config backup was not found")
    restored = load_config(candidate)
    if not recovery_key_hash(restored):
        raise RuntimeError("Refusing to restore a config backup without a recovery key")
    backup_config()
    atomic_write_config(restored)
    return candidate


def set_repository(cfg: dict, repository: str) -> str:
    repository = normalize_repository(repository)
    backup_config()
    cfg.setdefault("updates", {})["repository"] = repository
    atomic_write_config(cfg)
    return repository


def current_version() -> str:
    package_info = APP_DIR / "package-info.json"
    if package_info.is_file():
        try:
            return str(json.loads(package_info.read_text(encoding="utf-8")).get("version") or "unknown")
        except Exception:
            pass
    for source in (
        APP_DIR / "src" / "torrent_dashboard" / "__init__.py",
        APP_DIR / "src" / "torrent_dashboard" / "dashboard.py",
        APP_DIR / "dashboard.py",
    ):
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        match = re.search(r'^(?:VERSION|__version__)\s*=\s*["\']([^"\']+)', text, re.M)
        if match:
            return match.group(1)
    return "unknown"


def updater_module():
    try:
        import updater
        return updater
    except Exception:
        try:
            from torrent_dashboard import updater
            return updater
        except Exception as exc:
            raise RuntimeError(f"Updater module could not be loaded: {exc}") from exc


def validation_report(cfg: dict) -> list[tuple[str, bool, str]]:
    rows = [
        ("config.json parses", True, str(CONFIG_PATH)),
        ("setup complete", bool(cfg.get("setup", {}).get("complete")), "setup.complete"),
        ("recovery key", bool(recovery_key_hash(cfg)), "setup-generated key hash present"),
    ]
    try:
        rows.append(("update repository", True, configured_repository(cfg)))
    except Exception as exc:
        rows.append(("update repository", False, str(exc)))
    if is_frozen():
        for name in ("Dashboard.exe", "Recovery.exe", "Updater.exe"):
            path = APP_DIR / name
            rows.append((f"{name} present", path.is_file(), str(path)))
        rows.append(("static assets", (APP_DIR / "static" / "index.html").is_file(), str(APP_DIR / "static")))
        rows.append(("package metadata", (APP_DIR / "package-info.json").is_file(), str(APP_DIR / "package-info.json")))
    else:
        for name in ("dashboard.py", "updater.py", "recovery_tool.py"):
            path = APP_DIR / name
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
                rows.append((f"{name} syntax", True, "OK"))
            except Exception as exc:
                rows.append((f"{name} syntax", False, str(exc)))
    return rows


def show_status(cfg: dict) -> None:
    upd = updater_module()
    recovery = cfg.get("recovery", {}) if isinstance(cfg.get("recovery"), dict) else {}
    print(f"Application:       {APP_DIR}")
    print(f"Installed version: {current_version()}")
    print(f"Python:            {sys.executable}")
    print(f"Repository:        {configured_repository(cfg)}")
    print(f"Dashboard running: {'yes' if upd.dashboard_instance_running() else 'no'}")
    print(f"Recovery key:      configured · …{str(recovery.get('last4') or '')}")
    if UPDATE_STATE_PATH.exists():
        try:
            state = json.loads(UPDATE_STATE_PATH.read_text(encoding="utf-8"))
            print(f"Update state:      {state.get('state','unknown')}" + (f" · {state.get('version')}" if state.get('version') else ""))
        except Exception:
            print("Update state:      unreadable")
    else:
        print("Update state:      none")


def check_update(cfg: dict):
    upd = updater_module()
    repo = configured_repository(cfg)
    installed = current_version()
    version, release, asset = upd.newest_release(repo, upd.installation_distribution(APP_DIR))
    print(f"Installed version: {installed}")
    print(f"Latest version:    {version}")
    print(f"Repository:        {repo}")
    print(f"Channel:           {'prerelease' if release.get('prerelease') else 'stable'}")
    print(f"Published:         {release.get('published_at') or release.get('created_at') or 'unknown'}")
    if upd.version_key(installed) and upd.version_key(version):
        relation = "update available" if upd.version_key(version) > upd.version_key(installed) else "current"
        print(f"Status:            {relation}")
    return installed, version, release, asset


def install_update(cfg: dict, force: bool = False) -> None:
    upd = updater_module()
    repo = configured_repository(cfg)
    if upd.dashboard_instance_running():
        raise RuntimeError("Stop Torrent Dashboard before installing from local recovery")
    if is_frozen():
        cmd = updater_command(APP_DIR, detached=True)
        cmd += ["--github-update", "--target", str(APP_DIR), "--repository", repo, "--pid", str(os.getpid())]
        if force:
            cmd.append("--force")
        kwargs = {
            "cwd": str(APP_DIR),
            "stdin": subprocess.DEVNULL,
            "stdout": None,
            "stderr": None,
            "env": source_python_env(APP_DIR),
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(cmd, **kwargs)
        print("Updater.exe launched. Recovery will close so the recovery executable can be replaced safely.")
        raise SystemExit(0)
    upd.recovery_update(APP_DIR, repo, force=force)


def stage_update_download(cfg: dict) -> dict:
    upd = updater_module()
    state = stage_latest_update(APP_DIR, configured_repository(cfg), upd)
    if state.get("state") == "upToDate":
        print(f"Torrent Dashboard {state.get('currentVersion') or current_version()} is already current.")
    else:
        print(f"Verified update {state.get('version')} downloaded and retained for installation.")
        print(f"Package: {state.get('package')}")
    return state


def install_staged_update(requested_version: str | None = None) -> None:
    upd = updater_module()
    if upd.dashboard_instance_running():
        raise RuntimeError("Stop Torrent Dashboard before installing a staged update from local recovery")
    state = read_staged_update(APP_DIR, upd, requested_version)
    command = updater_command(APP_DIR, detached=True)
    command += [
        "--pid", str(os.getpid()),
        "--source", state["source"],
        "--target", str(APP_DIR),
        "--version", state["version"],
    ]
    kwargs = {
        "cwd": str(APP_DIR),
        "stdin": subprocess.DEVNULL,
        "stdout": None,
        "stderr": None,
        "env": source_python_env(APP_DIR),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, **kwargs)
    print(f"Installing verified staged update {state['version']}. Recovery will close so managed files can be replaced safely.")
    raise SystemExit(0)


def network_reset(cfg: dict) -> None:
    backup_config()
    dashboard = cfg.setdefault("dashboard", {})
    dashboard["bind_host"] = "0.0.0.0"
    dashboard["port"] = 8765
    dashboard["https_enabled"] = False
    dashboard["https_cert"] = ""
    dashboard["https_key"] = ""
    atomic_write_config(cfg)
    print("Network settings reset to 0.0.0.0:8765 with HTTPS disabled.")


def clear_update_state() -> None:
    UPDATE_STATE_PATH.unlink(missing_ok=True)
    if UPDATES_DIR.exists():
        shutil.rmtree(UPDATES_DIR, ignore_errors=True)
    print("Staged update files and update-status.json were cleared.")


def start_dashboard() -> None:
    upd = updater_module()
    if upd.dashboard_instance_running():
        print("Torrent Dashboard is already running.")
        return
    proc, log = upd.start_dashboard(APP_DIR)
    log.close()
    print(f"Torrent Dashboard start requested. PID {proc.pid}.")
    print(f"Startup log: {DATA_DIR / 'update-restart.log'}")


def tail_log(lines: int = 80) -> None:
    path = DATA_DIR / "update-restart.log"
    if not path.exists():
        print("No recovery/update restart log exists yet.")
        return
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in text[-max(1, min(500, int(lines))) :]:
        print(line)


def confirm(prompt: str, token: str = "YES") -> bool:
    return input(f"{prompt} Type {token} to continue: ").strip() == token


def parse_command(command: str) -> list[str]:
    raw = str(command or "").strip()
    if not raw:
        return []
    if len(raw) > 4096:
        raise RuntimeError("Recovery command is too long")
    if "\n" in raw or "\r" in raw:
        raise RuntimeError("Run one recovery command at a time")
    try:
        return shlex.split(raw, posix=True)
    except ValueError as exc:
        raise RuntimeError(f"Invalid recovery command: {exc}") from exc


def print_json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def print_update_status() -> None:
    if not UPDATE_STATE_PATH.exists():
        print_json({"state": "idle", "currentVersion": current_version()})
        return
    try:
        value = json.loads(UPDATE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Update status is unreadable: {exc}") from exc
    print_json(value if isinstance(value, dict) else {"state": "unknown"})


def print_help() -> None:
    print("""
Commands:
  status                                      Show local installation and recovery state
  validate                                    Validate config and critical application files
  config show                                 Show redacted dashboard configuration
  client list                                 List configured qBittorrent clients
  client test <client-id>                     Test one qBittorrent client directly
  integration list                            List configured integrations with secrets redacted
  integration test <integration-id>           Test one integration directly
  torrent list [client-id]                    Fetch live torrent identifiers and state from qBittorrent
  torrent action <client-id> <action> <hash|all>
                                              Run start, stop, recheck, or reannounce only
  jellyfin list <integration-id>              List Jellyfin scheduled tasks
  jellyfin start <integration-id> <task-id>   Start a Jellyfin scheduled task
  jellyfin stop <integration-id> <task-id>    Stop a Jellyfin scheduled task
  users                                       Show redacted local user/account records
  events [limit]                              Show recent local dashboard history events
  repo                                        Show the configured GitHub repository
  repo <owner/repository|default>             Back up config and change the update repository
  check                                       Check the configured repository for releases
  install                                     Download, verify, and install the latest newer release
  reinstall                                   Reinstall the latest verified release
  update status                               Show local update state
  update check                                Check for a release
  update repo [owner/repository|default]      Show or change the update repository
  update download                             Download, verify, and retain the latest update
  update install [version]                    Install the retained verified update
  update apply                                Download, verify, and install the latest update
  update reinstall                            Reinstall the latest release directly from GitHub
  update clear                                Clear stuck staged update files/status
  backup                                      Back up config.json
  backups                                     List local config backups
  restore <backup-file>                       Restore a recovery config backup
  network-reset                               Reset bind to 0.0.0.0:8765 and disable HTTPS
  clear-update                                Clear a stuck staged update and update status
  start                                       Start Torrent Dashboard without opening a browser
  log [lines]                                 Show the recovery/update restart log
  help                                        Show this help
  exit                                        Exit local recovery

Recovery.exe is the command and diagnostic recovery surface. The dashboard does not expose
an embedded command console. This is not an operating-system shell and no command starts a
listening recovery server.
""".strip())


def execute(command: str, cfg: dict) -> dict:
    parts = parse_command(command)
    if not parts:
        return cfg
    op = parts[0].lower()
    if op in ("exit", "quit", "q"):
        raise EOFError
    if op in ("help", "?"):
        print_help()
    elif op == "status":
        show_status(cfg)
    elif op == "validate":
        rows = validation_report(cfg)
        for name, ok, detail in rows:
            print(f"{'OK' if ok else 'FAIL':4}  {name}: {detail}")
        if not all(row[1] for row in rows):
            print("One or more recovery checks failed.")
    elif op == "config":
        if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() != "show"):
            raise RuntimeError("Usage: config show")
        print_json(redacted_configuration(cfg))
    elif op in ("clients", "client"):
        if op == "clients" or len(parts) == 1 or parts[1].lower() == "list":
            rows = list_clients(cfg)
            if not rows:
                print("No download clients are configured.")
            for item in rows:
                print(f"{item['id']}  {item['name']}  {'enabled' if item['enabled'] else 'disabled'}  {item['base_url']}")
        elif parts[1].lower() == "test" and len(parts) == 3:
            print_json(test_client(cfg, parts[2]))
        else:
            raise RuntimeError("Usage: client list | client test <client-id>")
    elif op in ("integrations", "integration"):
        if op == "integrations" or len(parts) == 1 or parts[1].lower() == "list":
            rows = list_integrations(cfg)
            if not rows:
                print("No integrations are configured.")
            for item in rows:
                print(f"{item.get('id','')}  {item.get('type','')}  {item.get('name') or item.get('type') or 'Integration'}  {'enabled' if item.get('enabled', True) else 'disabled'}")
        elif parts[1].lower() == "test" and len(parts) == 3:
            print_json(test_integration(cfg, parts[2]))
        else:
            raise RuntimeError("Usage: integration list | integration test <integration-id>")
    elif op in ("torrents", "torrent"):
        if op == "torrents":
            if len(parts) != 1:
                raise RuntimeError("Usage: torrents")
            client_id = None
            rows = list_torrents(cfg)
        elif len(parts) >= 2 and parts[1].lower() == "list":
            if len(parts) > 3:
                raise RuntimeError("Usage: torrent list [client-id]")
            client_id = parts[2] if len(parts) == 3 else None
            rows = list_torrents(cfg, client_id)
        elif len(parts) == 5 and parts[1].lower() == "action":
            action = parts[3].lower()
            if action not in SAFE_TORRENT_ACTIONS:
                raise RuntimeError("Recovery torrent actions are limited to start, stop, recheck, and reannounce")
            result = torrent_action(cfg, parts[2], action, parts[4])
            print(f"Torrent action {action} sent to {result['client_id']} (status {result['status']}).")
            return cfg
        else:
            raise RuntimeError("Usage: torrent list [client-id] | torrent action <client-id> <start|stop|recheck|reannounce> <hash|all>")
        if not rows:
            print("No torrents are currently available.")
        else:
            print("CLIENT  HASH  STATE  PROGRESS  NAME")
            for item in rows:
                print(f"{item['client_id']}  {item['hash'] or '-'}  {item['state']}  {item['progress']:5.1f}%  {item['name']}")
    elif op == "jellyfin":
        if len(parts) < 3:
            raise RuntimeError("Usage: jellyfin list|tasks|start|stop <integration-id> [task-id]")
        action, integration_id = parts[1].lower(), parts[2]
        if action in ("list", "tasks"):
            tasks = list_jellyfin_tasks(cfg, integration_id)
            if not tasks:
                print("Jellyfin reported no visible scheduled tasks.")
            for task in tasks:
                progress = task.get("progress")
                suffix = f" {progress:.0f}%" if isinstance(progress, (int, float)) else ""
                print(f"{task.get('id','')}  [{task.get('category','Other')}] {task.get('name','Scheduled task')}  {task.get('state','Idle')}{suffix}")
        elif action in ("start", "stop") and len(parts) == 4:
            result = jellyfin_task_action(cfg, integration_id, action, parts[3])
            print(result.get("message") or f"Jellyfin task {action} requested.")
        else:
            raise RuntimeError("Usage: jellyfin list|tasks|start|stop <integration-id> [task-id]")
    elif op == "users":
        if len(parts) != 1:
            raise RuntimeError("Usage: users")
        print_json(list_users(cfg))
    elif op == "events":
        if len(parts) > 2:
            raise RuntimeError("Usage: events [limit]")
        try:
            limit = int(parts[1]) if len(parts) == 2 else 50
        except ValueError as exc:
            raise RuntimeError("Events limit must be a number from 1 to 200") from exc
        print_json(history_events(DB_PATH, max(1, min(200, limit))))
    elif op == "repo":
        if len(parts) == 1:
            print(configured_repository(cfg))
        elif len(parts) == 2:
            requested = DEFAULT_REPOSITORY if parts[1].lower() == "default" else parts[1]
            value = set_repository(cfg, requested)
            cfg = load_config()
            print(f"Update repository set to {value}.")
        else:
            raise RuntimeError("Usage: repo [owner/repository|default]")
    elif op == "check":
        check_update(cfg)
    elif op in ("install", "reinstall"):
        action = "reinstall" if op == "reinstall" else "install"
        if not confirm(f"This will {action} Torrent Dashboard from {configured_repository(cfg)}."):
            print("Cancelled.")
        else:
            install_update(cfg, force=op == "reinstall")
    elif op == "update":
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub == "status" and len(parts) == 2:
            print_update_status()
        elif sub == "check" and len(parts) == 2:
            check_update(cfg)
        elif sub == "repo":
            if len(parts) == 2:
                print(configured_repository(cfg))
            elif len(parts) == 3:
                requested = DEFAULT_REPOSITORY if parts[2].lower() == "default" else parts[2]
                value = set_repository(cfg, requested)
                cfg = load_config()
                print(f"Update repository set to {value}.")
            else:
                raise RuntimeError("Usage: update repo [owner/repository|default]")
        elif sub == "download" and len(parts) == 2:
            stage_update_download(cfg)
        elif sub == "install" and len(parts) in (2, 3):
            requested_version = parts[2] if len(parts) == 3 else None
            label = f" {requested_version}" if requested_version else ""
            if not confirm(f"Install the retained verified update{label}?"):
                print("Cancelled.")
            else:
                install_staged_update(requested_version)
        elif sub == "apply" and len(parts) == 2:
            if not confirm(f"Download, verify, and install the latest Torrent Dashboard release from {configured_repository(cfg)}?"):
                print("Cancelled.")
            else:
                staged = stage_update_download(cfg)
                if staged.get("state") != "upToDate":
                    install_staged_update(str(staged.get("version") or ""))
        elif sub == "reinstall" and len(parts) == 2:
            if not confirm(f"Reinstall Torrent Dashboard from {configured_repository(cfg)}?"):
                print("Cancelled.")
            else:
                install_update(cfg, force=True)
        elif sub == "clear" and len(parts) == 2:
            if confirm("Delete staged update files and update-status.json?"):
                clear_update_state()
            else:
                print("Cancelled.")
        else:
            raise RuntimeError("Usage: update status|check|repo|download|install [version]|apply|reinstall|clear")
    elif op == "backup":
        print(f"Config backup created: {backup_config()}")
    elif op == "backups":
        items = list_backups()
        if not items:
            print("No recovery config backups exist.")
        for item in items:
            print(item.name)
    elif op == "restore":
        if len(parts) != 2:
            raise RuntimeError("Usage: restore <backup-file>")
        if not confirm(f"Restore {parts[1]} and replace the current config.json?"):
            print("Cancelled.")
        else:
            source = restore_config(parts[1])
            cfg = load_config()
            print(f"Restored {source.name}.")
    elif op == "network-reset":
        if not confirm("Reset dashboard bind/port/HTTPS settings?"):
            print("Cancelled.")
        else:
            network_reset(cfg)
            cfg = load_config()
    elif op == "clear-update":
        if not confirm("Delete staged update files and update-status.json?"):
            print("Cancelled.")
        else:
            clear_update_state()
    elif op == "start":
        start_dashboard()
    elif op == "log":
        if len(parts) > 2:
            raise RuntimeError("Usage: log [lines]")
        try:
            lines = int(parts[1]) if len(parts) == 2 else 80
        except ValueError as exc:
            raise RuntimeError("Log line count must be a number") from exc
        tail_log(lines)
    else:
        raise RuntimeError("Unknown recovery command. Type help.")
    return cfg


def interactive(cfg: dict) -> None:
    print(f"\nTorrent Dashboard Local Recovery · {current_version()}")
    print("Authenticated with the setup recovery key. Type help for commands.\n")
    while True:
        try:
            cfg = execute(input("recovery> "), cfg)
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print("\nUse exit to leave local recovery.")
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Torrent Dashboard local recovery tool")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Optional recovery command to run after authentication")
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
        require_recovery_key(cfg)
        if args.command:
            execute(" ".join(args.command), cfg)
        else:
            interactive(cfg)
        return 0
    except Exception as exc:
        print(f"Recovery failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
