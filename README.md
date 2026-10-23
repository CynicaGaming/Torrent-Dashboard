<div align="center">

# Torrent Dashboard

**A clean, self-hosted qBitTorrent dashboard built for desktop and mobile browsers.**

![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)
![qBitTorrent](https://img.shields.io/badge/qBitTorrent-Web%20API-2F67BA)
![Release](https://img.shields.io/github/v/release/CynicaGaming/Torrent-Dashboard?include_prereleases&label=pre-release)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)

</div>

> [!NOTE]
> Torrent Dashboard is currently in **0.x prerelease development**. Features and configuration may change between builds.

## Overview

Torrent Dashboard provides a modern browser interface for monitoring and managing one or more qBitTorrent clients. It is intended for local or trusted-network use and is designed for phones, tablets, and desktop displays.

### Highlights

- Live torrent status, progress, speed, ETA, ratio, and health information
- Responsive desktop and mobile interface
- qBitTorrent-style torrent context actions and details
- Multiple qBitTorrent client support
- Administrator and Standard User dashboard roles with self-service account profiles
- Trusted network-interface and IP/CIDR access controls
- Modular media-service and notification integrations
- Browser notifications and configurable completion sounds
- Manual in-application prerelease updates from a **public GitHub repository**
- Single-instance protection and update rollback safeguards

## Quick Start

### Requirements

- Python **3.13 or newer** for the editable/source package
- qBitTorrent with its **Web UI enabled**
- qBitTorrent 5.2+ is recommended for Web API key authentication

### Windows

The editable source package and the compiled Windows preview are both produced from the same `src/torrent_dashboard/` source tree.

For the source package:

1. Download the latest `Torrent-Dashboard-X.Y.Z.zip` from GitHub Releases.
2. Extract it to a permanent folder.
3. Run `Start Dashboard.bat`.
4. Complete the First Run Setup wizard.

For the compiled preview, download `TorrentDashboard-Windows-X.Y.Z-x64.zip`, extract the complete folder, and run `Dashboard.exe`. `Recovery.exe` and `Updater.exe` are included beside it. The Windows executable remains a prerelease/preview distribution while repeated compiled update and rollback scenarios are being exercised.

### Linux

1. Download and extract the latest source release ZIP.
2. Run `python3 src/torrent_dashboard/dashboard.py` from the extracted directory.
3. Complete the First Run Setup wizard.

Torrent Dashboard listens on `0.0.0.0` so permitted devices on your network can reach it. The wizard detects the local address and lets you choose the dashboard port and trusted interfaces.

## Configuration

Configuration is handled through the First Run Setup wizard and **Settings**. A hand-edited example configuration is intentionally not shipped.

Runtime configuration is stored in `config.json`; databases, uploaded sounds, update state, integrity cache, and backups are stored under `data/`. Both are ignored by Git and excluded from release packages.

Stored passwords, qBitTorrent API keys, integration secrets, and webhook URLs are redacted before settings data is returned to the browser. They are still local application secrets at rest, so access to the Torrent Dashboard installation directory should be restricted to trusted operating-system users.

## Updates

1. Open **Settings → Updates**.
2. Enter the public GitHub repository as `owner/repository`, then select **Save**.
3. Select **Check for updates**. Torrent Dashboard validates that the repository is publicly reachable before comparing releases.

No GitHub access token is required or supported by the default updater. Torrent Dashboard reads public GitHub Release metadata, verifies GitHub's SHA-256 digest for the release ZIP, stages the update, restarts, and rolls back if the new version fails its health check.

Release automation publishes `Torrent-Dashboard-X.Y.Z.zip` plus a generated `Torrent-Dashboard-X.Y.Z.release.json` provenance sidecar. The updater trusts the GitHub asset digest; the sidecar exists for release provenance and external inspection rather than as a second independently authored checksum source.

## Local recovery

The dashboard does not expose an embedded command console. Administrative diagnostics and recovery commands run locally through `Recovery.exe` on Windows or `torrent-dashboard-recovery` from the source package. The local recovery tool requires the setup-generated recovery key and never starts a listening recovery server. It can inspect redacted configuration, clients, integrations, users, history events, live qBittorrent torrent state, and Jellyfin tasks; its torrent mutations are limited to start, stop, recheck, and reannounce.

The browser recovery-key sign-in remains a separate account-recovery path; it does not expose the local command surface.

## Security and Privacy

Keep qBitTorrent itself on localhost or another protected interface whenever possible and expose only Torrent Dashboard to trusted clients.

The repository intentionally excludes live configuration and runtime data. Release packaging also runs a public-repository hygiene check that rejects common credential formats, private-key material, `.env` files, `config.json`, and runtime data if they are accidentally tracked.

If you discover a security issue, do not post credentials or sensitive exploit details in a public issue.

## Development

The maintained Python application lives under `src/torrent_dashboard/`. The Windows executable package is generated from that same source tree and contains `Dashboard.exe`, `Recovery.exe`, and `Updater.exe` in one extracted folder; frontend assets and runtime state remain external.

The compiled executables are rebuildable artifacts, not a separate codebase. Modify the Python/browser source normally and rebuild the Windows package from the updated checkout. See [`docs/WINDOWS_BUILD.md`](docs/WINDOWS_BUILD.md) for the local PyInstaller build command, package layout, smoke tests, and update/rollback test procedure.

Start with [`DEVELOPMENT.md`](DEVELOPMENT.md) for the contributor/fork workflow and [`HANDOFF.md`](HANDOFF.md) for the current portable development handoff. Architecture and module ownership are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md), interface/content conventions in [`DESIGN_LANGUAGE.md`](DESIGN_LANGUAGE.md), and the automated/manual verification contract in [`TESTING.md`](TESTING.md).

Important architectural choices and their rationale are kept under [`docs/decisions/`](docs/decisions/). Current unfinished engineering intent lives in [`development/current.json`](development/current.json); generated released-state context remains in [`PROJECT_STATE.md`](PROJECT_STATE.md).

Torrent Dashboard is a public project and these continuity files are intentionally fork-safe. Canonical upstream branch and PR references are labeled as upstream context rather than assumptions about a fork. Fork maintainers should update `development/current.json` when their roadmap diverges and should never place credentials, private infrastructure details, or conversation transcripts in public handoff material.

Backend tests and reusable source validation can be run with:

```bash
python release_tools/validate_source.py
```

UI contract validation can be run with:

```bash
python release_tools/validate_ui_strings.py
```

Structured release metadata in `release_notes/releases.json` generates the changelog, project state, portable handoff, and GitHub release body. Pull requests and forks are welcome. Fork maintainers can point **Settings → Updates** at their own public release repository or change `DEFAULT_UPDATE_REPOSITORY` for their build.

## Backups

Administrators can manage portable installation backups under **Settings → Backups**. A `.tdbackup` archive contains `config.json` plus persistent dashboard state such as history, profile pictures, and notification assets. It does not contain Torrent Dashboard application binaries, staged updates, or qBitTorrent data. Backup archives contain saved credentials and recovery data, so store exported files securely. Restores automatically create a pre-restore safety backup first.

